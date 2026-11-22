"""pi loop guard extension: similar bash streaks are nudged, then stopped; edit-and-rerun is not counted."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
import agent  # noqa: E402


@unittest.skipUnless(agent.NODE.exists(), 'Node 24 not installed')
class LoopGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([str(agent.NODE), str(ROOT / 'tests/loop_guard_harness.mjs'),
                                 str(ROOT / 'agents/pi/extensions/loop-guard.ts')],
                                capture_output=True, text=True, timeout=60, env=dict(os.environ, NODE_NO_WARNINGS='1'))
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.out = json.loads(result.stdout)

    def test_similarity_matches_difflib_quick_ratio(self):
        import difflib
        script = lambda n: f"cd /work && python3 - <<'EOF'\nimport json\nprint(json.dumps({{'case': {n}}}))\nEOF"
        self.assertAlmostEqual(self.out['similarity']['near'],
                               difflib.SequenceMatcher(None, script(1), script(2)).quick_ratio())
        self.assertEqual(self.out['similarity']['same'], 1)
        self.assertLess(self.out['similarity']['apart'], 0.9)

    def test_streak_runs_nine_then_nudges_then_stops_at_twenty(self):
        verdicts = self.out['verdicts']
        self.assertEqual(verdicts[:9], [None] * 9)
        for v in verdicts[9:19]:
            self.assertTrue(v['block'])
            self.assertFalse(v['terminate'])
        self.assertTrue(verdicts[19]['block'])
        self.assertTrue(verdicts[19]['terminate'])
        self.assertIn('20 bash commands in a row', verdicts[19]['reason'])
        self.assertIsNone(verdicts[20])  # a new count after the stop

    def test_successful_edit_resets_and_failed_edit_does_not(self):
        self.assertEqual(self.out['cycle'], 0)
        self.assertEqual(self.out['failedEdit'], ['run'] * 9 + ['nudge'])
        self.assertEqual(self.out['state'], {'a': 'run', 'unchanged': 'nudge'})

    def test_a_cycle_of_different_commands_counts_as_repeating(self):
        # Neighbours differ (a run, then a check), yet each repeats the one two calls back.
        self.assertLess(self.out['pairSimilarity'], 0.9)
        self.assertEqual(self.out['pair'], ['run'] * 10 + ['nudge'] * 2)  # the first two both start a count
        # Only the last three commands are compared.
        self.assertEqual(self.out['outsideWindow'], ['run'] * 5)

    def test_blocks_are_shown_in_the_ui(self):
        self.assertEqual(self.out['notes'], 14)


class LoopGuardWiringTests(unittest.TestCase):
    def test_code_passes_the_flag_and_suite_keeps_v1_cases_unchanged(self):
        from agentiac import command
        args = command.build_parser().parse_args(['code', '.', '--loop-guard'])
        args.extra = []
        self.assertIn('--loop-guard', command.code(args))
        sys.path.insert(0, str(ROOT / 'scripts'))
        import importlib.util
        spec = importlib.util.spec_from_file_location('suite_runner', ROOT / 'scripts/suite.py')
        suite = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(suite)
        self.assertIsNone(suite.guard_spec(False))
        guard = suite.guard_spec(True)
        self.assertEqual(guard['sha256'], __import__('hashlib').sha256(
            (ROOT / 'agents/pi/extensions/loop-guard.ts').read_bytes()).hexdigest())
        self.assertEqual(suite.label({'alias': 'a'}), 'a')
        self.assertEqual(suite.label({'alias': 'a', 'loop_guard': guard}), 'a +loop-guard')
        self.assertEqual(suite.label({'alias': 'a', 'loop_guard': guard, 'thinking': 'high'}),
                         'a +loop-guard +thinking-high')


if __name__ == '__main__':
    unittest.main()
