"""F05: one mutation lock; a second process is refused, a child of the holder is not, and every hold is journalled."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import mutation  # noqa: E402

# A child process that tries to hold the lock and reports what happened; a flock taken twice in one process
# would succeed, so contention is only real across processes.
CHILD = '''
import sys
from agentiac import mutation
root, operation, wait_s, hold_s = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
try:
    with mutation.hold(root, operation, wait_s=wait_s):
        import time; time.sleep(hold_s)
    print('held')
except mutation.Busy as error:
    print('busy', error.holder.get('operation'))
'''

# `ops deploy` takes the lock and then replaces itself with ansible: the lock has to survive the exec.
EXEC_HOLDER = '''
import os, sys
from agentiac import mutation
extra = mutation.hold_for_exec(sys.argv[1], 'ops deploy')
os.execvpe(sys.executable, [sys.executable, '-c', "print('held', flush=True); import time; time.sleep(1.5)"],
           dict(os.environ, **extra))
'''


class MutationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        self.addCleanup(os.environ.pop, mutation.HELD, None)
        environment = mock.patch.dict(os.environ)  # these tests lock under their own root, and so do their children
        self.addCleanup(environment.stop)
        environment.start()
        os.environ.pop(mutation.STATE_ROOT, None)

    def child_env(self, inherit_held=False):
        env = {name: value for name, value in os.environ.items() if inherit_held or name != mutation.HELD}
        env['PYTHONPATH'] = str(ROOT / 'src')
        return env

    def child(self, operation='child', wait_s=0.0, hold_s=0.0, inherit_held=False, wait=True):
        argv = [sys.executable, '-c', CHILD, self.root, operation, str(wait_s), str(hold_s)]
        if not wait:
            return subprocess.Popen(argv, env=self.child_env(inherit_held), stdout=subprocess.PIPE, text=True)
        return subprocess.run(argv, env=self.child_env(inherit_held), capture_output=True, text=True,
                              timeout=30).stdout.strip()

    def journal(self):
        return [json.loads(line) for line in (Path(self.root) / mutation.JOURNAL).read_text().splitlines()]

    def test_a_second_process_is_refused_and_told_who_holds_the_lock(self):
        with mutation.hold(self.root, 'claim worker-a'):
            self.assertEqual(self.child(), 'busy claim worker-a')

    def test_a_child_of_the_holder_does_not_take_the_lock_again(self):
        with mutation.hold(self.root, 'claim worker-a'):
            self.assertEqual(self.child(inherit_held=True), 'held')

    def test_after_the_hold_the_environment_is_clean_and_the_lock_is_free(self):
        with mutation.hold(self.root, 'first'):
            self.assertTrue(os.environ.get(mutation.HELD))
        self.assertNotIn(mutation.HELD, os.environ)
        with mutation.hold(self.root, 'second'):
            pass
        self.assertEqual(self.child(), 'held')

    def test_an_exception_propagates_is_journalled_as_failed_and_releases_the_lock(self):
        with self.assertRaises(RuntimeError):
            with mutation.hold(self.root, 'route set x'):
                raise RuntimeError('boom')
        self.assertEqual(self.journal()[-1]['event'], 'end')
        self.assertIs(self.journal()[-1]['ok'], False)
        self.assertNotIn(mutation.HELD, os.environ)
        self.assertEqual(self.child(), 'held')

    def test_the_journal_has_one_start_and_one_end_per_hold(self):
        for operation in ('claim worker-b', 'release worker-b'):
            with mutation.hold(self.root, operation):
                pass
        records = self.journal()
        self.assertEqual([(r['operation'], r['event']) for r in records],
                         [('claim worker-b', 'start'), ('claim worker-b', 'end'),
                          ('release worker-b', 'start'), ('release worker-b', 'end')])
        self.assertTrue(all(r['ok'] is True for r in records if r['event'] == 'end'))

    def test_busy_carries_the_first_holders_operation(self):
        holder = self.child('ops deploy', hold_s=1.5, wait=False)
        self.addCleanup(holder.wait)
        deadline = time.monotonic() + 10
        while not (Path(self.root) / mutation.LOCK).exists() or b'ops deploy' not in (Path(self.root) / mutation.LOCK).read_bytes():
            self.assertLess(time.monotonic(), deadline, 'the child never took the lock')
            time.sleep(0.05)
        with self.assertRaises(mutation.Busy) as caught:
            with mutation.hold(self.root, 'claim worker-a'):
                pass
        self.assertEqual(caught.exception.holder['operation'], 'ops deploy')
        self.assertIn('ops deploy', str(caught.exception))

    def test_the_actor_is_recorded_in_the_journal_and_the_lock_file(self):
        self.assertEqual(mutation.actor({}), 'operator')
        self.assertEqual(mutation.actor({'AGENTIAC_ACTOR': 'x'}), 'x')
        with mutation.hold(self.root, 'claim worker-a', env={'AGENTIAC_ACTOR': 'x'}):
            self.assertEqual(json.loads((Path(self.root) / mutation.LOCK).read_text())['actor'], 'x')
        self.assertEqual(self.journal()[0]['actor'], 'x')

    def test_a_waiting_hold_succeeds_when_the_holder_releases(self):
        holder = self.child('reclaim', hold_s=0.3, wait=False)
        self.addCleanup(holder.wait)
        deadline = time.monotonic() + 10
        while b'reclaim' not in ((Path(self.root) / mutation.LOCK).read_bytes()
                                 if (Path(self.root) / mutation.LOCK).exists() else b''):
            self.assertLess(time.monotonic(), deadline, 'the child never took the lock')
            time.sleep(0.02)
        with mutation.hold(self.root, 'recover', wait_s=3.0):
            pass
        self.assertEqual(holder.communicate(timeout=10)[0].strip(), 'held')

    def test_hold_or_exit_names_the_holder_in_the_message(self):
        holder = self.child('ops deploy', hold_s=1.5, wait=False)
        self.addCleanup(holder.wait)
        deadline = time.monotonic() + 10
        while b'ops deploy' not in ((Path(self.root) / mutation.LOCK).read_bytes()
                                    if (Path(self.root) / mutation.LOCK).exists() else b''):
            self.assertLess(time.monotonic(), deadline, 'the child never took the lock')
            time.sleep(0.05)
        with self.assertRaises(SystemExit) as caught:
            with mutation.hold_or_exit(self.root, 'claim worker-a'):
                pass
        self.assertIn('ops deploy', str(caught.exception.code))
        self.assertIn('run this command again', str(caught.exception.code))

    def test_a_lock_taken_for_exec_lasts_until_the_execed_program_exits(self):
        holder = subprocess.Popen([sys.executable, '-c', EXEC_HOLDER, self.root], env=self.child_env(),
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.assertEqual(holder.stdout.readline().strip(), 'held')  # printed by the program it exec'd into
        with self.assertRaises(mutation.Busy) as caught:
            with mutation.hold(self.root, 'x'):
                pass
        self.assertEqual(caught.exception.holder['operation'], 'ops deploy')
        self.assertEqual(self.child(), 'busy ops deploy')
        holder.wait(timeout=10)
        holder.stdout.close()
        with mutation.hold(self.root, 'x'):  # the kernel released the lock when the program exited
            pass
        deploy = [record['event'] for record in self.journal() if record['operation'] == 'ops deploy']
        self.assertEqual(deploy, ['start'])

    def test_a_process_started_by_a_holder_takes_no_lock_for_exec(self):
        with mutation.hold(self.root, 'claim worker-a'):
            self.assertEqual(mutation.hold_for_exec(self.root, 'ops deploy'), {})
        self.assertEqual([r['operation'] for r in self.journal()], ['claim worker-a', 'claim worker-a'])

    def test_the_state_root_is_the_given_root_unless_the_variable_names_another(self):
        self.assertEqual(mutation.state_root('/x', {}), Path('/x'))
        self.assertEqual(mutation.state_root('/x', {mutation.STATE_ROOT: ''}), Path('/x'))
        self.assertEqual(mutation.state_root('/x', {mutation.STATE_ROOT: '/y'}), Path('/y'))

    def test_a_redirected_hold_locks_and_journals_under_the_override_only(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            os.environ[mutation.STATE_ROOT] = elsewhere
            with mutation.hold(self.root, 'claim worker-a'):
                self.assertEqual(json.loads((Path(elsewhere) / mutation.LOCK).read_text())['operation'], 'claim worker-a')
                self.assertEqual(self.child(), 'busy claim worker-a')  # the child inherits the variable: same lock
            records = [json.loads(line) for line in (Path(elsewhere) / mutation.JOURNAL).read_text().splitlines()]
            self.assertEqual([r['event'] for r in records], ['start', 'end'])
            self.assertEqual(os.listdir(self.root), [])


if __name__ == '__main__':
    unittest.main()
