"""P2b management API tests: localhost-only, opaque session, CSRF, error shape and allowlist DTOs.

The session database is redirected to a temporary root; no artifact is written into the checkout.
"""
import datetime
import importlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from agentiac import paths  # noqa: E402
SITE = paths.root()

try:
    from fastapi.testclient import TestClient
    api = importlib.import_module('agentiac.management.app')
    session_module = importlib.import_module('agentiac.management.session')
except ModuleNotFoundError as error:
    if error.name not in {'fastapi', 'starlette', 'httpx', 'pydantic'}:
        raise
    raise unittest.SkipTest(f'management API dependencies are not installed: {error}')


class TestManagementAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(patch.stopall)
        ledger = importlib.import_module('agentiac.operations.ledger')
        patch.object(ledger, 'DB_PATH', Path(self.tmp.name) / 'operations.sqlite').start()
        patch.object(ledger, 'LOG_DIR', Path(self.tmp.name) / 'jobs').start()
        patch.object(api, 'SESSION_PATH', Path(self.tmp.name) / 'session.sqlite').start()
        session_module = importlib.import_module('agentiac.management.session')
        session_module.SessionStore(api.SESSION_PATH).initialize('test-owner-password')
        self.client = TestClient(api.app, base_url='http://127.0.0.1:8000')
        self.origin = {'Origin': 'http://127.0.0.1:8000'}

    def tearDown(self):
        self.tmp.cleanup()

    def post_session(self, password, headers):
        return self.client.post('/api/v1/session', json={'password': password}, headers=headers)

    def test_login_localhost(self):
        r = self.post_session('test-owner-password', self.origin)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['actor'] == 'owner' and body['permission'] == 'admin'
        assert 'management_session' in self.client.cookies
        cookie = self.client.cookies['management_session']
        assert len(cookie) > 32
        assert 'session_id' not in body
        assert cookie not in r.text
        assert r.headers['cache-control'] == 'no-store'
        header = r.headers['set-cookie']
        assert 'HttpOnly' in header and 'SameSite=strict' in header and 'Path=/api/v1' in header

    def test_login_origin_refused(self):
        r = self.post_session('test-owner-password', {'Origin': 'https://evil.example'})
        assert r.status_code == 403, r.text
        assert r.json()['code'] == 'origin_not_allowed'

    def test_login_schema_error(self):
        r = self.client.post('/api/v1/session', json={}, headers=self.origin)
        assert r.status_code == 422, r.text
        assert r.json()['code'] == 'schema_error'

    def test_bad_credential(self):
        self.post_session('test-owner-password', self.origin)
        r = self.post_session('different', self.origin)
        assert r.status_code == 401, r.text
        assert r.json()['code'] == 'bad_credential'

    def test_rate_limit(self):
        for _ in range(5):
            assert self.post_session('wrong-password', self.origin).status_code == 401
        r = self.post_session('test-owner-password', self.origin)
        assert r.status_code == 429, r.text
        assert r.json()['code'] == 'rate_limit'

    def test_successful_logins_do_not_lock_out_the_owner(self):
        for _ in range(8):
            assert self.post_session('test-owner-password', self.origin).status_code == 200
        for _ in range(4):
            assert self.post_session('wrong-password', self.origin).status_code == 401
        # A success clears earlier failures; the failure budget starts again.
        assert self.post_session('test-owner-password', self.origin).status_code == 200
        for _ in range(5):
            assert self.post_session('wrong-password', self.origin).status_code == 401
        assert self.post_session('test-owner-password', self.origin).status_code == 429

    def test_state_lost_while_running_is_refused_not_recreated_empty(self):
        self.post_session('test-owner-password', self.origin)
        assert self.client.get('/api/v1/jobs').status_code == 200
        ledger = importlib.import_module('agentiac.operations.ledger')
        ledger.DB_PATH.unlink()
        r = self.client.get('/api/v1/jobs')
        assert r.status_code == 503, r.text
        assert r.json()['code'] == 'state_missing'
        self.assertFalse(ledger.DB_PATH.exists())
        api.SESSION_PATH.unlink()
        r = self.client.get('/api/v1/session')
        assert r.status_code == 503, r.text
        self.assertFalse(api.SESSION_PATH.exists())

    def test_read_without_session(self):
        client = TestClient(api.app, base_url='http://127.0.0.1:8000')
        r = client.get('/api/v1/nodes')
        assert r.status_code == 401, r.text
        assert r.json()['code'] == 'unauthorized'

    def test_nodes(self):
        self.post_session('test-owner-password', self.origin)
        r = self.client.get('/api/v1/nodes')
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['schema_version'] == 1
        assert 'nodes' in body
        assert 'LITELLM_MASTER_KEY' not in json.dumps(body)

    def test_unknown_node(self):
        self.post_session('test-owner-password', self.origin)
        r = self.client.get('/api/v1/nodes/nope')
        assert r.status_code == 404, r.text
        assert r.json()['code'] == 'unknown_node'

    def test_models(self):
        self.post_session('test-owner-password', self.origin)
        r = self.client.get('/api/v1/models')
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['schema_version'] == 1
        assert body['current'] in {row['target'] for row in body['targets']}

    def test_agents(self):
        self.post_session('test-owner-password', self.origin)
        r = self.client.get('/api/v1/agents')
        assert r.status_code == 200, r.text
        body = r.json()
        if (SITE / 'agents/agents.yml').exists():
            assert any(a['id'] == 'pi' for a in body['agents'])
        else:
            assert body['agents'] == []

    def test_configuration_resources(self):
        self.post_session('test-owner-password', self.origin)
        r = self.client.get('/api/v1/configuration/resources?limit=2')
        assert r.status_code == 200, r.text
        assert len(r.json()['resources']) == 2

    def test_capabilities(self):
        self.post_session('test-owner-password', self.origin)
        r = self.client.get('/api/v1/capabilities')
        assert r.status_code == 200, r.text
        assert any(op['id'] == 'read-nodes' and op['implemented'] for op in r.json()['operations'])

    def test_overview(self):
        self.post_session('test-owner-password', self.origin)
        r = self.client.get('/api/v1/overview')
        assert r.status_code == 200, r.text
        assert r.json()['freshness']['status'] == 'unknown'

    def test_csrf_delete_refused(self):
        self.post_session('test-owner-password', self.origin)
        r = self.client.delete('/api/v1/session', headers=self.origin)
        assert r.status_code == 403, r.text
        assert r.json()['code'] == 'csrf'

    def test_csrf_delete(self):
        body = self.post_session('test-owner-password', self.origin).json()
        r = self.client.delete('/api/v1/session', headers={**self.origin,
                               'X-CSRF-Token': body['csrf_token']})
        assert r.status_code == 200, r.text
        assert r.json()['deleted'] is True
        assert 'management_session' not in self.client.cookies

    def test_expired_session(self):
        patch.object(session_module, 'SESSION_TTL_S', 0).start()
        self.post_session('test-owner-password', self.origin)
        r = self.client.get('/api/v1/session')
        assert r.status_code == 401, r.text
        assert r.json()['code'] == 'unauthorized'

    def test_limit_refused(self):
        self.post_session('test-owner-password', self.origin)
        r = self.client.get('/api/v1/nodes?limit=201')
        assert r.status_code == 422, r.text
        assert r.json()['code'] == 'schema_error'

    def test_uninitialized_owner_cannot_be_claimed_by_login(self):
        patch.object(api, 'SESSION_PATH', Path(self.tmp.name) / 'uninitialized.sqlite').start()
        r = self.post_session('attacker-password', self.origin)
        self.assertEqual(r.status_code, 503)
        self.assertFalse(session_module.SessionStore(api.SESSION_PATH).initialized())

    def test_failed_credentials_are_rate_limited(self):
        for _ in range(5):
            self.assertEqual(self.post_session('wrong', self.origin).status_code, 401)
        self.assertEqual(self.post_session('test-owner-password', self.origin).status_code, 429)

    def test_owner_hash_is_salted_and_initialization_cannot_replace_it(self):
        import sqlite3
        store = session_module.SessionStore(api.SESSION_PATH)
        with self.assertRaises(ValueError):
            store.initialize('replacement-password')
        with sqlite3.connect(api.SESSION_PATH) as conn:
            digest = conn.execute('SELECT password_hash FROM credentials').fetchone()[0]
        self.assertTrue(digest.startswith('scrypt$'))
        other = session_module.SessionStore(Path(self.tmp.name) / 'other.sqlite')
        other.initialize('test-owner-password')
        with sqlite3.connect(other.path) as conn:
            self.assertNotEqual(conn.execute('SELECT password_hash FROM credentials').fetchone()[0], digest)

    def test_host_and_exact_origin_are_checked(self):
        self.post_session('test-owner-password', self.origin)
        self.assertEqual(self.client.get('/api/v1/nodes', headers={'Host': 'evil.example'}).status_code, 400)
        self.assertEqual(self.post_session('test-owner-password', {'Origin': 'http://localhost:8001'}).status_code, 403)

    def test_validation_does_not_echo_password_and_rejects_extra_fields(self):
        sentinel = 'SECRET_SENTINEL'
        r = self.client.post('/api/v1/session', json={'password': {'secret': sentinel}}, headers=self.origin)
        self.assertEqual(r.status_code, 422)
        self.assertNotIn(sentinel, r.text)
        r = self.client.post('/api/v1/session', json={'password': 'test-owner-password', 'admin': True}, headers=self.origin)
        self.assertEqual(r.status_code, 422)
        r = self.client.post('/api/v1/session', content='x' * 17000,
                             headers={**self.origin, 'Content-Type': 'application/json'})
        self.assertEqual(r.status_code, 413)

    def test_configuration_uses_nested_field_allowlist(self):
        from agentiac.management.reads import safe_declaration, safe_route
        sentinel = 'SECRET_SENTINEL'
        public = safe_declaration({'id': 'safe', 'env': {'TOKEN': sentinel},
                                   'password': sentinel, 'model': {'credential': sentinel}})
        self.assertEqual(public, {'id': 'safe'})
        self.assertNotIn(sentinel, json.dumps(safe_route({'default': 'safe', 'targets': {
            'safe': {'node': 'worker-a', 'env': {'TOKEN': sentinel}}}})))

    def test_unknown_hardware_timestamp_does_not_attest_fingerprint(self):
        from agentiac.management.reads import hardware_dto
        naive = datetime.datetime(2026, 11, 12, 0, 0).isoformat()
        for timestamp in (None, 'invalid', naive):
            dto = hardware_dto({'hw_fp': 'old', 'collected_at': timestamp})
            self.assertIsNone(dto.hw_fp)
            self.assertIsNone(dto.stale)

    def test_production_probe_is_unavailable(self):
        body = self.post_session('test-owner-password', self.origin).json()
        target = self.client.get('/api/v1/nodes').json()['nodes'][0]['id']
        r = self.client.post('/api/v1/jobs', json={'idempotency_key': 'disabled',
                             'operation': 'probe-node', 'target': target},
                             headers={**self.origin, 'X-CSRF-Token': body['csrf_token']})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()['code'], 'probe_unavailable')

    def test_worker_readiness_controls_capability_target_scope_and_cooldown(self):
        from agentiac.operations.ledger import Ledger
        body = self.post_session('test-owner-password', self.origin).json()
        headers = {**self.origin, 'X-CSRF-Token': body['csrf_token']}
        node_ids = [row['id'] for row in self.client.get('/api/v1/nodes').json()['nodes']]
        first, second = node_ids[0], node_ids[1]
        store = Ledger()
        self.addCleanup(store.close)
        store.publish_worker('fixture', [first])
        operations = self.client.get('/api/v1/capabilities').json()['operations']
        self.assertTrue(next(op for op in operations if op['id'] == 'probe-node')['implemented'])
        payload = {'idempotency_key': 'ready', 'operation': 'probe-node', 'target': first}
        self.assertEqual(self.client.post('/api/v1/jobs', json=payload, headers=headers).status_code, 202)
        payload['idempotency_key'] = 'cooldown'
        self.assertEqual(self.client.post('/api/v1/jobs', json=payload, headers=headers).status_code, 429)
        payload['target'] = second
        self.assertEqual(self.client.post('/api/v1/jobs', json=payload, headers=headers).status_code, 403)
        store.unpublish_worker('fixture')
        operations = self.client.get('/api/v1/capabilities').json()['operations']
        self.assertFalse(next(op for op in operations if op['id'] == 'probe-node')['implemented'])

    def test_get_readers_never_start_a_process_or_probe(self):
        from agentiac.operations import probe
        self.post_session('test-owner-password', self.origin)
        with patch.object(probe.subprocess, 'Popen', side_effect=AssertionError('GET started a process')):
            for path in ('capabilities', 'overview', 'nodes', 'models', 'agents', 'configuration/resources', 'jobs'):
                response = self.client.get('/api/v1/' + path)
                self.assertEqual(response.status_code, 200, path + ': ' + response.text)
