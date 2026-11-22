"""Actual verified TLS API in an isolated code release and private temporary state."""
import contextlib
from http.cookies import SimpleCookie
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

if importlib.util.find_spec('fastapi') is None:
    raise unittest.SkipTest('optional dashboard API dependencies are not installed')

from agentiac.management import releases, snapshots, tls
from agentiac.management.session import SessionStore
from agentiac.operations.ledger import Ledger

REPO = Path(__file__).resolve().parents[1]


class DashboardTLSTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / 'state'
        self.state.mkdir(mode=0o700)
        self.store = SessionStore(self.state / 'session.sqlite')
        self.store.initialize('tls-fixture-password')
        ledger = Ledger(REPO, path=self.state / 'operations.sqlite', log_dir=self.state / 'jobs')
        ledger.close()
        self.cert, self.key = self.base / 'localhost.crt', self.base / 'localhost.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(self.key), '-out', str(self.cert), '-days', '1',
                        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10)
        self.key.chmod(0o600)
        source = self.base / 'source'
        shutil.copytree(REPO / 'src', source / 'src', ignore=shutil.ignore_patterns('__pycache__'))
        (source / 'dashboard/dist').mkdir(parents=True)
        (source / 'dashboard/dist/index.html').write_text('<html>immutable TLS release fixture</html>')
        shutil.copyfile(REPO / 'requirements-dashboard.lock', source / 'requirements-dashboard.lock')
        self.release = self.base / 'release'
        releases.prepare(source, self.release)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.url = 'https://127.0.0.1:' + str(self.port)
        self.process = None

    def tearDown(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.communicate(timeout=3)

    def start(self, state=None):
        env = {**os.environ, 'PYTHONPATH': str(self.release / 'src'),
               'AGENTIAC_MANAGEMENT_ROOT': str(REPO),
               'AGENTIAC_MANAGEMENT_STATE': str(state or self.state)}
        self.process = subprocess.Popen([sys.executable, '-m', 'agentiac.management.main',
                       '--host', '127.0.0.1', '--port', str(self.port),
                       '--tls-cert', str(self.cert), '--tls-key', str(self.key)],
                       cwd=self.release, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                       urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(self.cert))))
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if self.process.poll() is not None:
                self.fail('TLS API failed startup: ' + self.process.stderr.read().decode())
            try:
                opener.open(self.url + '/api/v1/session', timeout=0.2)
            except urllib.error.HTTPError as error:
                if error.code == 401:
                    error.read()
                    error.close()
                    return opener
            except (OSError, urllib.error.URLError):
                time.sleep(0.02)
        self.fail('TLS API did not become ready')

    def test_verified_https_secure_cookie_immutable_assets_and_live_backup_exclusion(self):
        opener = self.start()
        request = urllib.request.Request(self.url + '/api/v1/session',
                  data=json.dumps({'password': 'tls-fixture-password'}).encode(),
                  headers={'Content-Type': 'application/json', 'Origin': self.url})
        with opener.open(request, timeout=2) as response:
            cookie = SimpleCookie(response.headers['Set-Cookie'])['management_session']
            self.assertTrue(cookie['secure'])
            self.assertTrue(cookie['httponly'])
            self.assertNotIn('session_id', json.load(response))
        with opener.open(self.url + '/dashboard/', timeout=2) as response:
            self.assertEqual(response.read(), b'<html>immutable TLS release fixture</html>')
        with self.assertRaises(ValueError):
            snapshots.backup(self.state, self.base / 'live-backup')
        wrong = urllib.request.Request(self.url + '/api/v1/session', data=b'{"password":"tls-fixture-password"}',
                  headers={'Content-Type': 'application/json', 'Origin': 'https://localhost:1'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(wrong, timeout=2)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()
        self.process.terminate()
        self.process.communicate(timeout=8)
        snapshots.backup(self.state, self.base / 'offline-backup')
        snapshots.verify(self.base / 'offline-backup')

    def test_tls_key_pair_permissions_and_untrusted_certificate_are_rejected(self):
        with self.assertRaises(ValueError):
            tls.validate_tls(self.cert, None)
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):
            tls.validate_tls(self.cert, self.key)
        self.key.chmod(0o600)
        self.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler())
        with self.assertRaises(urllib.error.URLError):
            opener.open(self.url + '/dashboard/', timeout=2)

    def test_restored_credentials_cannot_start_until_offline_rotation(self):
        snapshots.backup(self.state, self.base / 'snapshot')
        destination = self.base / 'restored'
        snapshots.restore(self.base / 'snapshot', destination)
        env = {**os.environ, 'PYTHONPATH': str(self.release / 'src'),
               'AGENTIAC_MANAGEMENT_ROOT': str(REPO),
               'AGENTIAC_MANAGEMENT_STATE': str(destination)}
        result = subprocess.run([sys.executable, '-m', 'agentiac.management.main',
                  '--port', str(self.port)], cwd=self.release, env=env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('offline owner rotation', result.stderr)
        from agentiac.management import admin
        from unittest.mock import patch
        with patch.object(admin, 'getpass', side_effect=['new-restored-owner-password'] * 2):
            admin.main(['--state-dir', str(destination), 'owner', 'recover', '--backup',
                        str(self.base / 'before-reset'), '--confirm-owner', 'owner'])
        self.assertFalse((destination / '.credential-review-required').exists())
        self.start(state=destination)
