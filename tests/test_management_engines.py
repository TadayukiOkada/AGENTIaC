"""Engine releases derived from the model and configuration snapshots."""
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from agentiac.management import cache, engines, relations  # noqa: E402


def model(target, node, backend, engine=None, current=False):
    declared = {'backend': backend}
    if engine:
        declared['engine'] = engine
    return {'target': target, 'node': node, 'current': current, 'declared': declared}


def backend(stem, **declared):
    return SimpleNamespace(id=f'backend/{stem}', kind='backend', declared=declared)


STRATA = 'https://github.com/Example/Strata'
TF = {'repository': 'https://github.com/example/TensorFold.git', 'version': '1.1.4', 'revision': 'eab8582', 'ggml_revision': None}


def snapshots(models, configuration, nodes=(), agents=(), clock=None):
    store = cache.SnapshotCache(clock=clock) if clock else cache.SnapshotCache()
    ids = {'nodes': lambda r: r.id, 'models': lambda r: r['target'], 'agents': lambda r: r.id,
           'configuration': lambda r: r.id}
    rows = {'nodes': nodes, 'models': models, 'agents': agents, 'configuration': configuration}
    return {kind: store.get(kind, 'root', lambda kind=kind: (list(rows[kind]), {}), ids[kind]) for kind in rows}


FIXTURE = snapshots(
    models=[model('a', 'worker-a', 'strata-a', {'repository': STRATA, 'version': '0.2.12', 'revision': '146df3c',
                                                'ggml_revision': 'g1'}, current=True),
            model('b', 'worker-b', 'strata-a-variant'), model('tf', 'worker-c', 'tensorfold-backend', TF),
            model('llama', 'control', 'qwen35-4b-q4')],
    configuration=[backend('strata-a', repository=STRATA, source_version='0.2.12', source_revision='146df3c'),
                   backend('strata-a-variant', repository=STRATA, source_version='0.2.12', source_revision='146df3c'),
                   backend('strata-old', repository=STRATA, source_version='0.2.12', source_revision='0000000'),
                   backend('tensorfold-backend', id='tensorfold-backend-v114'),
                   backend('vllm-x', source_revision='3a09141'),
                   SimpleNamespace(id='profile/qwen35-4b-q4', kind='profile', declared={})],
    nodes=[SimpleNamespace(id=name, default_profile=None, strata_default_backend=None, spark_tensorfold=None)
           for name in ('worker-a', 'worker-b', 'worker-c', 'control')])


class EngineRowsTest(unittest.TestCase):
    def rows(self):
        return {row['id']: row for row in engines.engine_rows(FIXTURE['models'], FIXTURE['configuration'])}

    def test_backends_of_one_release_are_grouped_with_their_targets_and_nodes(self):
        rows = self.rows()
        self.assertEqual(sorted(rows), ['backend-vllm-x', 'strata-0.2.12@0000000', 'strata-0.2.12@146df3c',
                                        'tensorfold-1.1.4'])
        strata = rows['strata-0.2.12@146df3c']
        self.assertEqual((strata['backends'], strata['targets'], strata['nodes']),
                         (['strata-a', 'strata-a-variant'], ['a', 'b'], ['worker-a', 'worker-b']))
        self.assertEqual((strata['name'], strata['current'], strata['ggml_revisions']), ('strata', True, ['g1']))
        self.assertEqual(rows['strata-0.2.12@0000000']['targets'], [])

    def test_target_engine_block_fills_a_manifest_without_repository(self):
        tf = self.rows()['tensorfold-1.1.4']
        self.assertEqual((tf['repository'], tf['version'], tf['revision'], tf['backends'], tf['declared']),
                         (TF['repository'], '1.1.4', 'eab8582', ['tensorfold-backend'], True))

    def test_undeclared_engine_is_its_own_row_not_a_guess(self):
        vllm = self.rows()['backend-vllm-x']
        self.assertEqual((vllm['name'], vllm['repository'], vllm['version'], vllm['revision'], vllm['declared']),
                         (None, None, None, '3a09141', False))

    def test_profiles_are_not_engines(self):
        self.assertFalse([row for row in self.rows().values() if 'qwen35-4b-q4' in row['backends']])

    def test_snapshot_is_as_old_and_as_stale_as_its_sources(self):
        models, configuration = FIXTURE['models'], FIXTURE['configuration']
        older = cache.Snapshot(**{**configuration.__dict__, 'built': models.built - 5, 'error': 'OSError'})
        snap = engines.engine_snapshot(models, older)
        self.assertEqual((snap.kind, snap.built, snap.error), ('engines', models.built - 5, 'OSError'))
        self.assertEqual(snap.ids, tuple(sorted(snap.ids)))

    def test_engine_relations_both_ways(self):
        snaps = {**FIXTURE, 'engines': engines.engine_snapshot(FIXTURE['models'], FIXTURE['configuration'])}
        found = {(item['kind'], item['id'], item['relation']) for item in
                 relations.related('engines', 'strata-0.2.12@146df3c', snaps)}
        self.assertEqual(found, {('configuration', 'backend/strata-a', 'backend pinning it'),
                                 ('configuration', 'backend/strata-a-variant', 'backend pinning it'),
                                 ('models', 'a', 'target using it'), ('models', 'b', 'target using it'),
                                 ('nodes', 'worker-a', 'node using it'), ('nodes', 'worker-b', 'node using it')})
        self.assertIn(('engines', 'tensorfold-1.1.4', 'engine release'),
                      {(i['kind'], i['id'], i['relation']) for i in relations.related('models', 'tf', snaps)})
        self.assertIn(('engines', 'strata-0.2.12@0000000', 'engine release'),
                      {(i['kind'], i['id'], i['relation']) for i in
                       relations.related('configuration', 'backend/strata-old', snaps)})
        self.assertFalse([i for i in relations.related('models', 'llama', snaps) if i['kind'] == 'engines'])


try:
    from fastapi.testclient import TestClient
    api = importlib.import_module('agentiac.management.app')
    session_module = importlib.import_module('agentiac.management.session')
except ModuleNotFoundError as error:
    if error.name not in {'fastapi', 'starlette', 'httpx', 'pydantic'}:
        raise
    api = None


@unittest.skipIf(api is None, 'management API dependencies are not installed')
class EngineAPITest(unittest.TestCase):
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
        self.client = TestClient(api.app, base_url='http://127.0.0.1:8000')
        assert self.client.post('/api/v1/session', json={'password': 'test-owner-password'},
                                headers={'Origin': 'http://127.0.0.1:8000'}).status_code == 200

    def get(self, path, status=200):
        r = self.client.get('/api/v1' + path)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def test_lifecycle_list_detail_and_related_keep_mixed_uses(self):
        rows = {row['id']: row for row in self.get('/engines?limit=200')['engines']}
        self.assertTrue(rows)
        for row in rows.values():
            self.assertIn('lifecycle', row)
            self.assertEqual(self.get('/engines/' + row['id'])['lifecycle'], row['lifecycle'])
            self.assertEqual(self.get('/related/engines/' + row['id'])['kind'], 'engines')
        if any(row['lifecycle']['history_only'] for row in rows.values()):
            mixed = next(row for row in rows.values()
                         if not row['lifecycle']['history_only']
                         and {'default', 'rollback'} <= set(row['lifecycle']['roles']))
            related = self.get('/related/engines/' + mixed['id'])['relations']
            self.assertTrue(any(item['id'].startswith('retention/') and item['exists'] for item in related))

    def test_stale_engine_snapshot_survives_failed_rebuild(self):
        first = self.get('/engines?limit=200')
        old_ttl = api.SNAPSHOT_CACHE.ttl_s
        self.addCleanup(setattr, api.SNAPSHOT_CACHE, 'ttl_s', old_ttl)
        api.SNAPSHOT_CACHE.ttl_s = 0
        with patch.object(api, 'build_engine_snapshot', side_effect=ValueError('bad lifecycle')):
            stale = self.get('/engines?limit=200')
        self.assertTrue(stale['snapshot']['stale'])
        self.assertEqual(stale['engines'], first['engines'])
        self.assertEqual(stale['target_names'], first['target_names'])

    def test_reading_lifecycle_and_related_never_starts_a_process(self):
        with patch('subprocess.Popen', side_effect=AssertionError('unexpected subprocess')):
            body = self.get('/engines?limit=200')
            self.get('/engines/' + body['engines'][0]['id'])
            self.get('/related/engines/' + body['engines'][0]['id'])

    def test_engine_build_reads_one_graph_for_rows_and_lifecycle(self):
        with patch.object(engines.declared, 'load', wraps=engines.declared.load) as read:
            rows, _ = engines.build_engine_snapshot(api.ROOT)
        self.assertEqual(read.call_count, 1)
        self.assertTrue(all(row['lifecycle'] for row in rows))

    def test_first_engine_snapshot_failure_returns_503(self):
        with patch.object(api, 'build_engine_snapshot', side_effect=ValueError('bad lifecycle')):
            self.assertEqual(self.get('/engines', 503)['code'], 'snapshot_unavailable')

    def test_real_declarations(self):
        body = self.get('/engines?limit=200')
        rows = {row['id']: row for row in body['engines']}
        self.assertEqual(body['snapshot']['ttl_s'], 30)
        targets = {row['target']: row for row in self.get('/models?limit=200')['targets']}
        # Every target whose backend is a declared backend manifest appears under exactly one engine.
        backends = {row['id'].partition('/')[2] for row in self.get('/configuration/resources?limit=200')['resources']
                    if row['kind'] == 'backend'}
        listed = [target for row in rows.values() for target in row['targets']]
        self.assertEqual(sorted(listed), sorted(t for t, row in targets.items()
                                                if row['declared'].get('backend') in backends))
        current = [row for row in rows.values() if row['current']]
        self.assertEqual(len(current), 1)
        self.assertIn(self.get('/models?limit=200')['current'], current[0]['targets'])
        first = sorted(rows)[0]
        self.assertEqual(self.get(f'/engines/{first}')['id'], first)
        self.assertEqual(self.get('/engines/not-an-engine', 404)['code'], 'unknown_engine')
        walked, cursor = [], None
        for _ in range(50):
            page = self.get('/engines?limit=3' + (f'&cursor={cursor}' if cursor else ''))
            walked += [row['id'] for row in page['engines']]
            cursor = page['next_cursor']
            if not cursor:
                break
        self.assertEqual(walked, sorted(rows))
        self.assertEqual(self.get(f'/related/engines/{current[0]["id"]}')['kind'], 'engines')


if __name__ == '__main__':
    unittest.main()
