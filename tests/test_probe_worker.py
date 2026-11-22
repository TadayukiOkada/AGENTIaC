"""Real disposable process tests for the minimal probe, crash windows and recovery.

All nodes are temporary local fixtures. No test contacts SSH or a shared worker.
"""
import importlib
import json
import os
from pathlib import Path
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    from pydantic import BaseModel
except ModuleNotFoundError:
    raise unittest.SkipTest('optional dashboard dependencies are not installed')

from agentiac.management.errors import ApiError
from agentiac.operations.dto import JobRequestDTO
from agentiac.operations.ledger import Ledger
from agentiac.operations import probe, worker

REPO = Path(__file__).resolve().parents[1]
SLOW_CODE = probe.PROBE_CODE.replace('    memory = {}', '    time.sleep(10)\n    memory = {}')


class ProbeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'infra').mkdir()
        (self.root / 'infra/inventory.yml').write_text(
            'all:\n  vars:\n    ansible_user: fixture\n  children:\n    workers:\n      hosts:\n'
            '        fixture:\n          ansible_connection: local\n'
            '        other:\n          ansible_connection: local\n'
            '        sshfixture:\n          ansible_host: fixture.invalid\n          ansible_port: 2222\n')
        self.ledger = Ledger(self.root)
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=3)
        # Reap only our recorded disposable probe groups, even if an assertion failed.
        for row in self.ledger.conn.execute('SELECT local_handle, argv, remote_handle FROM executions').fetchall():
            handles = [json.loads(row[0])] if row[0] else probe.find_local(json.loads(row[1]))
            if row[2]:
                handles.append(json.loads(row[2]))
            for handle in handles:
                probe.stop_group(handle)
        self.ledger.close()
        self.temp.cleanup()

    def accept(self, key='test', **kwargs):
        return self.ledger.accept(JobRequestDTO(idempotency_key=key, operation='probe-node',
                                  target=kwargs.pop('target', 'fixture'), **kwargs), 'owner', 'admin')

    def child_worker(self, crash=None):
        script = '''
import os, signal, sys
from pathlib import Path
from agentiac.operations import probe, worker
from agentiac.operations.ledger import Ledger
root=Path(sys.argv[1]); ledger=Ledger(root)
probe.PROBE_CODE = probe.PROBE_CODE.replace('    memory = {}', '    time.sleep(10)\\n    memory = {}')
original = ledger.record_execution
def record(job, **kwargs):
    if sys.argv[2] == 'before_pid' and 'local_handle' in kwargs:
        os.kill(os.getpid(), signal.SIGKILL)
    original(job, **kwargs)
    if sys.argv[2] == 'before_go' and kwargs.get('authorized'):
        os.kill(os.getpid(), signal.SIGKILL)
ledger.record_execution = record
worker.run_once(ledger, probe.LiveProbeExecutor(root), 'child-worker')
ledger.close()
'''
        child = subprocess.Popen([sys.executable, '-c', script, str(self.root), crash or 'none'],
                                 cwd=REPO, env={**os.environ, 'PYTHONPATH': str(REPO / 'src')},
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(child)
        return child

    def until(self, predicate, child=None):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            if child is not None and child.poll() is not None:
                self.fail('worker exited unexpectedly: ' + child.stderr.read().decode())
            time.sleep(0.02)
        self.fail('fixture did not reach the expected crash point')

    def test_real_minimal_probe_has_allowlisted_result_and_separate_snapshot(self):
        accepted = self.accept()
        job = worker.run_once(self.ledger, probe.LiveProbeExecutor(self.root), 'worker')
        row = self.ledger.get(accepted['job_id'])
        self.assertEqual(row['status'], 'succeeded')
        self.assertTrue(row['result']['stopped'])
        self.assertTrue(probe.valid_data(row['result']['probe']))
        self.assertNotIn('hw_fp', row['result'])
        self.assertFalse((self.root / 'artifacts/inventory').exists())
        snapshot = self.ledger.probe_snapshot('fixture')
        self.assertEqual(snapshot['quality'], 'fresh')
        self.assertEqual(snapshot['job_id'], job['id'])
        self.assertTrue(self.ledger.execution(job['id'], 1)['authorized'])

    def test_ssh_argv_honors_declared_user_port_and_checks_host_keys(self):
        endpoint = probe.endpoint_for(self.root, 'sshfixture')
        argv = probe.argv_for(endpoint, 'a' * 32)
        self.assertEqual(argv[argv.index('-l') + 1], 'fixture')
        self.assertEqual(argv[argv.index('-p') + 1], '2222')
        for option in ('StrictHostKeyChecking=yes', 'BatchMode=yes', 'ClearAllForwardings=yes',
                       'ControlPath=none', 'ConnectionAttempts=1'):
            self.assertIn(option, argv)
        self.assertEqual(argv[argv.index('-F') + 1], '/dev/null')
        remote = shlex.split(argv[-1])
        self.assertEqual(remote[:4], ['/usr/bin/python3', '-I', '-u', '-c'])
        self.assertEqual(remote[-3:], [probe.probe_remote.MARKER, 'run', 'a' * 32])

    def test_invalid_connection_inventory_is_rejected_before_launch(self):
        for spec in ({'ansible_host': '-oProxyCommand=bad', 'ansible_user': 'user'},
                     {'ansible_host': 'host; echo bad', 'ansible_user': 'user'},
                     {'ansible_host': 'host', 'ansible_user': 'user; echo bad'},
                     {'ansible_host': 'host', 'ansible_user': 'user', 'ansible_port': True},
                     {'ansible_connection': 'arbitrary-plugin'}):
            with patch.object(probe, 'nodes', return_value={'fixture': spec}), self.assertRaises(ValueError):
                probe.endpoint_for(self.root, 'fixture')

    def test_disk_full_before_intent_never_spawns_a_process(self):
        self.accept()
        with patch.object(self.ledger, 'prepare_execution', side_effect=sqlite3.OperationalError('disk full')), \
                patch.object(probe.subprocess, 'Popen') as spawn:
            worker.run_once(self.ledger, probe.LiveProbeExecutor(self.root), 'worker')
        spawn.assert_not_called()
        self.assertEqual(self.ledger.unresolved()[0]['status'], 'needs_attention')
        worker.reconcile(self.ledger, probe.LiveProbeExecutor(self.root))
        self.assertFalse(self.ledger.unresolved())

    def test_pid_receipt_write_failure_stops_child_and_retains_intent(self):
        accepted = self.accept()
        with patch.object(self.ledger, 'record_execution', side_effect=sqlite3.OperationalError('disk full')):
            worker.run_once(self.ledger, probe.LiveProbeExecutor(self.root), 'worker')
        self.assertIsNotNone(self.ledger.execution(accepted['job_id'], 1))
        result = worker.reconcile(self.ledger, probe.LiveProbeExecutor(self.root))
        self.assertEqual(result[0]['status'], 'interrupted')

    def test_crash_before_pid_receipt_is_discovered_from_durable_argv(self):
        accepted = self.accept()
        child = self.child_worker('before_pid')
        child.wait(timeout=5)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        execution = self.ledger.execution(accepted['job_id'], 1)
        self.assertIsNone(execution['local_handle'])
        result = worker.reconcile(self.ledger, probe.LiveProbeExecutor(self.root), stop=True)
        self.assertEqual(result[0]['status'], 'interrupted')
        self.assertFalse(probe.find_local(execution['argv']))

    def test_inspect_only_reconcile_never_signals_a_local_probe(self):
        accepted = self.accept()
        child = self.child_worker()
        self.until(lambda: bool((self.ledger.execution(accepted['job_id'], 1) or {}).get('authorized')), child)
        child.kill()
        child.wait(timeout=3)
        execution = self.ledger.execution(accepted['job_id'], 1)
        # For a local target the local process is the probe; without --stop-probe it stays running.
        report = worker.reconcile(self.ledger, probe.LiveProbeExecutor(self.root))
        self.assertEqual(report[0]['status'], 'needs_attention')
        self.assertTrue(probe.handle_alive(execution['local_handle']))
        report = worker.reconcile(self.ledger, probe.LiveProbeExecutor(self.root), stop=True)
        self.assertEqual(report[0]['status'], 'interrupted')
        self.assertFalse(probe.handle_alive(execution['local_handle']))

    def test_crash_after_handle_before_go_does_not_replay_execution(self):
        accepted = self.accept()
        child = self.child_worker('before_go')
        child.wait(timeout=5)
        execution = self.ledger.execution(accepted['job_id'], 1)
        self.assertTrue(execution['authorized'])
        self.assertIsNotNone(execution['remote_handle'])
        result = worker.reconcile(self.ledger, probe.LiveProbeExecutor(self.root), stop=True)
        self.assertEqual(result[0]['status'], 'interrupted')
        self.assertEqual(self.ledger.get(accepted['job_id'])['attempt'], 1)
        self.assertIsNone(self.ledger.probe_snapshot('fixture'))

    def test_worker_sigkill_recovers_running_probe_and_requires_explicit_retry(self):
        accepted = self.accept()
        child = self.child_worker()
        self.until(lambda: bool((self.ledger.execution(accepted['job_id'], 1) or {}).get('authorized')), child)
        with self.assertRaises(ApiError):
            worker.reconcile(self.ledger, probe.LiveProbeExecutor(self.root))
        child.kill()
        child.wait(timeout=3)
        row = self.ledger.get(accepted['job_id'])
        self.assertEqual(row['status'], 'running')
        worker.reconcile(self.ledger, probe.LiveProbeExecutor(self.root), stop=True)
        self.assertEqual(self.ledger.get(accepted['job_id'])['status'], 'interrupted')
        with self.assertRaises(ApiError):
            self.ledger.finish(accepted['job_id'], 'child-worker', 1, 'succeeded')
        self.ledger.retry(accepted['job_id'], 'admin')
        worker.run_once(self.ledger, probe.LiveProbeExecutor(self.root), 'new-worker')
        self.assertEqual(self.ledger.get(accepted['job_id'])['status'], 'succeeded')
        self.assertEqual(self.ledger.get(accepted['job_id'])['attempt'], 2)
        self.assertEqual(self.ledger.conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 2)

    def test_cancel_running_verifies_owned_process_exit(self):
        accepted = self.accept()
        child = self.child_worker()
        self.until(lambda: bool((self.ledger.execution(accepted['job_id'], 1) or {}).get('authorized')), child)
        self.ledger.cancel(accepted['job_id'], 'admin')
        child.wait(timeout=5)
        row = self.ledger.get(accepted['job_id'])
        self.assertEqual(row['status'], 'cancelled', row)
        self.assertTrue(row['result']['stopped'])
        self.assertFalse(probe.group_alive(self.ledger.execution(row['id'], 1)['local_handle']['pid']))

    def test_timeout_has_a_bounded_cleanup_and_preserves_previous_snapshot(self):
        self.accept('first')
        worker.run_once(self.ledger, probe.LiveProbeExecutor(self.root), 'worker')
        previous = self.ledger.probe_snapshot('fixture')
        accepted = self.accept('second', timeout_s=1)
        start = time.monotonic()
        with patch.object(probe, 'PROBE_CODE', SLOW_CODE):
            worker.run_once(self.ledger, probe.LiveProbeExecutor(self.root), 'worker')
        row = self.ledger.get(accepted['job_id'])
        self.assertEqual(row['status'], 'failed', row)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(self.ledger.probe_snapshot('fixture')['job_id'], previous['job_id'])

    def test_unverifiable_remote_absence_remains_blocking(self):
        accepted = self.accept(timeout_s=1)

        def unreachable_inspector(endpoint, token, mode='run', settings=None):
            return ['/bin/false'] if mode == 'inspect' else probe.argv_for(endpoint, token)

        with patch.object(probe, 'PROBE_CODE', SLOW_CODE):
            worker.run_once(self.ledger, probe.LiveProbeExecutor(self.root, argv_builder=unreachable_inspector), 'worker')
        self.assertEqual(self.ledger.get(accepted['job_id'])['status'], 'needs_attention')
        with self.assertRaises(ApiError):
            self.ledger.retry(accepted['job_id'], 'admin')
        worker.reconcile(self.ledger, probe.LiveProbeExecutor(self.root))
        self.assertEqual(self.ledger.get(accepted['job_id'])['status'], 'interrupted')

    def test_excess_output_and_nonallowlisted_results_never_reach_browser(self):
        for index, code in enumerate((probe.PROBE_CODE.replace('    memory = {}',
                              "    print('SECRET_SENTINEL' * 3000, flush=True)\n    memory = {}"),
                              probe.PROBE_CODE.replace("'source': 'linux-proc-v1',",
                                                       "'source': 'linux-proc-v1', 'secret': 'SECRET_SENTINEL',"))):
            accepted = self.accept(str(index))
            with patch.object(probe, 'PROBE_CODE', code):
                worker.run_once(self.ledger, probe.LiveProbeExecutor(self.root), 'worker')
            row = self.ledger.get(accepted['job_id'])
            self.assertEqual(row['status'], 'failed', row)
            self.assertNotIn('SECRET_SENTINEL', json.dumps(row))
            self.assertNotIn('SECRET_SENTINEL', json.dumps(self.ledger.read_logs(row['id'])))

    def test_pid_reuse_never_signals_the_new_process(self):
        stale = probe.local_identity(os.getpid())
        stale['start_ticks'] = str(int(stale['start_ticks']) - 1)
        with patch.object(probe.os, 'killpg') as kill:
            self.assertTrue(probe.stop_group(stale))
        kill.assert_not_called()

    def test_legacy_running_attempt_without_protocol_receipt_is_not_auto_recovered(self):
        accepted = self.accept()
        self.ledger.dequeue('legacy-worker')
        self.ledger.conn.execute('DELETE FROM probe_attempt_receipts')
        self.ledger.conn.commit()
        result = worker.reconcile(self.ledger, probe.LiveProbeExecutor(self.root))
        self.assertEqual(result[0]['status'], 'needs_attention')
        self.assertEqual(self.ledger.get(accepted['job_id'])['status'], 'needs_attention')

    def test_readiness_requires_live_matching_worker_and_explicit_target(self):
        request = JobRequestDTO(idempotency_key='ready', operation='probe-node', target='fixture')
        with self.assertRaises(ApiError) as caught:
            self.ledger.accept(request, 'owner', 'admin', require_worker=True)
        self.assertEqual(caught.exception.status, 503)
        self.ledger.publish_worker('worker', ['other'])
        with self.assertRaises(ApiError) as caught:
            self.ledger.accept(request, 'owner', 'admin', require_worker=True)
        self.assertEqual(caught.exception.status, 403)
        self.ledger.publish_worker('worker', ['fixture'])
        self.ledger.accept(request, 'owner', 'admin', require_worker=True)
        self.assertEqual(self.ledger.available_targets(), ['fixture'])
        self.ledger.conn.execute("UPDATE probe_workers SET implementation='old-code'")
        self.ledger.conn.commit()
        self.assertFalse(self.ledger.available_targets())

    def test_worker_target_filter_and_cooldown_apply_to_retry_attempts(self):
        accepted = self.accept()
        self.assertIsNone(self.ledger.dequeue('worker', targets=['other']))
        worker.run_once(self.ledger, probe.LiveProbeExecutor(self.root), 'worker', targets=['fixture'])
        self.accept('next')
        self.assertIsNone(self.ledger.dequeue('worker', targets=['fixture']))

    def test_expired_lease_cannot_authorize_a_probe(self):
        self.accept()
        job = self.ledger.dequeue('worker', lease_s=0.01)
        time.sleep(0.02)
        with patch.object(probe.subprocess, 'Popen') as spawn:
            with self.assertRaises(ApiError):
                probe.LiveProbeExecutor(self.root, self.ledger).run(job)
        spawn.assert_not_called()

    def test_two_worker_processes_cannot_reconcile_or_execute_concurrently(self):
        with worker.WorkerLock(self.ledger):
            with self.assertRaises(ApiError) as caught:
                worker.run_once(self.ledger, probe.LiveProbeExecutor(self.root), 'second')
            self.assertEqual(caught.exception.code, 'worker_busy')


    def test_fake_ssh_remote_survives_transport_loss_until_owned_reconciliation(self):
        fake = self.root / 'fake-ssh'
        fake.write_text('#!' + sys.executable + '\n' +
                        'import shlex, subprocess, sys\n'
                        'child=subprocess.Popen(shlex.split(sys.argv[-1]), stdin=sys.stdin, '
                        'stdout=sys.stdout, stderr=sys.stderr, start_new_session=True)\n'
                        'raise SystemExit(child.wait())\n')
        fake.chmod(0o700)

        def fake_ssh(endpoint, token, mode='run', settings=None):
            argv = probe.argv_for(endpoint, token, mode, settings)
            return [str(fake), *argv[1:]]

        def disconnected(endpoint, token, mode='run', settings=None):
            return ['/bin/false'] if mode == 'inspect' else fake_ssh(endpoint, token, mode, settings)

        accepted = self.accept(target='sshfixture', timeout_s=1)
        with patch.object(probe, 'PROBE_CODE', SLOW_CODE):
            worker.run_once(self.ledger, probe.LiveProbeExecutor(self.root, argv_builder=disconnected), 'worker')
        self.assertEqual(self.ledger.get(accepted['job_id'])['status'], 'needs_attention')
        execution = self.ledger.execution(accepted['job_id'], 1)
        self.assertNotEqual(execution['local_handle']['pid'], execution['remote_handle']['pid'])
        executor = probe.LiveProbeExecutor(self.root, argv_builder=fake_ssh)
        report = worker.reconcile(self.ledger, executor)
        self.assertEqual(report[0]['status'], 'needs_attention')
        report = worker.reconcile(self.ledger, executor, stop=True)
        self.assertEqual(report[0]['status'], 'interrupted')

    def test_receipt_mismatch_never_grants_go(self):
        self.accept()
        job = self.ledger.dequeue('worker')
        with self.assertRaises(ApiError):
            self.ledger.record_execution(job, remote_handle=probe.local_identity(os.getpid()), authorized=True)

    def test_disk_failure_when_finishing_keeps_an_unresolved_attempt_for_recovery(self):
        accepted = self.accept()
        with patch.object(self.ledger, 'finish', side_effect=sqlite3.OperationalError('disk full')):
            worker.run_once(self.ledger, probe.LiveProbeExecutor(self.root), 'worker')
        self.assertEqual(self.ledger.get(accepted['job_id'])['status'], 'needs_attention')
        worker.reconcile(self.ledger, probe.LiveProbeExecutor(self.root))
        self.assertEqual(self.ledger.get(accepted['job_id'])['status'], 'interrupted')

    def test_readiness_expires_and_replayed_acceptance_is_still_idempotent(self):
        request = JobRequestDTO(idempotency_key='readiness', operation='probe-node', target='fixture')
        self.ledger.publish_worker('worker', ['fixture'])
        first = self.ledger.accept(request, 'owner', 'admin', require_worker=True)
        self.ledger.unpublish_worker('worker')
        replay = self.ledger.accept(request, 'owner', 'admin', require_worker=True)
        self.assertEqual(first['job_id'], replay['job_id'])
        self.ledger.publish_worker('worker', ['fixture'], now=time.time() - 30)
        self.assertFalse(self.ledger.available_targets())

    def test_cancel_intent_survives_needs_attention_recovery(self):
        self.accept()
        job = self.ledger.dequeue('worker')
        self.ledger.cancel(job['id'], 'admin')
        self.ledger.attention(job)
        worker.reconcile(self.ledger, probe.LiveProbeExecutor(self.root))
        self.assertEqual(self.ledger.get(job['id'])['status'], 'cancelled')


    def test_unknown_reconciliation_job_is_reported_and_legacy_failed_cannot_retry(self):
        with self.assertRaises(ApiError) as caught:
            worker.reconcile(self.ledger, probe.LiveProbeExecutor(self.root), job_id='missing')
        self.assertEqual(caught.exception.status, 404)
        self.accept()
        job = self.ledger.dequeue('legacy')
        self.ledger.finish(job['id'], 'legacy', 1, 'failed')
        self.ledger.conn.execute('DELETE FROM probe_attempt_receipts')
        self.ledger.conn.commit()
        with self.assertRaises(ApiError) as caught:
            self.ledger.retry(job['id'], 'admin')
        self.assertEqual(caught.exception.code, 'reconciliation_required')

    def test_worker_cli_requires_explicit_scope_without_creating_state(self):
        with self.assertRaises(SystemExit):
            worker.main(['--serve'])
        with self.assertRaises(SystemExit):
            worker.main(['--serve', '--target', 'fixture', '--stop-probe'])
        with self.assertRaises(SystemExit):
            worker.main(['--reconcile', 'job', '--target', 'fixture'])
