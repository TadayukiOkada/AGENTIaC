"""Deployment records for the dashboard: control deploy, route and dashboard plan/apply history."""
import datetime
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from agentiac import paths  # noqa: E402
from agentiac.management import deployments  # noqa: E402

SITE = paths.root()


def stamp(hour, minute=0):
    return datetime.datetime(2026, 11, 13, hour, minute, tzinfo=datetime.timezone.utc).isoformat()


def git(cwd, *argv):
    return subprocess.run(['git', '-C', str(cwd), *argv], check=True, capture_output=True, text=True,
                          env={**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
                               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}).stdout.strip()


class GitHeadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', 'one')

    def test_loose_packed_detached_and_worktree_heads_match_git(self):
        self.assertEqual(deployments.git_head(self.repo), git(self.repo, 'rev-parse', 'HEAD'))
        git(self.repo, 'pack-refs', '--all')
        self.assertFalse((self.repo / '.git/refs/heads/main').exists())
        self.assertEqual(deployments.git_head(self.repo), git(self.repo, 'rev-parse', 'HEAD'))
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', 'two')
        worktree = Path(self.tmp.name) / 'wt'
        git(self.repo, 'worktree', 'add', '-q', '-b', 'side', str(worktree))
        git(worktree, 'commit', '-q', '--allow-empty', '-m', 'three')
        self.assertEqual(deployments.git_head(worktree), git(worktree, 'rev-parse', 'HEAD'))
        self.assertNotEqual(deployments.git_head(worktree), deployments.git_head(self.repo))
        git(self.repo, 'checkout', '-q', '--detach')
        self.assertEqual(deployments.git_head(self.repo), git(self.repo, 'rev-parse', 'HEAD'))

    def test_unreadable_head_is_none(self):
        self.assertIsNone(deployments.git_head(Path(self.tmp.name) / 'missing'))
        (self.repo / '.git/HEAD').write_text('ref: refs/heads/nowhere\n')
        self.assertIsNone(deployments.git_head(self.repo))


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    def test_control_compares_the_deployed_revision_with_head(self):
        self.assertEqual(deployments.control(self.root),
                         {'revision': None, 'deployed_at': None, 'dirty': None, 'head': None, 'up_to_date': None})
        self.write('artifacts/deployed.json', {'revision': 'a' * 40, 'dirty': False, 'time': 't'})
        with patch.object(deployments, 'git_head', return_value='a' * 40):
            self.assertTrue(deployments.control(self.root)['up_to_date'])
        with patch.object(deployments, 'git_head', return_value='b' * 40):
            self.assertFalse(deployments.control(self.root)['up_to_date'])
        with patch.object(deployments, 'git_head', return_value=None):
            self.assertIsNone(deployments.control(self.root)['up_to_date'])

    def test_route_changes_newest_first_bounded_and_tolerant(self):
        self.write('gateway/local-coder.json', {'default': 'd'})
        lines = [json.dumps({'time': f't{n}', 'from': f'a{n}', 'to': f'b{n}'}) for n in range(30)]
        self.write('artifacts/gateway/route-log.jsonl', '\n'.join(lines[:10] + ['not json', '[]'] + lines[10:]) + '\n')
        state = deployments.route(self.root, 'b29')
        self.assertEqual((state['current'], state['declared_default']), ('b29', 'd'))
        self.assertEqual(len(state['changes']), deployments.ROUTE_CHANGES)
        self.assertEqual(state['changes'][0], {'time': 't29', 'from_target': 'a29', 'to_target': 'b29'})
        self.assertEqual(deployments.route(Path(self.tmp.name) / 'none', None)['changes'], [])

    def history(self):
        history = self.root / 'history'
        release = self.root / 'releases/20261113-1'
        self.write('releases/20261113-1/release.json', {'id': 'r1', 'revision': 'c' * 40})
        unit = {'text': '[Service]\nExecStart=/secret/path\n', 'active': True, 'enabled': True}
        config = {'release': str(release), 'release_id': 'r1', 'port': 8043, 'ssh_tunnel': True, 'cert': None,
                  'targets': ['worker-a', 'worker-b'], 'tls_files': {'/k': 'fingerprint'}}
        old = {'action': 'activate', 'id': '1' * 32, 'plan_hash': 'h1', 'config': {**config, 'release': '/gone/old'},
               'desired': {'u': unit}, 'before': {'u': unit}}
        new = {'action': 'activate', 'id': '2' * 32, 'plan_hash': 'h2', 'config': config,
               'previous_config': {'release': '/gone/old'}, 'desired': {'u': unit}, 'before': {'u': unit}}
        self.write(f'history/{"1" * 32}.json', old)
        self.write(f'history/{"2" * 32}.json', new)
        self.write('history/current.json', new)
        self.write(f'history/failed-{"3" * 32}.json', {'plan': {**old, 'id': '3' * 32}, 'plan_hash': 'h3'})
        self.write(f'history/{"4" * 32}-stop.json', {'plan': {'action': 'stop', 'desired': {}}, 'after': {}})
        self.write('history/notes.json', {'id': 'x'})
        self.write(f'history/{"5" * 32}.json', 'not json')
        for plan_id, created in (('1' * 32, stamp(1, 0)), ('2' * 32, stamp(3, 0)), ('3' * 32, stamp(0, 30))):
            self.write(f'history/backups/{plan_id}/manifest.json', {'created_at': created})
        stop_time = datetime.datetime(2026, 11, 13, tzinfo=datetime.timezone.utc).timestamp()  # no snapshot
        os.utime(history / f'{"4" * 32}-stop.json', (stop_time, stop_time))
        return history

    def test_dashboard_history_statuses_order_and_fields(self):
        report = deployments.dashboard(self.history())
        self.assertEqual(report['current'], '2' * 32)
        entries = report['entries']
        self.assertEqual([(e['id'][0], e['status']) for e in entries],
                         [('2', 'current'), ('1', 'superseded'), ('3', 'failed'), ('4', 'emergency stop')])
        current = entries[0]
        self.assertEqual((current['release'], current['revision'], current['previous_release'], current['port'],
                          current['transport'], current['probe_targets'], current['enabled_at_login'],
                          current['plan_hash'], current['applied_at']),
                         ('20261113-1', 'c' * 40, 'old', 8043, 'ssh-tunnel', ['worker-a', 'worker-b'], True, 'h2',
                          stamp(3, 0)))
        self.assertEqual((entries[1]['release'], entries[1]['revision']), ('old', None))
        self.assertEqual(entries[3]['applied_at'], stamp(0, 0))

    def test_allowlist_keeps_unit_text_paths_and_tls_out(self):
        text = json.dumps(deployments.dashboard(self.history()))
        for secret in ('ExecStart', '/secret/path', 'fingerprint', str(self.root)):
            self.assertNotIn(secret, text)

    def test_missing_history_is_empty(self):
        self.assertEqual(deployments.dashboard(self.root / 'none'), {'current': None, 'entries': []})


try:
    from fastapi.testclient import TestClient
    api = importlib.import_module('agentiac.management.app')
    session_module = importlib.import_module('agentiac.management.session')
except ModuleNotFoundError as error:
    if error.name not in {'fastapi', 'starlette', 'httpx', 'pydantic'}:
        raise
    api = None


@unittest.skipIf(api is None, 'management API dependencies are not installed')
class DeploymentsAPITest(RecordsTest):
    def setUp(self):
        super().setUp()
        self.addCleanup(patch.stopall)
        ledger = importlib.import_module('agentiac.operations.ledger')
        patch.object(ledger, 'DB_PATH', self.root / 'operations.sqlite').start()
        patch.object(ledger, 'LOG_DIR', self.root / 'jobs').start()
        patch.object(api, 'SESSION_PATH', self.root / 'session.sqlite').start()
        patch.object(api, 'DEPLOYMENT_HISTORY', self.history()).start()
        session_module.SessionStore(api.SESSION_PATH).initialize('test-owner-password')
        api.SNAPSHOT_CACHE.clear()
        self.addCleanup(api.SNAPSHOT_CACHE.clear)
        self.client = TestClient(api.app, base_url='http://127.0.0.1:8000')

    def login(self):
        r = self.client.post('/api/v1/session', json={'password': 'test-owner-password'},
                             headers={'Origin': 'http://127.0.0.1:8000'})
        self.assertEqual(r.status_code, 200, r.text)

    def test_deployments_endpoint(self):
        self.assertEqual(self.client.get('/api/v1/deployments').status_code, 401)
        self.login()
        body = self.client.get('/api/v1/deployments').json()
        self.assertEqual(body['dashboard_current'], '2' * 32)
        self.assertEqual([e['status'] for e in body['dashboard']], ['current', 'superseded', 'failed', 'emergency stop'])
        # Control and route read the real checkout: HEAD agrees with git when present, the route with the models list.
        expected_head = git(SITE, 'rev-parse', 'HEAD') if (SITE / '.git').exists() else None
        self.assertEqual(body['control']['head'], expected_head)
        self.assertEqual(body['route']['current'], self.client.get('/api/v1/models?limit=1').json()['current'])
        self.assertEqual(self.client.get(f'/api/v1/deployments/{"1" * 32}').json()['status'], 'superseded')
        r = self.client.get('/api/v1/deployments/' + '9' * 32)
        self.assertEqual((r.status_code, r.json()['code']), (404, 'unknown_deployment'))
        self.assertNotIn('ExecStart', self.client.get('/api/v1/deployments').text)


if __name__ == '__main__':
    unittest.main()
