"""Keep our public privacy defaults checked after removing the vendored Go test."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent


class PublicConfigTests(unittest.TestCase):
    def test_public_example_is_loopback_private_and_keeps_native_cue(self):
        config = json.loads((ROOT / 'config.example.json').read_text())
        self.assertEqual(config['listen'], '127.0.0.1:18861')
        self.assertEqual(config['mode'], 'hybrid')
        self.assertEqual(config['routing']['default_route'], 'llm')
        self.assertEqual(config['llm_base_url'], 'https://omniroute.example.invalid/v1')
        self.assertEqual(config['llm_model'], 'alice-gemini')
        self.assertIn('chat_template_kwargs', config['llm_chat_parameters'])
        self.assertIsNone(config['llm_chat_parameters']['chat_template_kwargs'])
        self.assertFalse(config['assistant_tools']['enabled'])
        self.assertFalse(config['conversation_log']['enabled'])
        self.assertEqual(config['llm_hosted_tools'], [])
        self.assertTrue(config['voice_cues']['enabled'])
        self.assertTrue(config['system_prompt'].strip())


if __name__ == '__main__':
    unittest.main()
