"""Native Codex images use the gateway, retaining owner and retry isolation."""
import tempfile
import base64
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, build_opener, ProxyHandler
from unittest.mock import patch

from image_hub.agent_bridge import ImageExecutor, eligible, inject, image_model_for_chat, image_tool_metadata
from image_hub.model_access import ImageAccessCache
from image_hub.server import Hub, Handler, HubError, MODEL, NATIVE_MODEL, NATIVE_MODELS, validate_payload


class NativeImageTests(unittest.TestCase):
    def test_accepted_task_model_survives_executor_and_progress(self):
        for model in NATIVE_MODELS:
            with self.subTest(model=model), patch('image_hub.agent_bridge.request_json', return_value=(200, {'task_id': 'accepted', 'status': 'completed', 'model': model})):
                events = []
                execute = ImageExecutor('sk-fixture', model='gpt-image-2')
                execute.progress = events.append
                result = execute({'prompt': 'fixture', 'model': model}, 'chosen')
                self.assertEqual(result['model'], model)
                self.assertEqual(events[0]['metadata']['model'], model)
                self.assertEqual(image_tool_metadata(result), {'metadata': {'model': model}})

    def test_missing_task_model_is_not_guessed_from_default_or_arguments(self):
        with patch('image_hub.agent_bridge.request_json', return_value=(200, {'task_id': 'legacy', 'status': 'completed'})):
            events = []
            execute = ImageExecutor('sk-fixture')
            execute.progress = events.append
            result = execute({'prompt': 'fixture', 'model': 'gpt-image-2'}, 'legacy')
        self.assertNotIn('model', result)
        self.assertNotIn('metadata', events[0])
        self.assertEqual(image_tool_metadata(result), {})
        self.assertEqual(image_tool_metadata({'model': 'gpt-6-sol'}), {})

    def test_image_model_metadata_reaches_all_protocol_tool_events(self):
        from image_hub.agent_tests import BODIES, GENERATORS, RESULT, frames
        from image_hub.agent_bridge import Stream, run
        from internal_tools.chat_bridge import ToolExecutor
        for kind, generate in GENERATORS.items():
            for shared in (False, True):
                for model in NATIVE_MODELS:
                    with self.subTest(kind=kind, shared=shared, model=model):
                        events, requests = [], []
                        def upstream(body):
                            requests.append(body)
                            return frames(generate(call=len(requests) == 1))
                        def image(*_):
                            return {**RESULT, 'model': model}
                        execute = ToolExecutor(image=image) if shared else image
                        stream = Stream(kind, lambda event, value: events.append((event, value)))
                        run(inject(BODIES[kind], kind), kind, upstream, execute, stream)
                        completed = [value for event, value in events if event == 'aerisun.tool' and value.get('status') == 'completed']
                        self.assertEqual(len(completed), 1)
                        self.assertEqual(completed[0]['metadata']['model'], model)

    def test_nonstream_tool_result_contains_image_model_metadata(self):
        import io
        from image_hub.agent_bridge import NAME
        from internal_tools.chat_bridge import ToolExecutor
        replies = [
            {'id': 'first-fixture', 'choices': [{'message': {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'image-fixture', 'type': 'function', 'function': {'name': NAME, 'arguments': '{"prompt":"fixture"}'}}]}, 'finish_reason': 'tool_calls'}]},
            {'id': 'second-fixture', 'choices': [{'message': {'role': 'assistant', 'content': 'done'}, 'finish_reason': 'stop'}]}
        ]
        def upstream(body):
            return io.BytesIO(json.dumps(replies.pop(0)).encode())
        result = ToolExecutor(image=lambda *_: {'status': 'completed', 'model': 'gpt-image-2.5-sunburst', 'result': {'data': []}}).run({'model': 'gpt-6-sol', 'messages': []}, 'chat', upstream)
        self.assertEqual(result['aerisun_tools'][0]['metadata']['model'], 'gpt-image-2.5-sunburst')

    def test_internal_model_selects_each_native_image_model(self):
        for model in NATIVE_MODELS:
            with self.subTest(model=model), patch('image_hub.agent_bridge.request_json', return_value=(200, {'status': 'completed'})) as request:
                result = ImageExecutor('sk-native')({'prompt': 'circle', 'model': model, 'quality': 'high'}, 'chosen')
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(request.call_args.args[2]['model'], model)
                self.assertEqual(request.call_count, 1)

    def test_unavailable_models_are_rejected_without_submission(self):
        with patch('image_hub.agent_bridge.request_json') as request:
            for model in (MODEL, 'unknown', 'gpt-image-2.5-sunburst'):
                execute = ImageExecutor('sk-flare-only', allowed_models=[NATIVE_MODEL])
                self.assertEqual(execute({'prompt': 'circle', 'model': model}, 'denied')['status'], 'failed')
            request.assert_not_called()
        with self.assertRaises(HubError):
            validate_payload({'model': MODEL, 'prompt': 'circle'})
        self.assertEqual(validate_payload({'prompt': 'circle'})['model'], NATIVE_MODEL)

    def test_tool_catalogues_expose_only_permitted_native_choices(self):
        examples = [('chat', {'messages': []}, 'parameters'), ('anthropic', {}, 'input_schema'),
                    ('responses', {}, 'parameters'), ('gemini', {}, 'parameters')]
        for kind, body, schema_key in examples:
            tool = inject(body, kind)['tools'][-1]
            tool = tool.get('function', tool)
            if kind == 'gemini': tool = tool['functionDeclarations'][0]
            self.assertEqual(set(tool[schema_key]['properties']['model']['enum']), NATIVE_MODELS)
        body = inject({'messages': []}, 'chat', image_model='gpt-image-2', allowed_models=['gpt-image-2'])
        choice = body['tools'][-1]['function']['parameters']['properties']['model']
        self.assertEqual(choice['enum'], ['gpt-image-2'])
        self.assertEqual(choice['default'], 'gpt-image-2')

    def test_one_catalogue_lookup_covers_all_three_choices(self):
        cache = ImageAccessCache(ttl=30)
        with patch('image_hub.model_access.request_json', return_value=(200, {'data': [{'id': model} for model in NATIVE_MODELS]})) as request:
            for model in NATIVE_MODELS:
                self.assertTrue(cache.allowed('http://gateway', 'sk-native', model=model))
            self.assertEqual(request.call_count, 1)

    def test_native_responses_are_not_intercepted(self):
        for model in ('gpt-6.1-sol', 'gpt-6-sol', 'gpt-6-astra', 'gpt-6-luna', 'gpt-5.6-sol'):
            self.assertFalse(eligible({'model': model}, 'responses', '/v1/responses'))
            self.assertTrue(eligible({'model': model}, 'anthropic', '/v1/messages'))
            self.assertEqual(image_model_for_chat(model), NATIVE_MODEL)
        self.assertEqual(image_model_for_chat('glm-5.3'), NATIVE_MODEL)
        self.assertTrue(eligible({'model': 'glm-5.3'}, 'responses', '/v1/responses'))

    def test_image25_variants_and_legacy_preserve_native_parameters(self):
        for model in NATIVE_MODELS:
            payload=validate_payload({'model':model, 'prompt':'circle', 'quality':'max'})
            self.assertEqual(payload['model'],model)
            self.assertEqual(payload['quality'],'max')
            self.assertEqual(payload['response_format'],'b64_json')
        with self.assertRaises(HubError):
            validate_payload({'model':NATIVE_MODEL, 'prompt':'circle', 'quality':'unknown'})

    def test_native_executor_keeps_model_and_never_resubmits(self):
        with patch('image_hub.agent_bridge.request_json', return_value=(200, {'task_id': 'native', 'status': 'completed'})) as request:
            execute=ImageExecutor('sk-native', model=NATIVE_MODEL)
            self.assertEqual(execute({'prompt': 'one circle'}, 'first')['status'], 'completed')
            self.assertEqual(execute({'prompt': 'one circle'}, 'second')['status'], 'failed')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[2]['model'], NATIVE_MODEL)
        body=inject({'model': 'gpt-6-sol', 'messages': []}, 'anthropic', image_model=NATIVE_MODEL)
        description=body['tools'][-1]['description']
        self.assertIn('native image_generation', description)
        self.assertNotIn('0.20', description)

    def test_native_task_is_owner_scoped_and_has_no_rightapi_price(self):
        with tempfile.TemporaryDirectory() as folder:
            hub=Hub(folder, env={'RIGHTAPI_IMAGE_API_KEY': 'unused'})
            calls=[]
            def request(url, key, body=None, **kwargs):
                calls.append((url, key, body))
                if url.endswith('/models'):
                    return 200, {'data': [{'id': NATIVE_MODEL}]}
                return 200, {'data': [{'url': 'https://example.invalid/native.png'}]}
            with patch('image_hub.server.request_json', side_effect=request), patch.object(hub, 'download_image', side_effect=HubError(502, 'fixture')):
                task=hub.submit('sk-native', {'model': NATIVE_MODEL, 'prompt': 'circle'}, idempotency='native-once')
                for _ in range(100):
                    result=hub.get_task('sk-native', task['task_id'])
                    if result['status']!='processing':break
                    time.sleep(.01)
                repeated=hub.submit('sk-native', {'model': NATIVE_MODEL, 'prompt': 'circle'}, idempotency='native-once')
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['model'], NATIVE_MODEL)
            self.assertNotIn('cost', result)
            self.assertNotIn('per_image', result['pricing'])
            self.assertEqual(repeated['task_id'], task['task_id'])
            generation=[call for call in calls if call[0].endswith('/images/generations')]
            self.assertEqual(len(generation),1)
            self.assertEqual(generation[0][1], 'sk-native')
            self.assertEqual(generation[0][2]['model'], NATIVE_MODEL)
            with self.assertRaises(HubError):hub.get_task('sk-other', task['task_id'])

    def test_cached_permission_does_not_cross_image_models(self):
        cache=ImageAccessCache(ttl=30)
        with patch('image_hub.model_access.request_json', return_value=(200, {'data': [{'id': MODEL}]})):
            self.assertTrue(cache.allowed('http://gateway', 'sk-test', model=MODEL))
            self.assertFalse(cache.allowed('http://gateway', 'sk-test', model=NATIVE_MODEL))

    def test_http_native_only_key_can_submit_and_poll(self):
        with tempfile.TemporaryDirectory() as folder:
            hub=Hub(folder, env={})
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.hub=hub
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            opener=build_opener(ProxyHandler({}))
            def request(url,key,body=None,**kwargs):
                if url.endswith('/models'):return 200,{'data':[{'id':NATIVE_MODEL}]}
                self.assertEqual(body['model'],NATIVE_MODEL)
                self.assertEqual(body['response_format'],'b64_json')
                return 200,{'data':[{'b64_json':base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()}]}
            def http(path,body=None):
                req=Request(f'http://127.0.0.1:{server.server_port}'+path,
                            data=json.dumps(body).encode() if body is not None else None,
                            headers={'Authorization':'Bearer sk-native-only','Content-Type':'application/json'})
                with opener.open(req,timeout=3) as response:return json.load(response)
            try:
                with patch('image_hub.server.request_json',side_effect=request):
                    task=http('/v1/images/generations/async',{'model':NATIVE_MODEL,'prompt':'circle'})
                    for _ in range(100):
                        result=http('/v1/images/tasks/'+task['task_id'])
                        if result['status']!='processing':break
                        time.sleep(.01)
                    self.assertEqual(result['status'],'completed')
                    self.assertEqual(result['model'],NATIVE_MODEL)
                    self.assertNotIn('b64_json',result['result']['data'][0])
                    self.assertTrue(result['result']['data'][0]['url'].startswith('https://aerisun.top/sub2api/image-hub/results/'))
            finally:server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()
