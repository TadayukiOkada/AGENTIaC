"""Budget exhaustion preserves final tool results without accepting task success."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agentiac.repo import repository_case


class RepositoryBudgetTests(unittest.TestCase):
    def exercise(self, timeout=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'module.py').write_text('VALUE=1\n')
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test',
                            '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
            revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
            folder = root / 'attempt'
            folder.mkdir()
            calls = [] if timeout else [{'id': 'edit', 'type': 'function', 'function': {
                'name': 'write_file', 'arguments': json.dumps({'path': 'module.py', 'content': 'VALUE=2\n'})}}]
            calls.append({'id': 'test', 'type': 'function', 'function': {'name': 'run_tests', 'arguments': '{}'}})
            response = {'message': {'role': 'assistant', 'tool_calls': calls}, 'finish_reason': 'tool_calls'}
            original_run = subprocess.run
            docker_calls = []
            def local_run(command, **kwargs):
                if command[0] != 'docker':
                    return original_run(command, **kwargs)
                docker_calls.append(command)
                if len(docker_calls) == 1:
                    return subprocess.CompletedProcess(command, 1, '', 'baseline fails')
                if timeout:
                    raise subprocess.TimeoutExpired(command, 1)
                return subprocess.CompletedProcess(command, 0, 'tests passed', '')
            with patch('agentiac.repo.subprocess.run', side_effect=local_run), patch(
                    'agentiac.repo.invoke', return_value=response) as invoke:
                result = repository_case('local', 'unused', 'fixture', {
                    'repo_path': str(source), 'revision': revision, 'image': 'fixture',
                    'instruction': 'repair', 'test_command': ['fixed-test'], 'max_turns': 1}, folder)
            checkpoint = json.loads((folder / 'partial-trace.json').read_text())
            self.assertEqual(invoke.call_count, 1)
            self.assertEqual(len(docker_calls), 2)
            self.assertEqual((source / 'module.py').read_text(), 'VALUE=1\n')
            self.assertFalse(result['passed'])
            self.assertEqual(result['reason'], 'repository turn budget exceeded')
            self.assertEqual(result['turns'], 1)
            self.assertEqual(result['repository_revision'], revision)
            self.assertEqual(result['baseline_returncode'], 1)
            self.assertEqual((checkpoint['stage'], checkpoint['turn']), ('turn-budget-exhausted', 1))
            self.assertEqual(checkpoint['baseline_returncode'], 1)
            self.assertEqual(checkpoint['messages'][-1]['tool_call_id'], 'test')
            return result, checkpoint

    def test_passing_last_tool_is_saved_but_does_not_complete_task(self):
        result, checkpoint = self.exercise()
        self.assertTrue(result['wrote'])
        self.assertTrue(result['tested'])
        self.assertEqual(result['last_test_returncode'], 0)
        self.assertEqual(checkpoint['last_test_returncode'], 0)
        self.assertEqual(json.loads(checkpoint['messages'][-1]['content']),
                         {'exit_code': 0, 'output': 'tests passed'})

    def test_last_tool_timeout_is_saved_without_fabricating_exit_code(self):
        result, checkpoint = self.exercise(timeout=True)
        self.assertFalse(result['tested'])
        self.assertIsNone(result['last_test_returncode'])
        self.assertIsNone(checkpoint['last_test_returncode'])
        self.assertIn('error', json.loads(checkpoint['messages'][-1]['content']))


if __name__ == '__main__':
    unittest.main()
