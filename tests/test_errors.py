"""T8.4: the failures people hit say what failed, why it may have, how to check, and what to do next."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import adapters, command, declared, errors, paths  # noqa: E402
from agentiac.errors import explain, headline  # noqa: E402
import borrow  # noqa: E402

SITE = paths.root()
SECRET = 'sk-worker-key-sentinel'
HOST = {'ansible_host': 'worker.example', 'api_key': SECRET, 'key': SECRET}


class ExplainTests(unittest.TestCase):
    def test_the_first_line_is_the_sentence_it_was_given(self):
        self.assertEqual(explain('it broke', ['one'], 'look', 'fix').splitlines()[0], 'it broke')

    def test_every_part_has_its_own_line(self):
        lines = explain('it broke', ['one', 'two'], 'look', 'fix').splitlines()
        self.assertEqual(lines[1:], ['  cause: one', '  cause: two', '  check: look', '  next:  fix'])

    def test_no_causes_still_says_how_to_check_and_what_next(self):
        self.assertEqual(explain('it broke', [], 'look', 'fix').splitlines(),
                         ['it broke', '  check: look', '  next:  fix'])

    def test_headline_gives_back_what_was_given(self):
        self.assertEqual(headline(explain('it broke', ['one'], 'look', 'fix')), 'it broke')
        self.assertEqual(headline(explain('it broke', [], 'look', 'fix')), 'it broke')

    def test_headline_keeps_a_multi_line_sentence_and_ordinary_errors(self):
        self.assertEqual(headline(explain('it broke\nsecond line', ['one'], 'look', 'fix')), 'it broke\nsecond line')
        self.assertEqual(headline(OSError('docker is down')), 'docker is down')


class Advice(unittest.TestCase):
    """Each site, raised for real. Advice that names a wrong command is worse than none, so the text is pinned."""

    def advice(self, call):
        with self.assertRaises(SystemExit) as raised:
            call()
        text = raised.exception.code
        self.assertIn('\n  check: ', text)
        self.assertIn('\n  next:  ', text)
        self.assertNotIn(SECRET, text)
        return text

    def adapter_error(self, call):
        with self.assertRaises(adapters.AdapterError) as raised:
            call()
        return str(raised.exception)


class MissingProgramTests(Advice):
    def test_a_program_of_the_venv_points_at_bootstrap_and_the_directory_it_looked_in(self):
        with mock.patch.object(command.os, 'chdir'):
            text = self.advice(lambda: command.run(['/nowhere/.venv/bin/python', 'x']))
        self.assertEqual(text.splitlines()[0], '/nowhere/.venv/bin/python not found; see Bootstrap in README.md')
        self.assertIn('worktree', text)
        self.assertIn('check: ls /nowhere/.venv/bin', text)

    def test_the_gateway_venv_is_a_venv_too(self):
        self.assertIn('check: ls /x/.venv-gateway/bin', command.missing_program('/x/.venv-gateway/bin/litellm'))

    def test_a_system_program_is_not_blamed_on_the_venv(self):
        with mock.patch.object(command.os, 'chdir'), mock.patch.object(command.os, 'execvpe',
                                                                      side_effect=FileNotFoundError):
            text = self.advice(lambda: command.run(['journalctl', '-u', 'x']))
        self.assertIn('check: command -v journalctl', text)
        self.assertNotIn('worktree', text)

    def test_the_command_really_prints_it_to_stderr_and_exits_1(self):
        code = "from agentiac import command; command.run(['/nowhere/.venv/bin/python'])"
        done = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                              env=dict(os.environ, PYTHONPATH=str(ROOT / 'src')))
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, '')
        self.assertIn('  next:  follow Bootstrap', done.stderr)


class DefaultProfileTests(Advice):
    def test_a_host_with_no_default_is_sent_to_the_profiles_not_to_the_aliases(self):
        with mock.patch.object(command, 'inventory', return_value={'worker-a': HOST}):
            text = self.advice(lambda: command.profile_for('worker-a', None))
        self.assertEqual(text.splitlines()[0], 'worker-a has no default_profile in infra/inventory.yml; give --model PROFILE')
        self.assertIn('check: ls profiles/', text)
        self.assertIn(f'{errors.command()} claim worker-a', text)

    def test_a_given_or_default_profile_raises_nothing(self):
        self.assertEqual(command.profile_for('worker-a', 'chosen'), 'chosen')
        with mock.patch.object(command, 'inventory', return_value={'worker-b': {'default_profile': 'p'}}):
            self.assertEqual(command.profile_for('worker-b', None), 'p')


class NotPinnedTests(Advice):
    def missing(self, name):
        chosen = adapters.adapter(name)
        return chosen, mock.patch.object(type(chosen), 'installed', return_value=False)

    def test_a_byte_pinned_agent_is_told_how_to_pin(self):
        chosen, absent = self.missing('claude-code')
        with absent:
            text = self.adapter_error(chosen.require_installed)
        self.assertTrue(text.splitlines()[0].startswith('claude-code is not pinned at '))
        self.assertTrue(text.splitlines()[0].endswith('(U4)'))
        self.assertIn(f'check: {errors.command()} agent list', text)
        self.assertIn('scripts/pin-agent.py claude-code --source PATH', text)

    def test_pi_is_not_sent_to_pin_agent_which_refuses_it(self):
        chosen, absent = self.missing('pi')
        with absent:
            text = self.adapter_error(chosen.require_installed)
        self.assertNotIn('pin-agent', text)
        self.assertIn('reinstall from agents/pi/', text)

    def test_a_version_that_differs_is_not_given_as_a_cause(self):
        # This sentence means the install is missing; verify_pin refuses the other case in its own words.
        chosen, absent = self.missing('codex')
        with absent:
            self.assertNotIn('version', self.adapter_error(chosen.require_installed))

    def test_a_probe_keeps_its_own_sentence(self):
        chosen, absent = self.missing('codex')
        with absent:
            text = self.adapter_error(lambda: adapters.probe(chosen, adapters.CAPABILITIES[0], None, None))
        self.assertTrue(text.splitlines()[0].endswith('; nothing was probed'))
        self.assertIn('  next:  ', text)


class SuiteTests(Advice):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('suite_for_errors', ROOT / 'scripts/suite.py')
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def args(self):
        return mock.Mock(agent='pi', loop_guard=False)

    def test_root_is_told_how_to_check_a_unit_since_the_launching_shell_is_not_root(self):
        user = declared.setting('ansible_user', SITE)
        with mock.patch.object(self.cli.os, 'geteuid', return_value=0):
            text = self.advice(lambda: self.cli.run(self.args()))
        self.assertIn('cannot run as root', text.splitlines()[0])
        self.assertIn('systemctl show --property=User UNIT', text)
        self.assertIn(f'--uid={user}', text.splitlines()[-1])

    def test_a_missing_install_is_advice_and_not_a_traceback(self):
        chosen = adapters.adapter('pi')
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(self.cli, 'DATA', Path(directory)), \
                mock.patch.object(self.cli.os, 'geteuid', return_value=1000), \
                mock.patch.object(self.cli.subprocess, 'check_output', return_value='sha256:test\n'), \
                mock.patch.object(self.cli.adapters, 'adapter', return_value=chosen), \
                mock.patch.object(type(chosen), 'installed', return_value=False):
            text = self.advice(lambda: self.cli.run(self.args()))
        self.assertIn('pi is not pinned at', text.splitlines()[0])


class ClaimedTargetTests(Advice):
    def test_a_context_nobody_declared_says_so(self):
        cmd = errors.command()
        text = self.advice(lambda: borrow.claimed_target('worker-a', 'nonexistent-backend', 65536))
        self.assertIn('no single local-coder target for worker-a', text.splitlines()[0])
        self.assertIn('no target in gateway/local-coder.json declares', text)
        self.assertIn(f'check: {cmd} model list', text)
        self.assertNotIn(f'check: {cmd} route', text)

    def test_two_matches_say_it_is_ambiguous(self):
        with mock.patch.object(borrow, 'targets_on', return_value=['a', 'b']), \
                mock.patch.object(borrow.route, 'ROUTES', mock.Mock(read_text=lambda: '{"targets": {"a": {}, "b": {}}}')):
            self.assertIn('2 targets match', self.advice(lambda: borrow.claimed_target('worker-a')))


class FrontTests(Advice):
    def failing(self, stderr='Failed to start transient service unit: Unit already exists.'):
        return subprocess.CompletedProcess([], 1, '', stderr)

    def install(self, host):
        with mock.patch.object(borrow, 'on_host', return_value=self.failing()):
            return self.advice(lambda: borrow.install_front('worker-a', host, '/d'))

    def test_a_failed_front_keeps_what_the_host_said_and_looks_on_that_host(self):
        user = declared.setting('ansible_user', SITE)
        prefix = declared.setting('resource_prefix', SITE)
        text = self.install(HOST)
        self.assertEqual(text.splitlines()[0], 'on-demand front failed to start on worker-a: '
                                               'Failed to start transient service unit: Unit already exists.')
        self.assertIn(f'check: ssh {user}@worker.example systemctl status {prefix}-ondemand', text)
        self.assertIn(f'{errors.command()} ops preflight worker-a', text)

    def test_the_control_gateway_log_is_not_offered_for_a_worker(self):
        self.assertNotIn('ops logs', self.install(HOST))

    def test_a_port_conflict_is_not_offered_because_systemd_run_returns_before_the_bind(self):
        self.assertNotRegex(self.install(HOST), r'\bport\b')

    def test_a_local_host_is_not_told_to_ssh(self):
        user = declared.setting('ansible_user', SITE)
        prefix = declared.setting('resource_prefix', SITE)
        text = self.install({'ansible_connection': 'local'})
        self.assertIn(f'check: systemctl status {prefix}-ondemand', text)
        self.assertNotIn(f'ssh {user}', text)


class WarmTests(Advice):
    def warm(self, code, status):
        with mock.patch.object(borrow, 'ondemand_call', return_value=(code, status)), mock.patch('builtins.print'):
            return self.advice(lambda: borrow.warm('worker-a', HOST))

    def test_a_refused_admission_names_that_cause_and_only_that_one(self):
        text = self.warm(503, {'last_error': 'admission refused: insufficient available host memory'})
        self.assertEqual(text.splitlines()[0], 'worker-a did not load: admission refused: insufficient available host memory')
        self.assertEqual(text.count('cause:'), 1)
        self.assertIn('cause: the host refused the start', text)

    def test_every_sentence_of_the_front_has_its_cause(self):
        prefix = declared.setting('resource_prefix', SITE)
        for error in ('start command exited 1: x', f'{prefix}-spark-tensorfold stopped while loading',
                      'some-other-unit stopped while loading', 'backend not ready before start timeout'):
            self.assertEqual(self.warm(503, {'last_error': error}).count('cause:'), 1, error)

    def test_the_sentences_matched_are_the_ones_the_front_really_raises(self):
        # A reworded LoadError would silently lose its cause, so the two files are held together here.
        front = (ROOT / 'scripts/ondemand-proxy.py').read_text()
        for fragment, _ in borrow.LOAD_FAILURES:
            self.assertIn(fragment, front)

    def test_an_unreachable_front_is_not_blamed_on_the_load(self):
        text = self.warm(None, {'error': 'OSError'})
        self.assertIn('cause: the control tunnel or the front on the host is down', text)
        self.assertEqual(text.count('cause:'), 1)

    def test_a_sentence_nobody_knows_invents_no_cause(self):
        self.assertNotIn('cause:', self.warm(401, {'error': 'unauthorized'}))

    def test_the_memory_reading_is_not_offered_because_it_says_nothing_without_a_metrics_unit(self):
        text = self.warm(503, {'last_error': 'backend not ready before start timeout'})
        self.assertNotIn('ops memory', text)
        self.assertIn(f'check: {errors.command()} status', text)
        self.assertIn(f'{errors.command()} warm worker-a', text)

    def test_a_good_warm_raises_nothing(self):
        with mock.patch.object(borrow, 'ondemand_call', return_value=(200, {'state': 'ready'})), \
                mock.patch('builtins.print'):
            borrow.warm('worker-a', HOST)


if __name__ == '__main__':
    unittest.main()
