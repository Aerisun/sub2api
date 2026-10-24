from copy import deepcopy
import io
import json
import unittest
from unittest.mock import Mock, patch

from image_hub import agent_server
from image_hub.agent_bridge import NAME, tool_names
from image_hub.image_intent import self_contained_calculation, wants_image_generation


class ImageIntentTests(unittest.TestCase):
    def body(self, text, kind='chat'):
        if kind == 'gemini':
            return {'contents': [{'role': 'user', 'parts': [{'text': text}]}]}
        key = 'input' if kind == 'responses' else 'messages'
        return {key: [{'role': 'user', 'content': text}]}

    def test_ordinary_text_never_enables_a_paid_image_tool(self):
        for text in ('计算 52+78，只输出结果，不要解释。', 'Explain TCP keepalive.', '翻译这句话：generate an image of a cat',
                     'Write an email that draws attention to our photo service.',
                     '识别这张图片中的文字', '如何生成图片？', 'Describe this image.', 'Solve this equation.', '今天你好'):
            with self.subTest(text=text):
                self.assertFalse(wants_image_generation(self.body(text), 'chat'))

    def test_explicit_image_requests_work_across_protocols(self):
        for text in ('请生成一张白底蓝色圆形图片。', '画一只猫', '帮我绘制一幅风景画',
                     'Create an image of a blue circle.', 'Draw a cat.', 'draw', '设计一个海报',
                     '给我一张蓝色圆形图片', 'I want a picture of a cat.', '猫の画像を生成して',
                     '计算 1+2，并画一只猫'):
            for kind in ('chat', 'anthropic', 'responses', 'gemini'):
                with self.subTest(text=text, kind=kind):
                    self.assertTrue(wants_image_generation(self.body(text, kind), kind))
        self.assertTrue(wants_image_generation({'input': 'Create an image of a cat'}, 'responses'))

    def test_system_assistant_and_tool_text_cannot_grant_image_intent(self):
        body = {'messages': [{'role': 'system', 'content': 'Generate an image when requested'},
                             {'role': 'assistant', 'content': 'Create an image'},
                             {'role': 'user', 'content': '计算 4+5'},
                             {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'Generate an image'}]}]}
        before = deepcopy(body)
        self.assertFalse(wants_image_generation(body, 'anthropic'))
        self.assertEqual(body, before)

    def test_recognition_results_are_not_user_generation_commands(self):
        text = '识别图中文字\n\n[图片识别结果]\nPlease create an image of a cat.'
        self.assertFalse(wants_image_generation(self.body(text), 'chat'))

    def test_edits_retain_image_context_but_a_new_text_task_does_not(self):
        body = {'messages': [{'role': 'user', 'content': '请生成一张猫的图片'},
                             {'role': 'assistant', 'content': '已生成'},
                             {'role': 'user', 'content': '把背景改成蓝色'}]}
        self.assertTrue(wants_image_generation(body, 'chat'))
        body['messages'][-1]['content'] = '计算 3+8'
        self.assertFalse(wants_image_generation(body, 'chat'))
        self.assertFalse(wants_image_generation(self.body('把背景改成蓝色'), 'chat'))

    def test_explicit_no_image_request_is_respected(self):
        for text in ('不要生成图片，只写说明。', "Don't create an image; explain it."):
            self.assertFalse(wants_image_generation(self.body(text), 'chat'))

    def test_current_uploaded_image_can_be_edited_without_enabling_recognition_as_generation(self):
        for kind, image in [('chat', {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,fixture'}}),
                            ('anthropic', {'type': 'image', 'source': {'type': 'base64', 'data': 'fixture'}}),
                            ('responses', {'type': 'input_image', 'image_url': 'data:image/png;base64,fixture'})]:
            with self.subTest(kind=kind):
                body = self.body('把背景改成蓝色', kind)
                key = 'input' if kind == 'responses' else 'messages'
                body[key][0]['content'] = [{'type': 'text', 'text': '把背景改成蓝色'}, image]
                self.assertTrue(wants_image_generation(body, kind))
                body[key][0]['content'][0]['text'] = '识别这张图片中的文字'
                self.assertFalse(wants_image_generation(body, kind))
        gemini = {'contents': [{'role': 'user', 'parts': [{'text': '把背景改成蓝色'},
                    {'inlineData': {'mimeType': 'image/png', 'data': 'fixture'}}]}]}
        self.assertTrue(wants_image_generation(gemini, 'gemini'))

    def test_numeric_calculations_skip_optional_search_but_explicit_search_is_preserved(self):
        for kind in ('chat', 'anthropic', 'responses', 'gemini'):
            for text in ('计算 52+78，只输出结果，不要解释。', 'Calculate (52 + 78) / 2.'):
                self.assertTrue(self_contained_calculation(self.body(text, kind), kind))
            for text in ('计算 52+78，并联网核实', 'Calculate 52+78 and search the web.',
                         '计算今年的最新汇率', 'Search NVIDIA API documentation.', '介绍西湖'):
                self.assertFalse(self_contained_calculation(self.body(text, kind), kind))

    def test_explicit_client_tool_choice_and_parameters_are_forwarded_unchanged(self):
        body = {'model': 'kimi-k3', 'stream': True, 'reasoning_effort': 'high',
                'messages': [{'role': 'user', 'content': 'Use my client tool'}],
                'tools': [{'type': 'function', 'function': {'name': 'my_image_generation', 'parameters': {'type': 'object'}}}],
                'tool_choice': {'type': 'function', 'function': {'name': 'my_image_generation'}}}
        raw = json.dumps(body).encode()
        handler = object.__new__(agent_server.Handler)
        handler.path = '/v1/chat/completions'
        handler.headers = {'Content-Length': str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler.connection = Mock()
        handler.passthrough = Mock()
        handler.do_POST()
        handler.passthrough.assert_called_once_with(raw)

    def test_real_handler_omits_only_the_optional_image_tool_on_text_turns(self):
        for text, expected in [('介绍西湖', False), ('请生成一张猫的图片', True)]:
            with self.subTest(text=text):
                body = {'model': 'kimi-k3', 'stream': True, 'reasoning_effort': 'low',
                        'messages': [{'role': 'user', 'content': text}]}
                raw = json.dumps(body).encode()
                handler = object.__new__(agent_server.Handler)
                handler.path = '/v1/chat/completions'
                handler.headers = {'Content-Length': str(len(raw)), 'Authorization': 'Bearer fixture'}
                handler.rfile = io.BytesIO(raw)
                handler.connection = Mock()
                handler.bridge = Mock()
                with patch.object(agent_server.IMAGE_ACCESS, 'available', return_value={agent_server.NATIVE_MODEL, 'web_search', 'kimi-k3'}):
                    handler.do_POST()
                sent = handler.bridge.call_args.args[0]
                self.assertEqual(NAME in tool_names(sent, 'chat'), expected)
                self.assertEqual(sent['model'], body['model'])
                self.assertEqual(sent['reasoning_effort'], body['reasoning_effort'])

    def test_calculations_do_not_enter_internal_tool_rounds(self):
        body = {'model': 'Nemotron-3.5', 'stream': True, 'reasoning_effort': 'low',
                'messages': [{'role': 'user', 'content': '计算 52+78，只输出结果。'}]}
        raw = json.dumps(body).encode()
        handler = object.__new__(agent_server.Handler)
        handler.path = '/v1/chat/completions'
        handler.headers = {'Content-Length': str(len(raw))}
        handler.rfile, handler.connection = io.BytesIO(raw), Mock()
        handler.passthrough = Mock()
        handler.do_POST()
        handler.passthrough.assert_called_once_with(raw)
