"""P2c job ledger tests: explicit acceptance, idempotency, concurrent dequeue, lease expiry,
worker interruption, remote residual, bounded logs and cursor validation.

The ledger database and job logs are redirected to a temporary root; no artifact is written
into the checkout.
"""
import importlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

try:
    from fastapi.testclient import TestClient
    from agentiac import paths
    api = importlib.import_module('agentiac.management.app')
    ledger_module = importlib.import_module('agentiac.operations.ledger')
    dto_module = importlib.import_module('agentiac.operations.dto')
    worker_module = importlib.import_module('agentiac.operations.worker')
    executor_module = importlib.import_module('agentiac.operations.executor')
    inventory_service = importlib.import_module('agentiac.services.inventory')
except ModuleNotFoundError as error:
    if error.name not in {'fastapi', 'starlette', 'httpx', 'pydantic'}:
        raise
    raise unittest.SkipTest(f'job ledger dependencies are not installed: {error}')


class TestJobLedgerAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patch.object(ledger_module, 'DB_PATH', Path(self.tmp.name) / 'operations.sqlite').start()
        patch.object(ledger_module, 'LOG_DIR', Path(self.tmp.name) / 'jobs').start()
        self.addCleanup(patch.stopall)
        patch.object(api, 'SESSION_PATH', Path(self.tmp.name) / 'session.sqlite').start()
        session_module = importlib.import_module('agentiac.management.session')
        session_module.SessionStore(api.SESSION_PATH).initialize('test-owner-password')
        workers = [name for name, facts in inventory_service.nodes(paths.root()).items()
                   if facts.get('ansible_connection') != 'local']
        self.assertGreaterEqual(len(workers), 2)
        self.target, self.other_target = workers[0], workers[1]
        ledger = ledger_module.Ledger()
        ledger.publish_worker('fixture-worker', [self.target, self.other_target])
        ledger.close()
        self.client = TestClient(api.app, base_url='http://127.0.0.1:8000')
        self.origin = {'Origin': 'http://127.0.0.1:8000', 'Content-Type': 'application/json'}

    def tearDown(self):
        self.tmp.cleanup()

    def login(self):
        r = self.client.post('/api/v1/session', json={'password': 'test-owner-password'}, headers=self.origin)
        assert r.status_code == 200, r.text
        return r.json()

    def test_job_accept(self):
        session = self.login()
        headers = {**self.origin, 'X-CSRF-Token': session['csrf_token']}
        r = self.client.post('/api/v1/jobs', headers=headers,
                            json={'idempotency_key': 'job-1', 'operation': 'probe-node',
                                  'target': self.target, 'args': {}, 'timeout_s': 20})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body['status'] == 'queued'
        assert body['job_id']
        assert r.headers['Location'] == f'/api/v1/jobs/{body["job_id"]}'

    def test_job_idempotent(self):
        session = self.login()
        headers = {**self.origin, 'X-CSRF-Token': session['csrf_token']}
        payload = {'idempotency_key': 'job-1', 'operation': 'probe-node',
                   'target': self.target, 'args': {}, 'timeout_s': 20}
        first = self.client.post('/api/v1/jobs', headers=headers, json=payload)
        assert first.status_code == 202, first.text
        second = self.client.post('/api/v1/jobs', headers=headers, json=payload)
        assert second.status_code == 202, second.text
        assert second.json()['job_id'] == first.json()['job_id']

    def test_job_conflict(self):
        session = self.login()
        headers = {**self.origin, 'X-CSRF-Token': session['csrf_token']}
        first = {'idempotency_key': 'job-1', 'operation': 'probe-node',
                 'target': self.target, 'args': {}, 'timeout_s': 20}
        assert self.client.post('/api/v1/jobs', headers=headers, json=first).status_code == 202
        payload = {'idempotency_key': 'job-1', 'operation': 'probe-node',
                   'target': self.other_target, 'args': {}, 'timeout_s': 20}
        r = self.client.post('/api/v1/jobs', headers=headers, json=payload)
        assert r.status_code == 409, r.text
        assert r.json()['code'] == 'conflict'

    def test_job_no_csrf(self):
        session = self.login()
        r = self.client.post('/api/v1/jobs', headers=self.origin,
                            json={'idempotency_key': 'job-2', 'operation': 'probe-node',
                                  'target': self.target, 'args': {}, 'timeout_s': 20})
        assert r.status_code == 403, r.text
        assert r.json()['code'] == 'csrf'

    def test_unknown_target(self):
        session = self.login()
        headers = {**self.origin, 'X-CSRF-Token': session['csrf_token']}
        r = self.client.post('/api/v1/jobs', headers=headers,
                            json={'idempotency_key': 'job-3', 'operation': 'probe-node',
                                  'target': 'nope', 'args': {}, 'timeout_s': 20})
        assert r.status_code == 404, r.text
        assert r.json()['code'] == 'unknown_target'

    def test_jobs_list(self):
        session = self.login()
        r = self.client.get('/api/v1/jobs')
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['schema_version'] == 1
        assert 'jobs' in body

    def test_unknown_job(self):
        session = self.login()
        r = self.client.get('/api/v1/jobs/nope')
        assert r.status_code == 404, r.text
        assert r.json()['code'] == 'unknown_job'

    def test_logs_empty(self):
        session = self.login()
        r = self.client.post('/api/v1/jobs', headers={**self.origin,
                            'X-CSRF-Token': session['csrf_token']},
                            json={'idempotency_key': 'job-4', 'operation': 'probe-node',
                                  'target': self.target, 'args': {}, 'timeout_s': 20})
        job_id = r.json()['job_id']
        r = self.client.get(f'/api/v1/jobs/{job_id}/logs')
        assert r.status_code == 200, r.text
        assert r.json()['lines'] == []

    def test_cancel_queued(self):
        session = self.login()
        headers = {**self.origin, 'X-CSRF-Token': session['csrf_token']}
        r = self.client.post('/api/v1/jobs', headers=headers,
                            json={'idempotency_key': 'job-5', 'operation': 'probe-node',
                                  'target': self.target, 'args': {}, 'timeout_s': 20})
        job_id = r.json()['job_id']
        r = self.client.post(f'/api/v1/jobs/{job_id}/cancel', headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()['status'] == 'cancelled'

    def test_retry_interrupted(self):
        session = self.login()
        ledger = ledger_module.Ledger()
        ledger.accept(dto_module.JobRequestDTO(
            idempotency_key='job-6', operation='probe-node', target=self.target, args={}, timeout_s=20
        ), actor=session['actor'], permission=session['permission'])
        job = ledger.dequeue('worker-1', lease_s=5)
        ledger.finish(job['id'], 'worker-1', job['attempt'], 'failed')
        headers = {**self.origin, 'X-CSRF-Token': session['csrf_token']}
        r = self.client.post(f'/api/v1/jobs/{job["id"]}/retry', headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()['status'] == 'queued'
        assert r.json()['attempt'] == 2

    def test_lease_expiry(self):
        session = self.login()
        ledger = ledger_module.Ledger()
        ledger.accept(dto_module.JobRequestDTO(
            idempotency_key='job-7', operation='probe-node', target=self.target, args={}, timeout_s=20
        ), actor=session['actor'], permission=session['permission'])
        job = ledger.dequeue('worker-2', lease_s=5)
        try:
            ledger.finish(job['id'], 'worker-2', job['attempt'], 'succeeded', now=10**12)
        except Exception as error:
            assert hasattr(error, 'status') and error.status == 409, error
            assert error.code == 'lease_expired'

    def test_concurrent_dequeue(self):
        ledger = ledger_module.Ledger()
        ledger.accept(dto_module.JobRequestDTO(
            idempotency_key='job-8', operation='probe-node', target=self.target, args={}, timeout_s=20
        ), actor='owner', permission='admin')
        first = ledger.dequeue('worker-a', lease_s=5)
        second = ledger.dequeue('worker-b', lease_s=5)
        assert first is not None
        assert second is None

    def test_worker_interruption(self):
        ledger = ledger_module.Ledger()
        ledger.accept(dto_module.JobRequestDTO(
            idempotency_key='job-9', operation='probe-node', target=self.target, args={}, timeout_s=20
        ), actor='owner', permission='admin')
        job = worker_module.run_once(ledger, executor_module.CrashExecutor(), 'worker-3', lease_s=5)
        row = ledger.get(job['id'])
        assert row['status'] == 'needs_attention'

    def test_remote_residual(self):
        ledger = ledger_module.Ledger()
        ledger.accept(dto_module.JobRequestDTO(
            idempotency_key='job-10', operation='probe-node', target=self.target, args={}, timeout_s=20
        ), actor='owner', permission='admin')
        job = worker_module.run_once(ledger, executor_module.ResidualExecutor(), 'worker-4', lease_s=5)
        row = ledger.get(job['id'])
        assert row['status'] == 'needs_attention'
        assert row['result']['residual'] is True

    def test_bounded_log(self):
        ledger = ledger_module.Ledger()
        accepted = ledger.accept(dto_module.JobRequestDTO(
            idempotency_key='job-11', operation='probe-node', target=self.target, args={}, timeout_s=20
        ), actor='owner', permission='admin')
        job_id = accepted['job_id']
        try:
            ledger.append_log(job_id, 'x' * (ledger_module.LOG_MAX_BYTES + 1))
        except Exception as error:
            assert hasattr(error, 'status') and error.status == 503, error
            assert error.code == 'log_full'

    def test_log_sanitized(self):
        ledger = ledger_module.Ledger()
        accepted = ledger.accept(dto_module.JobRequestDTO(
            idempotency_key='job-12', operation='probe-node', target=self.target, args={}, timeout_s=20
        ), actor='owner', permission='admin')
        job_id = accepted['job_id']
        ledger.append_log(job_id, '\x1B[31mred\x00control\x1B[0m')
        lines = ledger.read_logs(job_id)['lines']
        assert lines == ['redcontrol']

    def accept(self, ledger, key='regression', **kwargs):
        request = dto_module.JobRequestDTO(idempotency_key=key, operation='probe-node',
                                          target=self.target, **kwargs)
        return ledger.accept(request, 'owner', 'admin')

    def test_cancel_running_can_finish_and_has_single_event(self):
        ledger = ledger_module.Ledger()
        accepted = self.accept(ledger, timeout_s=7)
        job = ledger.dequeue('worker')
        self.assertEqual(job['timeout_s'], 7)
        ledger.cancel(job['id'], 'admin')
        self.assertIsNone(ledger.get(job['id'])['finished_at'])
        self.assertEqual(len(ledger.read_events(job['id'])['events']), 1)
        self.assertTrue(ledger.renew(job['id'], 'worker', job['attempt']))
        ledger.finish(job['id'], 'worker', job['attempt'], 'succeeded')
        self.assertEqual(ledger.get(accepted['job_id'])['status'], 'succeeded')

    def test_unknown_residual_blocks_retry_cancel_and_other_dequeue(self):
        ledger = ledger_module.Ledger()
        self.accept(ledger, 'first')
        job = worker_module.run_once(ledger, executor_module.ResidualExecutor(), 'worker')
        self.accept(ledger, 'second')
        self.assertIsNone(ledger.dequeue('other-worker'))
        for method in (ledger.retry, ledger.cancel):
            with self.assertRaises(api.ApiError) as caught:
                method(job['id'], 'admin')
            self.assertEqual(caught.exception.status, 409)

    def test_two_workers_cannot_execute_different_jobs_at_once(self):
        ledger = ledger_module.Ledger()
        self.accept(ledger, 'first')
        self.accept(ledger, 'second')
        first = ledger.dequeue('worker', now=100)
        other = ledger_module.Ledger()
        self.addCleanup(other.close)
        self.assertIsNone(other.dequeue('other', now=1000))
        with self.assertRaises(api.ApiError):
            ledger.finish(first['id'], 'worker', first['attempt'], 'succeeded', now=1000)

    def test_retry_clears_previous_result_but_retains_attempt(self):
        ledger = ledger_module.Ledger()
        self.accept(ledger)
        job = ledger.dequeue('worker')
        ledger.finish(job['id'], 'worker', job['attempt'], 'failed', {'reason': 'old'})
        ledger.retry(job['id'], 'admin')
        row = ledger.get(job['id'])
        for key in ('result', 'started_at', 'finished_at'):
            self.assertIsNone(row[key])
        self.assertEqual(ledger.conn.execute('SELECT status FROM attempts').fetchone()[0], 'failed')

    def test_timeout_is_part_of_idempotency_identity(self):
        ledger = ledger_module.Ledger()
        self.accept(ledger, timeout_s=7)
        with self.assertRaises(api.ApiError) as caught:
            self.accept(ledger, timeout_s=8)
        self.assertEqual(caught.exception.status, 409)

    def test_pagination_does_not_skip_equal_timestamps(self):
        ledger = ledger_module.Ledger()
        for i in range(5):
            request = dto_module.JobRequestDTO(idempotency_key=str(i), operation='probe-node', target=self.target)
            ledger.accept(request, 'owner', 'admin', now=100)
        ids, cursor = [], None
        while True:
            page = ledger.list(cursor, limit=2)
            ids.extend(job['id'] for job in page['jobs'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        self.assertEqual(len(set(ids)), 5)
        self.assertEqual(len(ids), 5)

    def test_newest_first_pages_backwards_without_skipping_equal_timestamps(self):
        ledger = ledger_module.Ledger()
        for i, at in enumerate((100, 200, 200, 200, 300)):
            request = dto_module.JobRequestDTO(idempotency_key=str(i), operation='probe-node', target=self.target)
            ledger.accept(request, 'owner', 'admin', now=at)
        first = ledger.list(limit=2, newest_first=True)
        self.assertEqual(first['jobs'][0]['idempotency_key'], '4')
        ids, cursor = [job['id'] for job in first['jobs']], first['next_cursor']
        while cursor is not None:
            page = ledger.list(cursor, limit=2, newest_first=True)
            ids.extend(job['id'] for job in page['jobs'])
            cursor = page['next_cursor']
        self.assertEqual(len(set(ids)), 5)
        self.assertEqual(ids[-1], ledger.list(limit=1)['jobs'][0]['id'])
        self.assertEqual([ledger.get(i)['created_at'] for i in ids],
                         sorted((ledger.get(i)['created_at'] for i in ids), reverse=True))
        ledger.close()

    def test_job_list_api_can_show_the_newest_jobs_first(self):
        ledger = ledger_module.Ledger()
        for i in range(3):
            request = dto_module.JobRequestDTO(idempotency_key=f'order-{i}', operation='probe-node', target=self.target)
            ledger.accept(request, 'owner', 'admin', now=100 + i)
        ledger.close()
        self.login()
        newest = self.client.get('/api/v1/jobs?limit=2&order=desc').json()
        self.assertEqual([job['idempotency_key'] for job in newest['jobs']], ['order-2', 'order-1'])
        self.assertIsNotNone(newest['next_cursor'])
        oldest = self.client.get('/api/v1/jobs?limit=2').json()
        self.assertEqual(oldest['jobs'][0]['idempotency_key'], 'order-0')
        self.assertEqual(self.client.get('/api/v1/jobs?order=sideways').status_code, 422)

    def test_limits_and_empty_log_polling(self):
        ledger = ledger_module.Ledger()
        job_id = self.accept(ledger)['job_id']
        self.assertEqual(ledger.read_logs(job_id, cursor='0')['lines'], [])
        for method in (ledger.list, lambda **kw: ledger.read_logs(job_id, **kw),
                       lambda **kw: ledger.read_events(job_id, **kw)):
            for limit in (-1, 0, 201):
                with self.assertRaises(api.ApiError):
                    method(limit=limit)

    def test_invalid_probe_arguments_and_nonfinite_timeout_are_rejected(self):
        from pydantic import ValidationError
        for timeout in (float('nan'), float('inf'), 0, 31):
            with self.assertRaises(ValidationError):
                dto_module.JobRequestDTO(idempotency_key='bad', operation='probe-node',
                                         target=self.target, timeout_s=timeout)
        ledger = ledger_module.Ledger()
        with self.assertRaises(api.ApiError):
            self.accept(ledger, args={'probe': {'command': 'anything'}})

    def test_live_executor_is_fail_closed(self):
        with self.assertRaises(RuntimeError):
            executor_module.LiveProbeExecutor(Path(self.tmp.name)).run({'operation': 'probe-node'})

    def test_job_api_does_not_disclose_worker_tokens_or_execution_handles(self):
        session = self.login()
        headers = {**self.origin, 'X-CSRF-Token': session['csrf_token']}
        accepted = self.client.post('/api/v1/jobs', headers=headers,
                       json={'idempotency_key': 'private-handle', 'operation': 'probe-node', 'target': self.target})
        row = self.client.get('/api/v1/jobs/' + accepted.json()['job_id']).json()
        for key in ('owner', 'token', 'argv', 'local_handle', 'remote_handle', 'endpoint'):
            self.assertNotIn(key, row)
