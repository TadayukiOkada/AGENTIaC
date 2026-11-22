"""Relations between declared resources for the dashboard detail views.

Unit tests build snapshots from small in-memory rows. API tests read the checkout's real declarations
through a cleared cache, with ledger/session state in a temporary directory.
"""
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from agentiac import paths  # noqa: E402
from agentiac.management import cache, relations  # noqa: E402

SITE = paths.root()


def node(node_id, **facts):
    return SimpleNamespace(id=node_id, default_profile=facts.get('default_profile'),
                           strata_default_backend=facts.get('strata_default_backend'),
                           spark_tensorfold=facts.get('spark_tensorfold'),
                           tensorfold_backend=facts.get('tensorfold_backend'))


def model(target, node_id, backend):
    return {'target': target, 'node': node_id, 'declared': {'backend': backend}}


def resource(resource_id, **declared):
    return SimpleNamespace(id=resource_id, kind=resource_id.split('/')[0], declared=declared)


def snapshots(nodes=(), models=(), agents=(), configuration=()):
    store = cache.SnapshotCache()
    ids = {'nodes': lambda row: row.id, 'models': lambda row: row['target'],
           'agents': lambda row: row.id, 'configuration': lambda row: row.id}
    rows = {'nodes': nodes, 'models': models, 'agents': agents, 'configuration': configuration}
    return {kind: store.get(kind, 'root', lambda kind=kind: (list(rows[kind]), {}), ids[kind]) for kind in rows}


FIXTURE = snapshots(
    nodes=[node('worker-a', default_profile='small'), node('worker-b', spark_tensorfold=True, tensorfold_backend='tf-1.1.4-qfn-mxfp8-nvfp4'),
           node('worker-c', strata_default_backend='strata-a')],
    models=[model('worker-a-a', 'worker-a', 'strata-a'), model('worker-a-small', 'worker-a', 'small'),
            model('worker-b', 'worker-b', 'tf-1.1.4-qfn-mxfp8-nvfp4'), model('ghost', 'worker-z', 'not-declared')],
    agents=[SimpleNamespace(id='pi')],
    configuration=[resource('backend/strata-a'), resource('backend/tf-1.1.4-qfn-mxfp8-nvfp4'), resource('profile/small'),
                   resource('retention/worker-a'), resource('component/pi', hosts=['worker-a', 'nowhere']),
                   resource('component/driver', hosts=['worker-a', 'worker-b']), resource('agent/pi')])


def pairs(found):
    return [(item['kind'], item['id'], item['relation'], item['exists']) for item in found]


class RelationsTest(unittest.TestCase):
    def test_node_relations(self):
        self.assertEqual(pairs(relations.related('nodes', 'worker-a', FIXTURE)), [
            ('configuration', 'component/driver', 'component on this node', True),
            ('configuration', 'component/pi', 'component on this node', True),
            ('configuration', 'profile/small', 'default profile', True),
            ('configuration', 'retention/worker-a', 'retention policy', True),
            ('models', 'worker-a-a', 'target on this node', True),
            ('models', 'worker-a-small', 'target on this node', True),
        ])
        self.assertIn(('configuration', 'backend/tf-1.1.4-qfn-mxfp8-nvfp4', 'TensorFold backend', True),
                      pairs(relations.related('nodes', 'worker-b', FIXTURE)))
        self.assertEqual(pairs(relations.related('nodes', 'worker-c', FIXTURE)),
                         [('configuration', 'backend/strata-a', 'default Strata backend', True)])

    def test_node_jobs_follow_declared_relations(self):
        jobs = [{'id': 'job-2', 'operation': 'probe-node', 'status': 'running'},
                {'id': 'job-1', 'operation': 'probe-node', 'status': 'succeeded'}]
        found = pairs(relations.related('nodes', 'worker-c', FIXTURE, jobs))
        self.assertEqual(found[-2:], [('jobs', 'job-2', 'probe-node job, running', True),
                                      ('jobs', 'job-1', 'probe-node job, succeeded', True)])

    def test_model_relations_resolve_backend_or_profile_and_keep_missing(self):
        self.assertEqual(pairs(relations.related('models', 'worker-a-a', FIXTURE)), [
            ('configuration', 'backend/strata-a', 'backend', True), ('nodes', 'worker-a', 'runs on', True)])
        self.assertEqual(pairs(relations.related('models', 'worker-a-small', FIXTURE))[0],
                         ('configuration', 'profile/small', 'profile', True))
        self.assertEqual(pairs(relations.related('models', 'ghost', FIXTURE)), [
            ('configuration', 'backend/not-declared', 'backend', False), ('nodes', 'worker-z', 'runs on', False)])

    def test_configuration_relations_are_the_reverse(self):
        self.assertEqual(pairs(relations.related('configuration', 'backend/strata-a', FIXTURE)), [
            ('models', 'worker-a-a', 'used by target', True), ('nodes', 'worker-c', 'node default Strata backend', True)])
        self.assertEqual(pairs(relations.related('configuration', 'profile/small', FIXTURE)), [
            ('models', 'worker-a-small', 'used by target', True), ('nodes', 'worker-a', 'node default profile', True)])
        self.assertEqual(pairs(relations.related('configuration', 'retention/worker-a', FIXTURE)),
                         [('nodes', 'worker-a', 'retention for node', True)])
        self.assertEqual(pairs(relations.related('configuration', 'component/pi', FIXTURE)), [
            ('agents', 'pi', 'agent', True), ('nodes', 'nowhere', 'installed on', False),
            ('nodes', 'worker-a', 'installed on', True)])
        self.assertEqual(pairs(relations.related('configuration', 'agent/pi', FIXTURE)), [
            ('agents', 'pi', 'agent', True), ('configuration', 'component/pi', 'component pin', True)])

    def test_agent_relations(self):
        self.assertEqual(pairs(relations.related('agents', 'pi', FIXTURE)), [
            ('configuration', 'agent/pi', 'agent declaration', True),
            ('configuration', 'component/pi', 'component pin', True)])

    def test_unknown_resource_is_none(self):
        for kind in ('nodes', 'models', 'agents', 'configuration'):
            self.assertIsNone(relations.related(kind, 'not-declared', FIXTURE))


try:
    from fastapi.testclient import TestClient
    api = importlib.import_module('agentiac.management.app')
    session_module = importlib.import_module('agentiac.management.session')
    dto_module = importlib.import_module('agentiac.operations.dto')
except ModuleNotFoundError as error:
    if error.name not in {'fastapi', 'starlette', 'httpx', 'pydantic'}:
        raise
    api = None


@unittest.skipIf(api is None, 'management API dependencies are not installed')
class RelatedAPITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        self.ledger_module = importlib.import_module('agentiac.operations.ledger')
        patch.object(self.ledger_module, 'DB_PATH', Path(self.tmp.name) / 'operations.sqlite').start()
        patch.object(self.ledger_module, 'LOG_DIR', Path(self.tmp.name) / 'jobs').start()
        patch.object(api, 'SESSION_PATH', Path(self.tmp.name) / 'session.sqlite').start()
        session_module.SessionStore(api.SESSION_PATH).initialize('test-owner-password')
        api.SNAPSHOT_CACHE.clear()
        self.addCleanup(api.SNAPSHOT_CACHE.clear)
        self.client = TestClient(api.app, base_url='http://127.0.0.1:8000')
        r = self.client.post('/api/v1/session', json={'password': 'test-owner-password'},
                             headers={'Origin': 'http://127.0.0.1:8000'})
        assert r.status_code == 200, r.text
        self.session = r.json()

    def get(self, path, status=200):
        r = self.client.get('/api/v1' + path)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def test_every_declared_relation_points_at_a_declared_resource(self):
        lists = {'nodes': ('nodes', 'id'), 'models': ('targets', 'target'), 'agents': ('agents', 'id'),
                 'configuration': ('resources', 'id'), 'engines': ('engines', 'id')}
        paths_map = {'nodes': '/nodes', 'models': '/models', 'agents': '/agents',
                     'configuration': '/configuration/resources', 'engines': '/engines'}
        ids = {kind: {row[key] for row in self.get(paths_map[kind] + '?limit=200')[field]}
               for kind, (field, key) in lists.items()}
        seen = 0
        for kind, members in ids.items():
            for item in members:
                body = self.get(f'/related/{kind}/{item}')
                self.assertEqual((body['kind'], body['id']), (kind, item))
                self.assertEqual(set(body['snapshots']), set(lists))
                self.assertTrue(all(relation['kind'] != kind or relation['id'] != item for relation in body['relations']))
                for relation in body['relations']:
                    seen += 1
                    self.assertTrue(relation['exists'], (kind, item, relation))
                    self.assertIn(relation['id'], ids[relation['kind']], (kind, item, relation))
        self.assertGreater(seen, 0)

    def test_relations_are_symmetric_for_targets_and_nodes(self):
        for target in self.get('/models?limit=200')['targets']:
            node_relations = self.get(f"/related/nodes/{target['node']}")['relations']
            self.assertIn({'kind': 'models', 'id': target['target'], 'relation': 'target on this node', 'exists': True},
                          node_relations)

    def test_node_relations_list_its_recent_jobs_newest_first(self):
        node_ids = [row['id'] for row in self.get('/nodes?limit=200')['nodes']]
        first, second = node_ids[0], node_ids[1]
        ledger = self.ledger_module.Ledger()
        try:
            for number, target in enumerate([first, second, first]):
                ledger.accept(dto_module.JobRequestDTO(idempotency_key=f'key-{number}', operation='probe-node',
                                                       target=target, args={}, timeout_s=20),
                              actor=self.session['actor'], permission=self.session['permission'], now=1000 + number)
            expected = [job['id'] for job in ledger.list(newest_first=True)['jobs'] if job['target'] == first]
        finally:
            ledger.close()
        jobs = [item for item in self.get(f'/related/nodes/{first}')['relations'] if item['kind'] == 'jobs']
        self.assertEqual([item['id'] for item in jobs], expected)
        self.assertEqual(jobs[0]['relation'], 'probe-node job, queued')
        legacy = json.loads((SITE / 'gateway/local-coder.json').read_text()).get('legacy_targets') or {}
        if legacy:
            old_key, new_target = next(iter(legacy.items()))
            related = self.get(f'/related/models/{old_key}')
            self.assertEqual(related['id'], new_target)
            self.assertFalse([item for item in related['relations'] if item['kind'] == 'jobs'])
            self.assertEqual(self.get(f'/models/{old_key}')['target'], new_target)
        else:
            target = self.get('/models?limit=200')['targets'][0]['target']
            related = self.get(f'/related/models/{target}')
            self.assertEqual(related['id'], target)
            self.assertFalse([item for item in related['relations'] if item['kind'] == 'jobs'])

    def test_unknown_resource_kind_and_session(self):
        self.assertEqual(self.get('/related/nodes/not-declared', 404)['code'], 'unknown_resource')
        self.assertEqual(self.get('/related/jobs/anything', 422)['code'], 'schema_error')
        first_config = self.get('/configuration/resources?limit=1')['resources'][0]['id']
        config = self.get(f'/related/configuration/{first_config}')
        self.assertEqual(config['id'], first_config)
        first_node = self.get('/nodes?limit=1')['nodes'][0]['id']
        anonymous = TestClient(api.app, base_url='http://127.0.0.1:8000')
        self.assertEqual(anonymous.get(f'/api/v1/related/nodes/{first_node}').status_code, 401)


class LedgerRecentForTargetTest(unittest.TestCase):
    def test_limit_is_validated(self):
        ledger_module = importlib.import_module('agentiac.operations.ledger')
        errors = importlib.import_module('agentiac.management.errors')
        with tempfile.TemporaryDirectory() as directory:
            ledger = ledger_module.Ledger(path=Path(directory) / 'operations.sqlite', log_dir=Path(directory) / 'jobs')
            try:
                self.assertEqual(ledger.recent_for_target('worker-a'), [])
                with self.assertRaises(errors.ApiError):
                    ledger.recent_for_target('worker-a', 0)
            finally:
                ledger.close()


@unittest.skipIf(api is None, 'management API dependencies are not installed')
class NodeFirstNamesAPITest(unittest.TestCase):
    """Responses that mention route targets carry their node-first display names (naming stage 1)."""
    setUp = RelatedAPITest.setUp
    get = RelatedAPITest.get

    def test_models_and_target_names(self):
        body = self.get('/models?limit=200')
        names = {row['target']: row['name'] for row in body['targets']}
        self.assertTrue(all(name and not name.startswith('strata-') for name in names.values()), names)
        self.assertEqual(body['current_name'], names[body['current']])
        first_node = self.get('/nodes?limit=1')['nodes'][0]['id']
        legacy = json.loads((SITE / 'gateway/local-coder.json').read_text()).get('legacy_targets') or {}
        for path in ('/engines?limit=200', '/configuration/resources?limit=200', '/deployments',
                     f'/related/nodes/{first_node}'):
            mapped = self.get(path)['target_names']
            self.assertEqual({key: mapped[key] for key in names}, names, path)
            for old_key, new_target in legacy.items():
                self.assertEqual(mapped.get(old_key), new_target)
        # IDs and URLs keep the route key.
        first = sorted(names)[0]
        self.assertEqual(self.get(f'/models/{first}')['target'], first)


if __name__ == '__main__':
    unittest.main()
