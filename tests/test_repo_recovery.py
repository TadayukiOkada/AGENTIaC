"""Partial repository traces survive failures without a second model request."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from agentiac.repo import repository_case


class RepositoryRecoveryTests(unittest.TestCase):
    def test_http_failure_preserves_applied_tool_and_pending_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            (source / 'module.py').write_text('old = True\n')
            subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'fixture'], check=True)
            revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
            folder = root / 'attempt'
            folder.mkdir()
            response = {'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'edit-1', 'type': 'function', 'function': {'name': 'write_file',
                 'arguments': json.dumps({'path': 'module.py', 'content': 'old = False\n'})}}]}}
            run = subprocess.run
            def local_run(command, **kwargs):
                if command[0] == 'docker':
                    return subprocess.CompletedProcess(command, 1, '', 'baseline fails')
                return run(command, **kwargs)
            with patch('agentiac.repo.subprocess.run', side_effect=local_run), patch(
                    'agentiac.repo.invoke', side_effect=[response, HTTPError('local', 500, 'failure', {}, None)]) as invoke:
                with self.assertRaises(HTTPError):
                    repository_case('local', 'unused', 'fixture', {
                        'repo_path': str(source), 'revision': revision, 'image': 'fixture',
                        'instruction': 'repair', 'test_command': ['false'], 'max_turns': 3}, folder)
            state = json.loads((folder / 'partial-trace.json').read_text())
            self.assertEqual((state['stage'], state['turn']), ('request', 2))
            self.assertEqual(len(state['trace']), 1)
            self.assertTrue(state['wrote'])
            self.assertEqual(json.loads(state['messages'][-1]['content']), {'ok': True})
            self.assertEqual((folder / 'workspace/module.py').read_text(), 'old = False\n')
            self.assertEqual((source / 'module.py').read_text(), 'old = True\n')
            self.assertEqual(invoke.call_count, 2)
            self.assertFalse((folder / 'result.json').exists())
            self.assertFalse((folder / 'partial-trace.json.tmp').exists())


if __name__ == '__main__':
    unittest.main()
