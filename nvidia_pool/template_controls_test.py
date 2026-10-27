import unittest
from nvidia_pool.server import prepare_request


class RetiredModelTests(unittest.TestCase):
    def test_retired_glm_models_cannot_reenter_the_pool(self):
        for model in ('z-ai/glm-5.2', 'z-ai/glm-5.3', 'z-ai/glm-5.3-flash'):
            with self.subTest(model=model), self.assertRaises(ValueError):
                prepare_request({'model':model,'reasoning_effort':'low'})


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
