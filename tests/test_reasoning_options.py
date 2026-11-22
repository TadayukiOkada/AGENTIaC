"""Explicit per-task generation options retain defaults and cannot override other fields."""
from pathlib import Path
import unittest
from unittest.mock import patch
from agentiac.tasks import invoke
from agentiac.repo import repository_case


class ReasoningOptionTests(unittest.TestCase):
    def test_only_explicit_none_changes_payload(self):
        with patch('agentiac.tasks.request', return_value={}) as request:
            invoke('base', 'key', 'model', [])
            default = request.call_args.args[2]
            self.assertNotIn('reasoning_effort', default)
            invoke('base', 'key', 'model', [], reasoning_effort='none')
            explicit = request.call_args.args[2]
            self.assertEqual(explicit, dict(default, reasoning_effort='none'))

    def test_invalid_options_fail_before_network(self):
        with patch('agentiac.tasks.request') as request:
            for option in ('high', '', {}, False):
                with self.subTest(option=option), self.assertRaises(ValueError):
                    invoke('base', 'key', 'model', [], reasoning_effort=option)
            request.assert_not_called()

    def test_repository_rejects_invalid_option_before_cloning(self):
        with patch('agentiac.repo.subprocess.run') as run:
            with self.assertRaises(ValueError):
                repository_case('', '', '', {'reasoning_effort': 'high'}, Path('/nonexistent'))
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
