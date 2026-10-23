import unittest
from nvidia_pool.server import prepare_request


class GLMTemplateTests(unittest.TestCase):
    def test_unified_effort_reaches_native_glm_template_and_root(self):
        for model in ['z-ai/glm-5.3','z-ai/glm-5.3-flash']:
            for effort,expected in [('auto','low'),('low','low'),('medium','high'),('high','high'),('xhigh','max'),('max','max')]:
                with self.subTest(model=model,effort=effort):
                    value=prepare_request({'model':model,'reasoning_effort':effort})
                    self.assertEqual(value['reasoning_effort'],expected)
                    self.assertEqual(value['chat_template_kwargs']['reasoning_effort'],expected)
                    self.assertIs(value['chat_template_kwargs']['clear_thinking'],False)

    def test_template_only_effort_survives_and_explicit_unified_field_has_precedence(self):
        value=prepare_request({'model':'z-ai/glm-5.3-flash','chat_template_kwargs':{'reasoning_effort':'high'}})
        self.assertEqual(value['reasoning_effort'],'high')
        value=prepare_request({'model':'z-ai/glm-5.3-flash','reasoning_effort':'low','chat_template_kwargs':{'reasoning_effort':'max'}})
        self.assertEqual(value['chat_template_kwargs']['reasoning_effort'],'low')


class StepTemplateTests(unittest.TestCase):
    def test_disabled_thinking_uses_supported_minimum_without_corrupting_content(self):
        for controls in [{'reasoning_effort':'none'},{'enable_thinking':False},{'thinking':{'type':'disabled'}}]:
            value=prepare_request({'model':'stepfun-ai/step-5-preview',**controls,'chat_template_kwargs':{'enable_thinking':False}})
            self.assertIs(value['chat_template_kwargs']['enable_thinking'],True)
            self.assertIs(value['chat_template_kwargs']['preserve_thinking'],True)
            self.assertEqual(value['reasoning_effort'],'low')

    def test_explicit_step_intensity_and_thinking_history_are_preserved(self):
        value=prepare_request({'model':'stepfun-ai/step-5-preview','reasoning_effort':'max','messages':[{'role':'assistant','reasoning_content':'prior thinking'}]})
        self.assertEqual(value['reasoning_effort'],'max')
        self.assertEqual(value['messages'][0]['reasoning_content'],'prior thinking')
