"""IP-15 item 4: the steps restore-control.py leaves to a human match docs/operations.md."""
import importlib.util
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from agentiac import command  # noqa: E402

SPEC = importlib.util.spec_from_file_location('restore_control', ROOT / 'scripts' / 'restore-control.py')
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)


def planned(*argv):
    """The argv the command would exec, without running it."""
    with patch.object(command, 'run', side_effect=lambda plan: plan) as run:
        command.main(list(argv))
    return run.call_args.args[0]


class DocumentTests(unittest.TestCase):
    """The steps the script refuses to run are the steps the operations document lists, in the same order."""

    def documented(self):
        text = (ROOT / 'docs' / 'operations.md').read_text()
        section = text[text.index('**Restore.**'):]
        section = section[:section.index('\n### ')]
        return re.findall(r'^\d+\. `([^`]+)`', section, flags=re.M)

    def test_the_document_lists_the_same_steps_in_the_same_order(self):
        expected = [step.replace(restore.cli_name(), 'agentiac', 1) for step, _ in restore.HUMAN_STEPS]
        self.assertEqual(self.documented(), expected)

    def test_the_restore_command_is_documented(self):
        self.assertIn('agentiac ops restore', (ROOT / 'docs' / 'operations.md').read_text())

    def test_the_cli_passes_the_target_and_nothing_else_by_default(self):
        plan = planned('ops', 'restore', '--into', '/tmp/rebuilt')
        self.assertEqual(plan[1:], [str(ROOT / 'scripts/restore-control.py'), '--into', '/tmp/rebuilt'])

    def test_a_requested_archive_and_digest_are_passed_on(self):
        plan = planned('ops', 'restore', '--into', '/tmp/rebuilt', '--archive', 'a.tar.zst', '--sha256', 'f' * 64,
                       '--dry-run')
        self.assertEqual(plan[1:], [str(ROOT / 'scripts/restore-control.py'), '--into', '/tmp/rebuilt',
                                    '--dry-run', '--archive', 'a.tar.zst', '--sha256', 'f' * 64])


if __name__ == '__main__':
    unittest.main()
