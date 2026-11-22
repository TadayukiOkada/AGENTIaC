"""Offline credential/snapshot/release/user-unit tests; never touches real systemd or SSH."""
from contextlib import closing, ExitStack
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))

from agentiac import backup as set_b  # noqa: E402
from agentiac import paths  # noqa: E402
from agentiac.management import snapshots, releases, deployment, admin  # noqa: E402
from agentiac.management.lifecycle import StateLock  # noqa: E402
from agentiac.management.paths import require_complete  # noqa: E402
from agentiac.management.session import SessionStore  # noqa: E402
from agentiac.operations.ledger import Ledger  # noqa: E402

SITE = paths.root()


class FakeSystemctl:
    def __init__(self, unit_dir=None):
        self.states = {name: {'active': False, 'enabled': False} for name in releases.UNITS}
        self.calls = []
        self.fail_next_start = False
        self.unit_dir = unit_dir

    def loaded(self, name):
        # Like the real user manager: a unit without a unit file is unknown (approximation:
        # loaded state follows the file without modelling daemon-reload timing).
        return self.unit_dir is None or (Path(self.unit_dir) / name).exists() or self.states[name]['active']

    def state(self, name):
        assert name in releases.UNITS
        return dict(self.states[name])

    def call(self, verb, *names):
        assert verb in {'daemon-reload', 'stop', 'disable', 'enable', 'start'}
        self.calls.append((verb, *names))
        for name in names:
            assert name in releases.UNITS
            if verb in ('stop', 'disable') and not self.loaded(name):
                raise subprocess.CalledProcessError(5, ['systemctl', '--user', verb, name])
            if verb == 'start' and self.fail_next_start:
                self.fail_next_start = False
                raise ValueError('injected user-service failure')
            if verb in ('start', 'stop'):
                self.states[name]['active'] = verb == 'start'
            if verb in ('enable', 'disable'):
                self.states[name]['enabled'] = verb == 'enable'


class DashboardOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / 'state'
        self.state.mkdir(mode=0o700)
        self.sessions = SessionStore(self.state / 'session.sqlite')
        self.sessions.initialize('initial-owner-password')
        self.ledger = Ledger(REPO, path=self.state / 'operations.sqlite', log_dir=self.state / 'jobs')
        self.addCleanup(self.ledger.close)
        self.units = self.base / 'units'
        self.manager = FakeSystemctl(self.units)
        self.history = self.base / 'history'

    def snapshot(self):
        return snapshots.backup(self.state, self.base / 'snapshot')

    def plan(self, **kwargs):
        arguments = dict(release=self.base / 'release', root=SITE, state=self.state,
                         unit_dir=self.units, history=self.history, ssh_tunnel=True,
                         allow_uncommitted=True, manager=self.manager)
        arguments.update(kwargs)
        return deployment.create_plan(**arguments)

    def fake_release(self):
        stack = ExitStack()
        stack.enter_context(patch.object(releases, 'check_state'))
        stack.enter_context(patch.object(releases, 'verify', return_value={'id': 'frozen-release', 'uncommitted': False}))
        return stack

    def test_legacy_credential_and_cookie_state_is_excluded_from_set_b(self):
        self.assertTrue(set_b.excluded('management/'))
        self.assertTrue(set_b.excluded('management/session.sqlite'))
        self.assertTrue(set_b.excluded('management/session.sqlite-wal'))
        self.assertFalse(set_b.excluded('management-validation/observations.json'))
        self.assertFalse(set_b.excluded('suite/runs/trace.jsonl'))

    def test_snapshot_preserves_attempts_but_revokes_sessions_and_readiness(self):
        login = self.sessions.login('initial-owner-password')
        self.ledger.publish_worker('fixture', ['worker-a'])
        self.ledger.conn.execute("INSERT INTO events VALUES ('history',1,1,'retained','old attempt retained')")
        self.ledger.conn.commit()
        self.snapshot()
        manifest = snapshots.verify(self.base / 'snapshot')
        self.assertEqual(set(manifest['databases']), set(snapshots.DATABASES))
        self.assertNotIn('initial-owner-password', json.dumps(manifest))
        destination = self.base / 'restored'
        snapshots.restore(self.base / 'snapshot', destination)
        restored_sessions = SessionStore(destination / 'session.sqlite')
        self.assertIsNone(restored_sessions.read(login['session_id']))
        restored_sessions.login('initial-owner-password')
        from agentiac.management.paths import require_ready
        with self.assertRaises(ValueError):
            require_ready(destination)
        restored = Ledger(REPO, path=destination / 'operations.sqlite', log_dir=destination / 'jobs')
        self.addCleanup(restored.close)
        self.assertFalse(restored.available_targets())
        self.assertEqual(restored.conn.execute('SELECT message FROM events').fetchone()[0], 'old attempt retained')
        self.assertIsNotNone(self.sessions.read(login['session_id']))

    def test_shared_runtime_locks_allow_api_worker_but_exclude_maintenance(self):
        with StateLock(self.state), StateLock(self.state):
            with self.assertRaises(ValueError):
                self.snapshot()
        with StateLock(self.state, exclusive=True):
            with self.assertRaises(ValueError):
                with StateLock(self.state):
                    pass
        self.snapshot()

    def test_snapshots_never_overwrite_existing_or_export_private_state_to_artifacts(self):
        self.snapshot()
        original = snapshots.digest(self.base / 'snapshot/manifest.json')
        with self.assertRaises(FileExistsError):
            self.snapshot()
        self.assertEqual(snapshots.digest(self.base / 'snapshot/manifest.json'), original)
        with self.assertRaises(ValueError):
            snapshots.backup(self.state, SITE / 'artifacts/credential-backup')
        with self.assertRaises(ValueError):
            snapshots.restore(self.base / 'snapshot', SITE / 'artifacts/restored-state')
        with self.assertRaises(FileExistsError):
            snapshots.restore(self.base / 'snapshot', self.state)

    def test_tampered_or_symlinked_snapshot_is_rejected(self):
        self.snapshot()
        manifest = self.base / 'snapshot/manifest.json'
        original = manifest.read_text()
        data = json.loads(original)
        data['databases']['../../escape'] = data['databases'].pop('session.sqlite')
        manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            snapshots.verify(manifest.parent)
        manifest.write_text(original)
        file = manifest.parent / 'session.sqlite'
        moved = self.base / 'stolen.sqlite'
        file.rename(moved)
        file.symlink_to(moved)
        with self.assertRaises(ValueError):
            snapshots.verify(manifest.parent)

    def test_unknown_state_files_or_new_schema_are_not_silently_omitted(self):
        (self.state / 'untracked-important-file').write_text('keep')
        with self.assertRaises(ValueError):
            self.snapshot()
        (self.state / 'untracked-important-file').unlink()
        self.ledger.conn.execute('CREATE TABLE future_schema (value TEXT)')
        self.ledger.conn.commit()
        with self.assertRaises(ValueError):
            self.snapshot()

    def test_interrupted_restore_stays_visibly_incomplete_and_cannot_start(self):
        self.snapshot()
        destination = self.base / 'partial'
        original = snapshots.validate_database

        def fail_second(path, expected):
            if Path(path) == destination / 'operations.sqlite':
                raise OSError('injected disk failure')
            return original(path, expected)
        with patch.object(snapshots, 'validate_database', side_effect=fail_second):
            with self.assertRaises(OSError):
                snapshots.restore(self.base / 'snapshot', destination)
        with self.assertRaises(ValueError):
            require_complete(destination)
        self.assertTrue((destination / '.incomplete').exists())

    def test_password_rotation_revokes_existing_sessions_and_recovery_is_explicit(self):
        login = self.sessions.login('initial-owner-password')
        with self.assertRaises(ValueError):
            self.sessions.change_password('changed-owner-password', old_password='wrong')
        self.assertIsNotNone(self.sessions.read(login['session_id']))
        self.sessions.change_password('changed-owner-password', old_password='initial-owner-password')
        self.assertIsNone(self.sessions.read(login['session_id']))
        self.sessions.login('changed-owner-password')
        with closing(self.sessions._connect()) as conn, conn:
            conn.execute("UPDATE credentials SET password_hash=?", (hashlib.sha256(b'old').hexdigest(),))
        self.assertFalse(self.sessions.initialized())
        self.sessions.change_password('recovered-owner-password', recover=True)
        self.sessions.login('recovered-owner-password')

    def test_owner_cli_requires_snapshot_and_local_confirmation_before_recovery(self):
        with self.assertRaises(SystemExit):
            admin.main(['--state-dir', str(self.state), 'owner', 'recover'])
        destination = self.base / 'before-recovery'
        with self.assertRaises(SystemExit):
            admin.main(['--state-dir', str(self.state), 'owner', 'recover', '--backup', str(destination)])
        with patch.object(admin, 'getpass', side_effect=['recovered-owner-password', 'recovered-owner-password']):
            admin.main(['--state-dir', str(self.state), 'owner', 'recover', '--backup', str(destination), '--confirm-owner', 'owner'])
        snapshots.verify(destination)
        self.sessions.login('recovered-owner-password')

    def test_release_copies_only_code_assets_and_lock_and_detects_mutation(self):
        release = self.base / 'release'
        source = self.base / 'source'
        (source / 'src/agentiac').mkdir(parents=True)
        (source / 'src/agentiac/__init__.py').write_text('# source fixture\n')
        (source / 'dashboard/dist').mkdir(parents=True)
        (source / 'dashboard/dist/index.html').write_text('<html>fixture</html>')
        (source / 'requirements-dashboard.lock').write_text('# no dependencies for content fixture\n')
        receipt = releases.prepare(source, release)
        self.assertEqual(releases.verify(release)['id'], receipt['id'])
        self.assertFalse((release / 'private').exists())
        self.assertFalse((release / 'artifacts').exists())
        self.assertFalse((release / '.git').exists())
        file = release / 'dashboard/dist/index.html'
        file.chmod(0o600)
        file.write_text('changed')
        with self.assertRaises(ValueError):
            releases.verify(release)

    def test_service_plans_default_to_no_probe_worker_and_bind_exact_approval(self):
        with self.fake_release():
            plan = self.plan()
            self.assertIsNone(plan['desired'][releases.UNITS[1]]['text'])
            with self.assertRaises(ValueError):
                deployment.apply(plan, 'wrong-approval', self.manager, check_health=lambda config: None)
            self.assertFalse(self.manager.calls)
            result = deployment.apply(plan, deployment.plan_hash(plan), self.manager, check_health=lambda config: None)
        self.assertTrue(result['applied'])
        self.assertTrue(self.manager.state(releases.UNITS[0])['active'])
        self.assertFalse(self.manager.state(releases.UNITS[1])['active'])
        snapshots.verify(self.history / 'backups' / plan['id'])

    def test_failed_upgrade_compensates_units_without_resetting_database(self):
        with self.fake_release():
            first = self.plan(enable=True)
            deployment.apply(first, deployment.plan_hash(first), self.manager, check_health=lambda config: None)
            old = (self.units / releases.UNITS[0]).read_text()
            self.ledger.conn.execute("INSERT INTO events VALUES ('history',1,1,'saved','do not reset')")
            self.ledger.conn.commit()
            upgrade = self.plan(port=8001, enable=True)
            self.manager.fail_next_start = True
            with self.assertRaisesRegex(ValueError, 'previous owned unit state restored'):
                deployment.apply(upgrade, deployment.plan_hash(upgrade), self.manager, check_health=lambda config: None)
        self.assertEqual((self.units / releases.UNITS[0]).read_text(), old)
        self.assertTrue(self.manager.state(releases.UNITS[0])['active'])
        self.assertTrue(self.manager.state(releases.UNITS[0])['enabled'])
        self.assertEqual(self.ledger.conn.execute('SELECT message FROM events').fetchone()[0], 'do not reset')
        self.assertFalse((self.history / '.incomplete').exists())

    def test_failed_first_install_removes_enabled_links_and_only_its_own_units(self):
        with self.fake_release():
            plan = self.plan(enable=True)
            with self.assertRaisesRegex(ValueError, 'previous owned unit state restored'):
                deployment.apply(plan, deployment.plan_hash(plan), self.manager,
                                 check_health=lambda config: (_ for _ in ()).throw(ValueError('bad HTTPS')))
        for name in releases.UNITS:
            self.assertFalse((self.units / name).exists())
            self.assertEqual(self.manager.state(name), {'active': False, 'enabled': False})

    def test_failure_before_install_compensates_without_stopping_unknown_units(self):
        with self.fake_release(), patch.object(snapshots, 'backup', side_effect=OSError('snapshot failed')):
            plan = self.plan()
            with self.assertRaisesRegex(ValueError, 'previous owned unit state restored'):
                deployment.apply(plan, deployment.plan_hash(plan), self.manager, check_health=lambda config: None)
        self.assertFalse((self.history / '.incomplete').exists())
        self.assertNotIn('stop', [call[0] for call in self.manager.calls])
        for name in releases.UNITS:
            self.assertFalse((self.units / name).exists())

    def test_an_attempted_plan_is_refused_before_any_downtime(self):
        with self.fake_release():
            plan = self.plan()
            with self.assertRaisesRegex(ValueError, 'previous owned unit state restored'):
                deployment.apply(plan, deployment.plan_hash(plan), self.manager,
                                 check_health=lambda config: (_ for _ in ()).throw(ValueError('slow start')))
            calls = list(self.manager.calls)
            with self.assertRaisesRegex(ValueError, 'already attempted; create a new plan'):
                deployment.apply(plan, deployment.plan_hash(plan), self.manager, check_health=lambda config: None)
            self.assertEqual(self.manager.calls, calls)
            self.assertFalse((self.history / '.incomplete').exists())
            fresh = self.plan()
            self.assertTrue(deployment.apply(fresh, deployment.plan_hash(fresh), self.manager,
                                             check_health=lambda config: None)['applied'])

    def test_health_waits_by_elapsed_time_and_fails_fast_on_a_broken_contract(self):
        import http.server
        import socket
        import threading
        import time
        release = self.base / 'health-release'
        (release / 'dashboard/dist').mkdir(parents=True)
        (release / 'dashboard/dist/index.html').write_bytes(b'<!doctype html>fixture')

        def serve(session_status, delay):
            with socket.socket() as probe_socket:
                probe_socket.bind(('127.0.0.1', 0))
                port = probe_socket.getsockname()[1]

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path == '/api/v1/session':
                        body = json.dumps({'code': 'unauthorized'}).encode()
                        self.send_response(session_status)
                    else:
                        body = (release / 'dashboard/dist/index.html').read_bytes()
                        self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    pass

            servers = []

            def start():
                server = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
                servers.append(server)
                server.serve_forever()
            timer = threading.Timer(delay, start)
            timer.daemon = True
            timer.start()
            self.addCleanup(lambda: [server.shutdown() for server in servers])
            self.addCleanup(timer.cancel)
            return {'release': str(release), 'cert': None, 'ca': None, 'port': port}

        # A cold start slower than the old ~2 s attempt budget is accepted.
        started = time.monotonic()
        deployment.health(serve(401, 3.0), timeout_s=15)
        self.assertGreaterEqual(time.monotonic() - started, 3.0)
        # A service that answers but breaks the authentication contract fails without waiting.
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, 'post-check failed'):
            deployment.health(serve(200, 0), timeout_s=15)
        self.assertLess(time.monotonic() - started, 5)
        # Nothing ever answering fails at the deadline.
        with self.assertRaisesRegex(ValueError, 'post-check failed'):
            deployment.health(serve(401, 60), timeout_s=1)

    def test_explicit_stop_and_code_rollback_preserve_current_state(self):
        with self.fake_release():
            plan = self.plan(enable=True)
            deployment.apply(plan, deployment.plan_hash(plan), self.manager, check_health=lambda config: None)
            stop = self.plan(action='stop')
            deployment.apply(stop, deployment.plan_hash(stop), self.manager)
            self.assertFalse(self.manager.state(releases.UNITS[0])['active'])
            rollback = self.plan(action='rollback')
            deployment.apply(rollback, deployment.plan_hash(rollback), self.manager, check_health=lambda config: None)
            self.assertTrue(self.manager.state(releases.UNITS[0])['active'])

    def test_plan_drift_or_foreign_unit_refuses_before_service_operations(self):
        with self.fake_release():
            plan = self.plan()
            self.units.mkdir()
            (self.units / releases.UNITS[0]).write_text('[Service]\nExecStart=/bin/foreign\n')
            with self.assertRaises(ValueError):
                deployment.apply(plan, deployment.plan_hash(plan), self.manager)
        self.assertFalse(self.manager.calls)

    def test_unit_renderer_rejects_systemd_expansion_and_arbitrary_target_arguments(self):
        for path in ('/tmp/with space', '/tmp/%n', '/tmp/$HOME'):
            with self.assertRaises(ValueError):
                releases.render_units(path, REPO, self.state, 8000)
        with self.assertRaises(ValueError):
            releases.render_units('/tmp/release', REPO, self.state, 8000, targets=['worker-a; restart'])
        units = releases.render_units('/tmp/release', REPO, self.state, 8000, targets=['worker-a'])
        self.assertIn('--target worker-a', units[releases.UNITS[1]])
        self.assertIn('ProtectSystem=strict', units[releases.UNITS[0]])

    def test_unrecoverable_compensation_retains_intent_and_refuses_new_apply(self):
        with self.fake_release():
            plan = self.plan()
            with patch.object(deployment, 'install_units', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(ValueError, 'compensation needs operator attention'):
                    deployment.apply(plan, deployment.plan_hash(plan), self.manager)
            self.assertTrue((self.history / '.incomplete').exists())
            with self.assertRaisesRegex(ValueError, 'unresolved compensation'):
                deployment.apply(plan, deployment.plan_hash(plan), self.manager)

    def test_native_runtime_commands_delegate_without_changing_control_dependencies(self):
        runtime = self.base / '.venv-dashboard/bin/python'
        runtime.parent.mkdir(parents=True)
        runtime.touch()
        with patch.object(admin, 'SOURCE_ROOT', self.base), patch.object(admin.os, 'execve') as execute:
            admin.main(['--state-dir', str(self.state), 'serve', '--port', '8043'])
        program, argv, env = execute.call_args.args
        self.assertIn('.venv-dashboard/bin/python', program)
        self.assertEqual(argv[1:3], ['-m', 'agentiac.management.main'])
        self.assertEqual(env['AGENTIAC_MANAGEMENT_STATE'], str(self.state))
        self.assertNotIn('password', json.dumps(argv))

    def test_restored_state_requires_owner_review_and_an_incomplete_release_is_rejected(self):
        self.snapshot()
        restored = self.base / 'restored'
        snapshots.restore(self.base / 'snapshot', restored)
        from agentiac.management.paths import require_ready
        with self.assertRaises(ValueError):
            require_ready(restored)
        with patch.object(admin, 'getpass', side_effect=['reviewed-owner-password'] * 2):
            admin.main(['--state-dir', str(restored), 'owner', 'recover', '--backup',
                        str(self.base / 'restore-owner-backup'), '--confirm-owner', 'owner'])
        require_ready(restored)

    def test_incompatible_target_release_is_rejected_before_any_downtime(self):
        with self.fake_release():
            plan = self.plan()
            with patch.object(releases, 'check_state', side_effect=ValueError('incompatible schema')):
                with self.assertRaises(ValueError):
                    deployment.apply(plan, deployment.plan_hash(plan), self.manager)
        self.assertFalse(self.manager.calls)

    def test_systemctl_distinguishes_a_missing_unit_from_an_unavailable_manager(self):
        from types import SimpleNamespace
        with patch.object(deployment.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='not-found\n')):
            self.assertEqual(deployment.Systemctl().state(releases.UNITS[0]), {'active': False, 'enabled': False})
        with patch.object(deployment.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='')):
            with self.assertRaises(ValueError):
                deployment.Systemctl().state(releases.UNITS[0])

    def test_transitioning_or_masked_unit_is_reported_as_such(self):
        from types import SimpleNamespace
        for verb_output in ('activating', 'deactivating', 'masked'):
            outputs = iter([SimpleNamespace(returncode=0, stdout='loaded\n'),
                            SimpleNamespace(returncode=3, stdout=verb_output + '\n'),
                            SimpleNamespace(returncode=1, stdout='masked\n')])
            with patch.object(deployment.subprocess, 'run', side_effect=lambda *a, **k: next(outputs)):
                with self.assertRaisesRegex(ValueError, 'transitioning or masked'):
                    deployment.Systemctl().state(releases.UNITS[0])

    def test_emergency_stop_plan_does_not_need_readable_tls_material(self):
        tls = self.base / 'tls'
        tls.mkdir()
        files = {name: tls / f'{name}.pem' for name in ('cert', 'key', 'ca')}
        for path in files.values():
            path.write_text('fixture\n')
        with self.fake_release(), patch.object(releases, 'validate_tls', return_value={'fixture': True}):
            activate = self.plan(ssh_tunnel=False, **{name: str(path) for name, path in files.items()})
            deployment.apply(activate, deployment.plan_hash(activate), self.manager, check_health=lambda config: None)
            files['key'].unlink()
            stop = self.plan(action='stop')
            deployment.apply(stop, deployment.plan_hash(stop), self.manager)
        self.assertFalse(self.manager.state(releases.UNITS[0])['active'])

    def test_missing_operations_database_is_not_silently_recreated_on_startup(self):
        self.ledger.close()
        (self.state / 'operations.sqlite').unlink()
        with self.assertRaises(ValueError):
            snapshots.validate_startup(self.state)
        self.assertFalse((self.state / 'operations.sqlite').exists())

    def test_emergency_stop_retains_unknown_intent_and_works_with_bad_state(self):
        with self.fake_release():
            activate = self.plan(enable=True)
            deployment.apply(activate, deployment.plan_hash(activate), self.manager, check_health=lambda config: None)
            intent = {'failure': 'unresolved schema change'}
            deployment.write_json(self.history / '.incomplete', intent)
            (self.state / '.incomplete').touch()
            stop = self.plan(action='stop', root=self.base / 'missing-declaration-root')
            result = deployment.apply(stop, deployment.plan_hash(stop), self.manager)
        self.assertTrue(result['stopped'])
        self.assertEqual(json.loads((self.history / '.incomplete').read_text()), intent)
        self.assertFalse(self.manager.state(releases.UNITS[0])['active'])
        self.assertFalse(self.manager.state(releases.UNITS[0])['enabled'])

    def test_normal_stop_does_not_require_ready_credentials(self):
        with self.fake_release():
            activate = self.plan()
            deployment.apply(activate, deployment.plan_hash(activate), self.manager, check_health=lambda config: None)
            (self.state / '.credential-review-required').touch()
            stop = self.plan(action='stop')
            deployment.apply(stop, deployment.plan_hash(stop), self.manager)
        self.assertFalse(self.manager.state(releases.UNITS[0])['active'])
