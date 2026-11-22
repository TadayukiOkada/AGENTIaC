"""First-release quality gates for the management API: route/permission contract, error shape and load.

Every `/api/v1` route must appear in ROUTES with the permission it requires, so a new endpoint without a
deliberate permission decision fails here. Sessions are downgraded in the session database to exercise
viewer and operator permissions (only the owner can log in today). Reads use the checkout's real
declarations; ledger/session state is temporary.
"""
import importlib
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from agentiac import declared, paths  # noqa: E402
NODE_ID = next(iter(declared.load(paths.root()).nodes))

try:
    from fastapi.routing import APIRoute
    from fastapi.testclient import TestClient
    api = importlib.import_module('agentiac.management.app')
    session_module = importlib.import_module('agentiac.management.session')
except ModuleNotFoundError as error:
    if error.name not in {'fastapi', 'starlette', 'httpx', 'pydantic'}:
        raise
    raise unittest.SkipTest(f'management API dependencies are not installed: {error}')

ORIGIN = 'http://127.0.0.1:8000'
JOB = '0' * 32
# (method, route path) -> (required permission, concrete path for the test). 'public' needs no session.
ROUTES = {
    ('GET', '/api/v1/capabilities'): ('viewer', '/api/v1/capabilities'),
    ('GET', '/api/v1/overview'): ('viewer', '/api/v1/overview'),
    ('GET', '/api/v1/nodes'): ('viewer', '/api/v1/nodes'),
    ('GET', '/api/v1/nodes/{node_id}'): ('viewer', f'/api/v1/nodes/{NODE_ID}'),
    ('GET', '/api/v1/models'): ('viewer', '/api/v1/models'),
    ('GET', '/api/v1/models/{target}'): ('viewer', '/api/v1/models/not-a-target'),
    ('GET', '/api/v1/engines'): ('viewer', '/api/v1/engines'),
    ('GET', '/api/v1/engines/{engine_id}'): ('viewer', '/api/v1/engines/not-an-engine'),
    ('GET', '/api/v1/deployments'): ('viewer', '/api/v1/deployments'),
    ('GET', '/api/v1/deployments/{deployment_id}'): ('viewer', f'/api/v1/deployments/{JOB}'),
    ('GET', '/api/v1/agents'): ('viewer', '/api/v1/agents'),
    ('GET', '/api/v1/agents/{agent_id}'): ('viewer', '/api/v1/agents/pi'),
    ('GET', '/api/v1/configuration/resources'): ('viewer', '/api/v1/configuration/resources'),
    ('GET', '/api/v1/configuration/resources/{resource_id:path}'): ('viewer', '/api/v1/configuration/resources/agent/pi'),
    ('GET', '/api/v1/related/{kind}/{resource_id:path}'): ('viewer', f'/api/v1/related/nodes/{NODE_ID}'),
    ('GET', '/api/v1/configuration/history'): ('viewer', '/api/v1/configuration/history'),
    ('GET', '/api/v1/jobs'): ('viewer', '/api/v1/jobs'),
    ('GET', '/api/v1/jobs/{job_id}'): ('viewer', f'/api/v1/jobs/{JOB}'),
    ('GET', '/api/v1/jobs/{job_id}/logs'): ('viewer', f'/api/v1/jobs/{JOB}/logs'),
    ('GET', '/api/v1/jobs/{job_id}/events'): ('viewer', f'/api/v1/jobs/{JOB}/events'),
    ('POST', '/api/v1/jobs'): ('operator', '/api/v1/jobs'),
    ('POST', '/api/v1/jobs/{job_id}/cancel'): ('operator', f'/api/v1/jobs/{JOB}/cancel'),
    ('POST', '/api/v1/jobs/{job_id}/retry'): ('operator', f'/api/v1/jobs/{JOB}/retry'),
    ('GET', '/api/v1/session'): ('session', '/api/v1/session'),
    ('POST', '/api/v1/session'): ('public', '/api/v1/session'),
    ('DELETE', '/api/v1/session'): ('session', '/api/v1/session'),
}
JOB_BODY = {'idempotency_key': 'contract', 'operation': 'probe-node', 'target': NODE_ID, 'args': {}, 'timeout_s': 15}
ERROR_FIELDS = {'code', 'message', 'request_id', 'retryable', 'details'}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        ledger = importlib.import_module('agentiac.operations.ledger')
        patch.object(ledger, 'DB_PATH', Path(self.tmp.name) / 'operations.sqlite').start()
        patch.object(ledger, 'LOG_DIR', Path(self.tmp.name) / 'jobs').start()
        patch.object(api, 'SESSION_PATH', Path(self.tmp.name) / 'session.sqlite').start()
        session_module.SessionStore(api.SESSION_PATH).initialize('test-owner-password')
        api.SNAPSHOT_CACHE.clear()
        self.addCleanup(api.SNAPSHOT_CACHE.clear)

    def client(self, permission=None):
        """A client logged in as the owner, then downgraded to `permission` in the session database."""
        client = TestClient(api.app, base_url=ORIGIN)
        if permission is None:
            return client, None
        r = client.post('/api/v1/session', json={'password': 'test-owner-password'}, headers={'Origin': ORIGIN})
        self.assertEqual(r.status_code, 200, r.text)
        session = r.json()
        with sqlite3.connect(api.SESSION_PATH) as conn:
            conn.execute('UPDATE sessions SET permission=? WHERE csrf_token=?', (permission, session['csrf_token']))
        return client, session

    def call(self, client, method, path, csrf=None, origin=True):
        headers = {'Origin': ORIGIN} if origin else {}
        if csrf:
            headers['X-CSRF-Token'] = csrf
        if method == 'POST':
            body = {'password': 'wrong-password'} if path == '/api/v1/session' else (
                JOB_BODY if path == '/api/v1/jobs' else {'confirm': True})
            return client.post(path, json=body, headers=headers)
        return client.request(method, path, headers=headers)


class RouteContractTest(Base):
    def test_every_api_route_has_a_declared_permission(self):
        routes = {(method, route.path) for route in api.app.routes if isinstance(route, APIRoute)
                  and route.path.startswith('/api/') for method in route.methods - {'HEAD'}}
        self.assertEqual(routes - set(ROUTES), set(), 'new route: decide and declare its permission here')
        self.assertEqual(set(ROUTES) - routes, set(), 'declared route no longer exists')

    def test_no_session_is_refused_everywhere_but_login(self):
        client, _ = self.client()
        for (method, _), (permission, path) in ROUTES.items():
            r = self.call(client, method, path)
            if permission == 'public':
                self.assertEqual(r.status_code, 401, (method, path))  # wrong password, not a missing session
                self.assertEqual(r.json()['code'], 'bad_credential')
                continue
            self.assertEqual((r.status_code, r.json()['code']), (401, 'unauthorized'), (method, path))

    def test_viewer_reads_and_is_refused_operations(self):
        client, session = self.client('viewer')
        for (method, _), (permission, path) in ROUTES.items():
            if permission == 'public' or method == 'DELETE':
                continue
            r = self.call(client, method, path, csrf=session['csrf_token'])
            if permission == 'operator':
                self.assertEqual((r.status_code, r.json()['code']), (403, 'permission'), (method, path))
            else:
                self.assertNotIn(r.status_code, (401, 403), (method, path, r.text))
                self.assertLess(r.status_code, 500, (method, path, r.text))

    def test_operator_operations_need_the_csrf_token_and_same_origin(self):
        client, session = self.client('operator')
        for (method, _), (permission, path) in ROUTES.items():
            if permission != 'operator':
                continue
            r = self.call(client, method, path)
            self.assertEqual((r.status_code, r.json()['code']), (403, 'csrf'), (method, path))
            r = self.call(client, method, path, csrf='not-the-token')
            self.assertEqual((r.status_code, r.json()['code']), (403, 'csrf'), (method, path))
            r = self.call(client, method, path, csrf=session['csrf_token'], origin=False)
            self.assertEqual((r.status_code, r.json()['code']), (403, 'origin_not_allowed'), (method, path))
            r = self.call(client, method, path, csrf=session['csrf_token'])
            self.assertNotIn(r.status_code, (401, 403), (method, path, r.text))
            # Past the gates: a domain answer (404 unknown job, 409, ...) or no ready probe worker here.
            self.assertTrue(r.status_code < 500 or r.json()['code'] == 'probe_unavailable', (method, path, r.text))

    def test_logout_needs_the_csrf_token(self):
        client, session = self.client('viewer')
        self.assertEqual(self.call(client, 'DELETE', '/api/v1/session').status_code, 403)
        self.assertEqual(self.call(client, 'DELETE', '/api/v1/session', csrf=session['csrf_token']).status_code, 200)
        self.assertEqual(self.call(client, 'GET', '/api/v1/nodes').status_code, 401)

    def test_error_responses_share_one_shape_and_leak_nothing(self):
        client, session = self.client('admin')
        for method, path in (('GET', '/api/v1/nodes/not-a-node'), ('GET', '/api/v1/nodes?limit=0'),
                             ('GET', '/api/v1/nodes?cursor=1'), ('GET', '/api/v1/related/jobs/x'),
                             ('GET', '/api/v1/jobs?cursor=%%'), ('POST', '/api/v1/jobs')):
            headers = {'Origin': ORIGIN, 'X-CSRF-Token': session['csrf_token']}
            r = (client.post(path, json={'password': 'secret-value', 'unexpected': 1}, headers=headers)
                 if method == 'POST' else client.get(path, headers=headers))
            self.assertGreaterEqual(r.status_code, 400, path)
            body = r.json()
            self.assertEqual(set(body), ERROR_FIELDS, path)
            for leak in ('Traceback', 'File "', 'secret-value', str(api.ROOT)):
                self.assertNotIn(leak, r.text, path)
            self.assertEqual(r.headers['cache-control'], 'no-store', path)


class LoadTest(Base):
    PATHS = ('/api/v1/overview', '/api/v1/nodes?limit=200', '/api/v1/models?limit=200', '/api/v1/engines?limit=200',
             '/api/v1/agents?limit=200', '/api/v1/configuration/resources?limit=200', '/api/v1/deployments',
             f'/api/v1/related/nodes/{NODE_ID}', '/api/v1/jobs?limit=100&order=desc')
    THREADS, ROUNDS = 8, 6
    GET_LIMIT_S = 5.0  # the design's initial GET bound (dashboard-design.md §5)

    def test_concurrent_cold_reads_build_each_snapshot_once_and_stay_under_the_get_bound(self):
        _, session = self.client('viewer')
        cookie = {'management_session': None}
        counts, lock = {}, threading.Lock()
        wrapped = {}
        for kind, (build, row_id) in api.SNAPSHOTS.items():
            def counted(root, kind=kind, build=build):
                with lock:
                    counts[kind] = counts.get(kind, 0) + 1
                return build(root)
            wrapped[kind] = (counted, row_id)
        # One login; every thread reuses its cookie through its own client.
        login = TestClient(api.app, base_url=ORIGIN)
        login.post('/api/v1/session', json={'password': 'test-owner-password'}, headers={'Origin': ORIGIN})
        cookie['management_session'] = login.cookies['management_session']
        api.SNAPSHOT_CACHE.clear()
        timings, failures = [], []
        start = threading.Barrier(self.THREADS)

        def worker():
            client = TestClient(api.app, base_url=ORIGIN, cookies=cookie)
            start.wait(10)
            for _ in range(self.ROUNDS):
                for path in self.PATHS:
                    began = time.perf_counter()
                    r = client.get(path)
                    elapsed = time.perf_counter() - began
                    with lock:
                        timings.append(elapsed)
                        if r.status_code != 200:
                            failures.append((path, r.status_code, r.text[:200]))

        with patch.dict(api.SNAPSHOTS, wrapped):
            threads = [threading.Thread(target=worker) for _ in range(self.THREADS)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(120)
        self.assertEqual(failures, [])
        self.assertEqual(len(timings), self.THREADS * self.ROUNDS * len(self.PATHS))
        # A full round runs well inside one TTL, so each kind is read from the files exactly once.
        self.assertEqual(counts, {kind: 1 for kind in api.SNAPSHOTS})
        timings.sort()
        p95 = timings[int(len(timings) * 0.95) - 1]
        self.assertLess(timings[-1], self.GET_LIMIT_S, f'slowest GET {timings[-1]:.2f}s, p95 {p95:.3f}s')


if __name__ == '__main__':
    unittest.main()
