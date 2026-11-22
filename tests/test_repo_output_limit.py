"""Output caps cannot apply tools or turn a passing patch into a completed trial."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agentiac.repo import repository_case


def response(finish, calls=None):
    return {'finish_reason': finish, 'message': {
        'role': 'assistant', 'content': 'final' if not calls else None,
        **({'tool_calls': calls} if calls else {})}}


def call(name, arguments, identifier):
    return {'id': identifier, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments)}}


class RepositoryOutputLimitTests(unittest.TestCase):
    def test_capped_response_never_applies_tools_or_completes_trial(self):
        edit = call('write_file', {'path': 'module.py', 'content': 'fixed = True\n'}, 'edit')
        tests = call('run_tests', {}, 'tests')
        variants = [
            ([response('length', [edit])], False, 1),
            ([response('tool_calls', [edit, tests]), response('length')], True, 2),
        ]
        for responses, edited, docker_calls in variants:
            with self.subTest(edited=edited), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'source'
                source.mkdir()
                subprocess.run(['git', 'init', '-q', str(source)], check=True)
                (source / 'module.py').write_text('fixed = False\n')
                subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
                subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test',
                                '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
                revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
                folder = Path(directory) / 'attempt'
                folder.mkdir()
                run = subprocess.run
                observed = []
                def local_run(command, **kwargs):
                    if command[0] == 'docker':
                        observed.append(command)
                        return subprocess.CompletedProcess(command, 0, 'passing tests', '')
                    return run(command, **kwargs)
                with patch('agentiac.repo.subprocess.run', side_effect=local_run), patch(
                        'agentiac.repo.invoke', side_effect=responses) as invoke:
                    result = repository_case('local', 'unused', 'fixture', {
                        'repo_path': str(source), 'revision': revision, 'image': 'fixture',
                        'instruction': 'repair', 'test_command': ['true']}, folder)
                self.assertFalse(result['passed'])
                self.assertEqual(result['failure_category'], 'model_output_limit')
                self.assertTrue(result['truncated'])
                self.assertEqual(len(observed), docker_calls)
                self.assertEqual(invoke.call_count, len(responses))
                self.assertEqual((folder / 'workspace/module.py').read_text(),
                                 'fixed = True\n' if edited else 'fixed = False\n')
                state = json.loads((folder / 'partial-trace.json').read_text())
                self.assertEqual(state['trace'][-1]['finish_reason'], 'length')


if __name__ == '__main__':
    unittest.main()
