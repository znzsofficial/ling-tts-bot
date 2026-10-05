import importlib.util
import pathlib
import unittest
from types import SimpleNamespace


@unittest.skipUnless(importlib.util.find_spec('maibot_sdk'), 'requires MaiBot SDK')
class PlannerCompatibility(unittest.IsolatedAsyncioTestCase):
    async def test_current_and_legacy_payloads_receive_one_instruction(self):
        spec = importlib.util.spec_from_file_location('tts_planner_test', pathlib.Path(__file__).with_name('plugin.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        bot = SimpleNamespace(config=SimpleNamespace(trigger=SimpleNamespace(mode='llm_trigger'),
                                                     output=SimpleNamespace(mode='text_and_voice')))
        bot._planner_instruction = lambda: mod.LingTTSBot._planner_instruction(bot)
        for key in ('items', 'messages'):
            payload = {key: [], 'tool_definitions': []}
            for _ in range(2):
                payload = (await mod.LingTTSBot.configure_planner_tools(bot, **payload))['modified_kwargs']
            self.assertEqual(len(payload[key]), 1)
            if key == 'items':
                self.assertEqual(payload[key][0]['item_type'], 'SystemMessageItem')
                self.assertIn('ling_text_reply', payload[key][0]['parts'][0]['text'])
