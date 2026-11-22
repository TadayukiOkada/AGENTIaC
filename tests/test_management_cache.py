"""Snapshot cache and keyset cursors for the management resource lists.

The cache tests use a fake clock and in-memory builders. The API tests read the checkout's real
declarations through a cleared cache, with state redirected to a temporary root.
"""
import datetime
import importlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from agentiac.management import cache  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def rows(*ids):
    return [SimpleNamespace(id=row_id) for row_id in ids]


def row_id(row):
    return row.id


class SnapshotCacheTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cache = cache.SnapshotCache(ttl_s=30, clock=self.clock)
        self.builds = 0

    def builder(self, *ids, extra=None):
        def build():
            self.builds += 1
            return rows(*ids), extra or {}
        return build

    def test_reused_within_ttl_and_rebuilt_after(self):
        first = self.cache.get('nodes', 'root', self.builder('b', 'a'), row_id)
        self.clock.now += 29
        self.assertIs(self.cache.get('nodes', 'root', self.builder('c'), row_id), first)
        self.assertEqual(self.builds, 1)
        self.clock.now += 1
        second = self.cache.get('nodes', 'root', self.builder('c'), row_id)
        self.assertEqual(second.ids, ('c',))
        self.assertEqual(self.builds, 2)
        self.assertEqual(self.cache.describe(second)['age_s'], 0)

    def test_rows_are_sorted_and_found_by_id(self):
        snap = self.cache.get('nodes', 'root', self.builder('worker-b', 'control', 'worker-a'), row_id)
        self.assertEqual(snap.ids, ('control', 'worker-a', 'worker-b'))
        self.assertEqual(snap.by_id('worker-a').id, 'worker-a')
        self.assertIsNone(snap.by_id('worker'))
        self.assertIsNone(snap.by_id('zzz'))

    def test_scopes_and_kinds_are_separate(self):
        self.cache.get('nodes', 'root-a', self.builder('a'), row_id)
        self.assertEqual(self.cache.get('nodes', 'root-b', self.builder('b'), row_id).ids, ('b',))
        self.assertEqual(self.cache.get('agents', 'root-a', self.builder('c'), row_id).ids, ('c',))
        self.assertEqual(self.builds, 3)

    def test_concurrent_requests_share_one_rebuild(self):
        started, release = threading.Event(), threading.Event()

        def slow():
            self.builds += 1
            started.set()
            release.wait(5)
            return rows('a'), {}

        results = []
        threads = [threading.Thread(target=lambda: results.append(self.cache.get('nodes', 'root', slow, row_id)))
                   for _ in range(8)]
        for thread in threads:
            thread.start()
        self.assertTrue(started.wait(5))
        release.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(self.builds, 1)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(result is results[0] for result in results))

    def test_failed_rebuild_serves_last_good_snapshot_marked_stale(self):
        good = self.cache.get('models', 'root', self.builder('a', extra={'current': 'a'}), row_id)

        def broken():
            self.builds += 1
            raise OSError('declaration unreadable')

        self.clock.now += 30
        with self.assertLogs('agentiac.management.cache', 'ERROR'):
            stale = self.cache.get('models', 'root', broken, row_id)
        self.assertEqual((stale.ids, stale.extra, stale.generated_at), (good.ids, good.extra, good.generated_at))
        described = self.cache.describe(stale)
        self.assertEqual((described['stale'], described['error'], described['age_s']), (True, 'OSError', 30))
        self.assertIn('rebuild failed: OSError', self.cache.freshness('root')['models'])
        # Not retried on every request: the next attempt waits another TTL.
        self.clock.now += 10
        self.cache.get('models', 'root', broken, row_id)
        self.assertEqual(self.builds, 2)
        self.clock.now += 20
        recovered = self.cache.get('models', 'root', self.builder('b'), row_id)
        self.assertEqual((recovered.ids, recovered.error), (('b',), None))

    def test_first_build_failure_is_unavailable(self):
        def broken():
            raise ValueError('bad yaml')

        with self.assertLogs('agentiac.management.cache', 'ERROR'), self.assertRaises(cache.SnapshotUnavailable):
            self.cache.get('agents', 'root', broken, row_id)
        self.assertEqual(self.cache.freshness('root'), {})

    def test_duplicate_ids_are_a_failed_build(self):
        with self.assertLogs('agentiac.management.cache', 'ERROR'), self.assertRaises(cache.SnapshotUnavailable):
            self.cache.get('agents', 'root', self.builder('a', 'a'), row_id)

    def test_freshness_does_not_build(self):
        self.assertEqual(self.cache.freshness('root'), {})
        self.cache.get('nodes', 'root', self.builder('a'), row_id)
        self.clock.now += 12
        self.assertEqual(self.cache.freshness('root'), {'nodes': 'snapshot 12s old, refreshed after 30s'})
        self.assertEqual(self.builds, 1)


class CursorTest(unittest.TestCase):
    def snap(self, kind, *ids):
        return cache.SnapshotCache().get(kind, 'root', lambda: (rows(*ids), {}), row_id)

    def walk(self, snap, limit):
        seen, cursor = [], None
        for _ in range(100):
            chunk, cursor = cache.page(snap, limit, cursor)
            seen += [row.id for row in chunk]
            if cursor is None:
                return seen
        self.fail('cursor did not terminate')

    def test_walk_returns_every_row_once_in_id_order(self):
        ids = [f'profile/{n:02d}' for n in range(23)]
        snap = self.snap('configuration', *reversed(ids))
        for limit in (1, 2, 7, 22, 23, 200):
            self.assertEqual(self.walk(snap, limit), ids, limit)

    def test_empty_and_exact_pages(self):
        self.assertEqual(cache.page(self.snap('agents'), 50), ([], None))
        chunk, cursor = cache.page(self.snap('agents', 'a', 'b'), 2)
        self.assertEqual(([row.id for row in chunk], cursor), (['a', 'b'], None))

    def test_cursor_continues_across_a_rebuild(self):
        chunk, cursor = cache.page(self.snap('nodes', 'a', 'b', 'c', 'd'), 2)
        self.assertEqual([row.id for row in chunk], ['a', 'b'])
        # 'b' and 'c' were removed and 'bb' and 'e' added before the next page was read.
        chunk, cursor = cache.page(self.snap('nodes', 'a', 'bb', 'd', 'e'), 2, cursor)
        self.assertEqual([row.id for row in chunk], ['bb', 'd'])
        chunk, cursor = cache.page(self.snap('nodes', 'a', 'bb', 'd', 'e'), 2, cursor)
        self.assertEqual(([row.id for row in chunk], cursor), (['e'], None))

    def test_cursor_is_opaque_and_bound_to_its_list(self):
        _, cursor = cache.page(self.snap('nodes', 'a', 'b'), 1)
        self.assertNotEqual(cursor, 'a')
        self.assertFalse(cursor.isdigit())
        self.assertEqual(cache.decode_cursor('nodes', cursor), 'a')
        with self.assertRaisesRegex(ValueError, 'another resource list'):
            cache.page(self.snap('models', 'a', 'b'), 1, cursor)

    def test_invalid_cursors_and_limits_are_refused(self):
        snap = self.snap('nodes', 'a', 'b')
        for cursor in ('1', 'not base64!', cache.encode_cursor('nodes', 'a')[:-3] + '###', 'x' * 2000,
                       'eyJ2IjoyLCJraW5kIjoibm9kZXMiLCJhZnRlciI6ImEifQ'):  # version 2
            with self.assertRaises(ValueError, msg=cursor):
                cache.page(snap, 1, cursor)
        for limit in (0, -1, 201):
            with self.assertRaises(ValueError):
                cache.page(snap, limit)


try:
    from fastapi.testclient import TestClient
    api = importlib.import_module('agentiac.management.app')
    session_module = importlib.import_module('agentiac.management.session')
except ModuleNotFoundError as error:
    if error.name not in {'fastapi', 'starlette', 'httpx', 'pydantic'}:
        raise
    api = None


@unittest.skipIf(api is None, 'management API dependencies are not installed')
class ManagementCacheAPITest(unittest.TestCase):
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
        r = self.client.post('/api/v1/session', json={'password': 'test-owner-password'},
                             headers={'Origin': 'http://127.0.0.1:8000'})
        assert r.status_code == 200, r.text

    def get(self, path, status=200):
        r = self.client.get('/api/v1' + path)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def test_every_list_walks_to_the_same_rows_as_one_page(self):
        for path, key, ident in (('/nodes', 'nodes', 'id'), ('/models', 'targets', 'target'),
                                 ('/agents', 'agents', 'id'), ('/configuration/resources', 'resources', 'id')):
            whole = self.get(f'{path}?limit=200')
            self.assertIsNone(whole['next_cursor'], path)
            ids = [row[ident] for row in whole[key]]
            self.assertEqual(ids, sorted(ids), path)
            self.assertEqual(len(ids), len(set(ids)), path)
            snapshot = whole['snapshot']
            self.assertEqual((snapshot['ttl_s'], snapshot['stale'], snapshot['error']), (30, False, None), path)
            walked, cursor = [], None
            for _ in range(200):
                body = self.get(f'{path}?limit=2' + (f'&cursor={cursor}' if cursor else ''))
                walked += [row[ident] for row in body[key]]
                cursor = body['next_cursor']
                if cursor is None:
                    break
            self.assertEqual(walked, ids, path)

    def test_lists_and_details_are_served_from_one_build(self):
        build, row_id = api.SNAPSHOTS['configuration']
        calls = []

        def counted(root):
            calls.append(root)
            return build(root)

        with patch.dict(api.SNAPSHOTS, {'configuration': (counted, row_id)}):
            body = self.get('/configuration/resources?limit=200')
            self.assertTrue(body['route'] and body['references'] is not None)
            first = body['resources'][0]['id']
            self.assertEqual(self.get(f'/configuration/resources/{first}')['id'], first)
            self.get('/configuration/resources?limit=5')
            self.get('/configuration/resources/profile/not-declared', 404)
        self.assertEqual(len(calls), 1)

    def test_model_current_comes_from_the_same_snapshot(self):
        body = self.get('/models?limit=200')
        self.assertIn(body['current'], [row['target'] for row in body['targets']] + [body['current']])
        current = [row for row in body['targets'] if row['current']]
        self.assertTrue(all(row['target'] == body['current'] for row in current))

    def test_node_probe_is_attached_per_request_not_cached(self):
        node_ids = [row['id'] for row in self.get('/nodes?limit=200')['nodes']]
        first, second = node_ids[0], node_ids[1]
        stamp = datetime.datetime(2026, 11, 13, 0, 0, tzinfo=datetime.timezone.utc).isoformat()
        sentinel = {'data': {'system': 'Linux', 'release': 'r', 'machine': 'm', 'source': 'linux-proc-v1',
                             'collected_at': stamp},
                    'job_id': 'job', 'attempt': 1, 'recorded_at': stamp, 'quality': 'fresh'}
        ledger = importlib.import_module('agentiac.operations.ledger')
        with patch.object(ledger.Ledger, 'probe_snapshot', lambda self, node: sentinel if node == first else None):
            nodes = {row['id']: row for row in self.get('/nodes?limit=200')['nodes']}
            self.assertEqual(nodes[first]['probe']['job_id'], 'job')
            self.assertIsNone(nodes[second]['probe'])
            self.assertEqual(self.get(f'/nodes/{first}')['probe']['job_id'], 'job')

    def test_bad_cursor_and_limit_are_schema_errors(self):
        cursor = self.get('/nodes?limit=1')['next_cursor']
        self.assertIsNotNone(cursor)
        for path in ('/nodes?cursor=1', '/nodes?cursor=%25%25', f'/models?cursor={cursor}',
                     '/agents?limit=0', '/agents?limit=201'):
            body = self.get(path, 422)
            self.assertEqual(body['code'], 'schema_error', path)

    def test_unreadable_declarations_are_503_and_overview_reports_freshness(self):
        def broken(root):
            raise OSError('unreadable')

        with patch.dict(api.SNAPSHOTS, {'agents': (broken, api.SNAPSHOTS['agents'][1])}), \
                self.assertLogs('agentiac.management.cache', 'ERROR'):
            body = self.get('/agents', 503)
        self.assertEqual((body['code'], body['retryable']), ('snapshot_unavailable', True))
        self.assertNotIn('unreadable', str(body))
        self.assertEqual(self.get('/overview')['freshness']['declared'], 'no snapshot read yet')
        self.get('/agents')
        freshness = self.get('/overview')['freshness']
        self.assertNotIn('declared', freshness)
        self.assertRegex(freshness['agents'], r'^snapshot \d+s old, refreshed after 30s$')


if __name__ == '__main__':
    unittest.main()
