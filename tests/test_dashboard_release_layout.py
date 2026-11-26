"""The API must work from an immutable release, which ships only Python code, assets and the lock.

A module that reads a non-Python file next to itself works in the checkout but fails in a release. This runs every
read endpoint from a copy of exactly the release's code files, with declarations in the configured root.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    import fastapi  # noqa: F401
    import httpx  # noqa: F401
except ModuleNotFoundError as error:
    raise unittest.SkipTest(f'dashboard dependencies are not installed: {error}')

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))

from agentiac import paths  # noqa: E402
from agentiac.management import releases  # noqa: E402

SITE = paths.root()          # the declarations a release reads; REPO is the code it is built from

SWEEP = r'''
import json, sys
from pathlib import Path
from fastapi.testclient import TestClient
from agentiac.management import app as api
from agentiac.management.session import SessionStore
from agentiac.operations.ledger import Ledger
state = Path(sys.argv[1])
SessionStore(state / 'session.sqlite').initialize('release-layout-password')
Ledger(api.ROOT, path=state / 'operations.sqlite', log_dir=state / 'jobs').close()
client = TestClient(api.app, base_url='http://127.0.0.1:8000')
origin = {'Origin': 'http://127.0.0.1:8000'}
assert client.post('/api/v1/session', json={'password': 'release-layout-password'}, headers=origin).status_code == 200
statuses, bodies = {}, {}
for path in ('/api/v1/capabilities', '/api/v1/overview', '/api/v1/nodes?limit=200', '/api/v1/models?limit=200',
             '/api/v1/agents?limit=200', '/api/v1/engines?limit=200', '/api/v1/configuration/resources?limit=200',
             '/api/v1/configuration/history', '/api/v1/jobs?limit=100&order=desc'):
    response = client.get(path)
    statuses[path] = response.status_code
    bodies[path] = response.json() if response.status_code == 200 else None
engine_rows = (bodies.get('/api/v1/engines?limit=200') or {}).get('engines', [])
assert engine_rows and all(row.get('lifecycle') for row in engine_rows), 'lifecycle missing from release API'
for listing, field, prefix in (('/api/v1/nodes?limit=200', 'nodes', '/api/v1/nodes/'),
                               ('/api/v1/models?limit=200', 'targets', '/api/v1/models/'),
                               ('/api/v1/agents?limit=200', 'agents', '/api/v1/agents/'),
                               ('/api/v1/engines?limit=200', 'engines', '/api/v1/engines/'),
                               ('/api/v1/configuration/resources?limit=200', 'resources',
                                '/api/v1/configuration/resources/')):
    rows = (bodies[listing] or {}).get(field) or []
    statuses[prefix + '*'] = len(rows)
    if rows:
        path = prefix + (rows[0].get('id') or rows[0].get('target'))
        statuses[path] = client.get(path).status_code
print(json.dumps(statuses))
'''


class ReleaseLayoutTests(unittest.TestCase):
    def test_module_relative_schemas_ship_with_the_code(self):
        shipped = {path.relative_to(REPO).as_posix() for path in releases.code_files(REPO)}
        self.assertTrue({'infra/components.schema.json', 'infra/retention.schema.json', 'infra/backend-lifecycle.schema.json'} <= shipped)
        self.assertFalse(any(rel.startswith(('infra/inventory', 'profiles/', 'gateway/', 'secrets'))
                             for rel in shipped), 'declarations must stay in the configured checkout')

    @unittest.skipUnless((REPO / 'dashboard/dist/index.html').is_file(), 'build the dashboard UI to test a prepared release')
    def test_prepared_release_serves_lifecycle_from_isolated_declarations(self):
        # Build the actual immutable layout with an isolated declaration root and session in /tmp.
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root, release, state = base / 'declarations', base / 'release', base / 'state'
            state.mkdir(mode=0o700)
            shutil.copytree(REPO / 'examples/site', root, ignore=shutil.ignore_patterns('__pycache__', 'node_modules'))
            receipt = releases.prepare(REPO, release)
            self.assertIsInstance(receipt['uncommitted'], bool)
            self.assertIn('infra/backend-lifecycle.schema.json', receipt['files'])
            self.assertEqual(releases.verify(release)['id'], receipt['id'])
            env = {key: value for key, value in os.environ.items() if not key.startswith('AGENTIAC_')}
            env.update(PYTHONPATH=str(release / 'src'), AGENTIAC_MANAGEMENT_ROOT=str(root),
                       AGENTIAC_MANAGEMENT_STATE=str(state), PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-c', SWEEP, str(state)], cwd=release, env=env,
                                    capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr[-4000:])
            statuses = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(statuses['/api/v1/engines?limit=200'], 200)
            self.assertGreater(statuses['/api/v1/engines/*'], 0)

    def test_every_read_endpoint_works_from_release_code_files_only(self):
        with tempfile.TemporaryDirectory() as folder:
            release, state = Path(folder) / 'release', Path(folder) / 'state'
            state.mkdir(mode=0o700)
            for path in releases.code_files(REPO):
                if 'dist' in path.relative_to(REPO).parts:
                    continue  # built UI assets are irrelevant to API reads and absent before a build
                target = release / path.relative_to(REPO)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            env = {key: value for key, value in os.environ.items() if not key.startswith('AGENTIAC_')}
            env.update(PYTHONPATH=str(release / 'src'), AGENTIAC_MANAGEMENT_ROOT=str(SITE),
                       AGENTIAC_MANAGEMENT_STATE=str(state), PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-c', SWEEP, str(state)], cwd=release, env=env,
                                    capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr[-4000:])
            statuses = json.loads(result.stdout.strip().splitlines()[-1])
        failures = {path: code for path, code in statuses.items() if not path.endswith('*') and code != 200}
        self.assertEqual(failures, {}, statuses)
        required = ['/api/v1/nodes/*', '/api/v1/models/*',
                    '/api/v1/configuration/resources/*', '/api/v1/engines/*']
        if (SITE / 'agents/agents.yml').is_file():
            required.append('/api/v1/agents/*')
        else:
            self.assertEqual(statuses['/api/v1/agents/*'], 0)
        for listing in required:
            self.assertGreater(statuses[listing], 0, f'{listing} returned no rows from the release layout')


if __name__ == '__main__':
    unittest.main()
