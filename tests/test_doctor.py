"""T8.3: one read of everything a session start asks, and T8.2's contract for the commands it stands on."""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from agentiac import paths  # noqa: E402
SITE = paths.root()
from agentiac import command, declared, doctor, errors  # noqa: E402
import borrow  # noqa: E402
import drift as drift_script  # noqa: E402
import preflight as preflight_script  # noqa: E402
import storage as storage_script  # noqa: E402

HEALTHY = lambda: (True, 'ok, 22 aliases')  # noqa: E731


class FakeDrift:
    def __init__(self, *sections):
        self.parts = sections

    def sections(self, hosts):
        return self.parts


class FakePreflight:
    def __init__(self, **records):
        self.records = records

    def check_host(self, name, host):
        return {'host': name, **self.records[name]}


def report(drift=(), preflight=None, hosts=None, gateway=HEALTHY, root=ROOT):
    records = preflight or {'worker-a': {'ok': True, 'failed': [], 'free_gib': 100}}
    hosts = hosts or {name: {} for name in records}
    # The checkout's own dashboard record and user manager are live state: a down probes unit there must not
    # change what these tests count. A temporary ROOT has no record, so the real check runs there.
    live = (mock.patch.object(doctor, 'dashboard_probes_check',
                              lambda _: doctor.check('dashboard.probes', 'ok', 'not read in this test'))
            if Path(root) == ROOT else contextlib.nullcontext())
    with live:
        return doctor.summary(root, hosts, gateway, drift=FakeDrift(*drift), preflight=FakePreflight(**records),
                              hygiene_read=lambda: [doctor.check('hygiene', 'ok', 'not read in this test')])


def by_id(record):
    return {item['id']: item for item in record['checks']}


class ShapeTests(unittest.TestCase):
    def test_every_check_has_the_four_fields_and_a_declared_status(self):
        record = report([[('ok', 'gateway', 'running'), ('drift', 'route', 'down')]])
        self.assertEqual(record['schema_version'], 1)
        for item in record['checks']:
            self.assertEqual(set(item), {'id', 'status', 'evidence', 'next'})
            self.assertIn(item['status'], doctor.STATES)

    def test_problems_come_first(self):
        self.assertEqual(report([[('ok', 'a', 'x'), ('drift', 'route', 'down')]])['checks'][0]['status'], 'problem')

    def test_everything_pa_10_names_is_read(self):
        ids = set(by_id(report([[('ok', 'storage worker-c', 'x')]])))
        self.assertLessEqual({'gateway', 'git', 'preflight.worker-a', 'suite.leases', 'backup',
                              'drift.storage-worker-c'}, ids)

    def test_the_saved_audit_is_read(self):
        self.assertIn('audit', by_id(report()))

    def test_a_problem_says_what_to_run_next(self):
        self.assertEqual(by_id(report([[('drift', 'route', 'down')]]))['drift.route']['next'],
                         f'{errors.command()} reclaim')


class StateTests(unittest.TestCase):
    def test_a_host_nobody_could_reach_is_unknown_and_not_a_note(self):
        found = by_id(report([[('note', 'storage worker-d', 'host not reachable; disk not measured')]]))
        self.assertEqual(found['drift.storage-worker-d']['status'], 'unknown')

    def test_a_gateway_that_is_down_is_a_problem(self):
        record = report(gateway=lambda: (False, 'down (URLError)'))
        self.assertEqual(by_id(record)['gateway']['status'], 'problem')

    def test_a_host_that_fails_preflight_is_a_problem_and_an_unreachable_one_unknown(self):
        record = report(preflight={'worker-a': {'ok': False, 'failed': ['docker'], 'free_gib': 50},
                                   'worker-d': {'unreachable': 'timed out'}})
        self.assertEqual(by_id(record)['preflight.worker-a']['status'], 'problem')
        self.assertIn('docker', by_id(record)['preflight.worker-a']['evidence'])
        self.assertEqual(by_id(record)['preflight.worker-d']['status'], 'unknown')

    def test_low_disk_alone_names_itself(self):
        found = by_id(report(preflight={'worker-a': {'ok': False, 'failed': [], 'free_gib': 10}}))
        self.assertIn('10 GiB free', found['preflight.worker-a']['evidence'])

    def test_control_is_not_asked_over_ssh(self):
        record = report(preflight={'worker-a': {'ok': True, 'failed': [], 'free_gib': 100}},
                        hosts={'worker-a': {}, 'control': {'ansible_connection': 'local'}})
        self.assertNotIn('preflight.control', by_id(record))
        self.assertIn('preflight.worker-a', by_id(record))       # asked anyway, the fake raises and this line is lost


class ExitCodeTests(unittest.TestCase):
    def test_the_contract(self):
        for counts, code in (({'problem': 0, 'unknown': 0}, 0), ({'problem': 1, 'unknown': 1}, 1),
                             ({'problem': 0, 'unknown': 1}, 3)):
            self.assertEqual(doctor.exit_code({'summary': counts}), code, counts)


class SurvivalTests(unittest.TestCase):
    """The day a source breaks is the day this screen is needed."""

    def test_one_drift_section_failing_costs_that_section(self):
        def broken():
            yield ('ok', 'first', 'x')
            raise OSError('tunnel gone')

        found = by_id(report([broken(), [('ok', 'after', 'y')]]))
        self.assertEqual(found['drift.section-0']['status'], 'unknown')
        self.assertIn('tunnel gone', found['drift.section-0']['evidence'])
        self.assertEqual(found['drift.after']['status'], 'ok')

    def test_a_gateway_check_that_raises_is_unknown(self):
        def broken():
            raise OSError('no key file')

        self.assertEqual(by_id(report(gateway=broken))['gateway']['status'], 'unknown')

    def test_a_corrupt_suite_database_is_unknown_and_the_rest_still_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'artifacts/suite'
            path.mkdir(parents=True)
            (path / 'state.sqlite').write_bytes(b'not a database file, long enough to read a header' * 4)
            found = by_id(report(root=Path(directory)))
        self.assertEqual(found['suite.leases']['status'], 'unknown')
        self.assertEqual(found['backup']['status'], 'note')


class LeaseTests(unittest.TestCase):
    def database(self, directory, rows):
        path = Path(directory) / 'artifacts/suite'
        path.mkdir(parents=True)
        with sqlite3.connect(path / 'state.sqlite') as db:
            db.execute('CREATE TABLE cases (id TEXT, status TEXT, lease_until REAL)')
            db.executemany('INSERT INTO cases VALUES (?,?,?)', rows)

    def test_only_an_expired_running_case_counts(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as directory:
            self.database(directory, [('a', 'running', now - 60), ('b', 'running', now + 60), ('c', 'complete', now - 60)])
            found = doctor.lease_check(directory, now=now)
        self.assertEqual(found['status'], 'note')
        self.assertIn('1 cases', found['evidence'])

    def test_no_suite_is_ok(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(doctor.lease_check(directory)['status'], 'ok')


class DashboardProbeTargetTests(unittest.TestCase):
    UNIT = declared.resource('dashboard-probes.service')

    def write_deployment(self, root, targets, active=True, unit_targets=None):
        history = Path(root) / 'private/dashboard-deployment'
        unit_dir = Path(root) / 'units'
        history.mkdir(parents=True)
        unit_dir.mkdir(parents=True)
        flags = ' '.join(f'--target {t}' for t in (targets if unit_targets is None else unit_targets))
        text = f'[Service]\nExecStart=/venv/bin/python -m agentiac.operations.worker --serve {flags}\n'
        (unit_dir / self.UNIT).write_text(text)
        record = {'action': 'activate', 'unit_dir': str(unit_dir), 'config': {'targets': list(targets)},
                  'desired': {self.UNIT: {'active': True, 'enabled': True, 'text': text}},
                  'after': {self.UNIT: {'active': active, 'enabled': True, 'text': text}}}
        (history / 'current.json').write_text(json.dumps(record))

    def test_no_deployment_record_is_not_a_problem(self):
        with tempfile.TemporaryDirectory() as directory:
            found = doctor.dashboard_probes_check(directory, hosts={'worker-a': {}})
        self.assertEqual(found['status'], 'ok')
        self.assertIn('no dashboard deployment', found['evidence'])

    def test_all_declared_targets_are_ok(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_deployment(directory, ['control', 'worker-a', 'worker-b'])
            found = doctor.dashboard_probes_check(
                directory, hosts={'control': {}, 'worker-a': {}, 'worker-b': {}}, unit_state='active')
        self.assertEqual(found['status'], 'ok')
        self.assertIn('worker-b', found['evidence'])

    def test_a_retired_target_is_a_problem_naming_the_fix(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_deployment(directory, ['control', 'worker-a', 'worker-c'])
            found = doctor.dashboard_probes_check(
                directory, hosts={'control': {}, 'worker-a': {}, 'worker-b': {}}, unit_state='active')
        self.assertEqual(found['status'], 'problem')
        self.assertIn('worker-c', found['evidence'])
        self.assertIn('dashboard plan', found['next'])
        self.assertIn('dashboard apply', found['next'])

    def test_a_failed_or_inactive_probes_unit_is_a_problem(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_deployment(directory, ['control', 'worker-a'], active=False)
            found = doctor.dashboard_probes_check(
                directory, hosts={'control': {}, 'worker-a': {}}, unit_state='failed')
        self.assertEqual(found['status'], 'problem')
        self.assertIn('failed', found['evidence'])
        self.assertIn('systemctl --user status', found['next'])

    def test_probes_the_plan_turned_off_are_not_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_deployment(directory, ['worker-c'])
            path = Path(directory) / 'private/dashboard-deployment/current.json'
            record = json.loads(path.read_text())
            record['desired'][self.UNIT]['active'] = False
            path.write_text(json.dumps(record))
            found = doctor.dashboard_probes_check(directory, hosts={'worker-b': {}}, unit_state='inactive')
        self.assertEqual(found['status'], 'ok')
        self.assertIn('not enabled', found['evidence'])

    def test_the_user_manager_is_asked_even_when_the_unit_file_is_gone(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_deployment(directory, ['worker-a'])
            (Path(directory) / 'units' / self.UNIT).unlink()
            answer = mock.Mock(stdout='inactive\n', returncode=3)
            with mock.patch.object(doctor.subprocess, 'run', return_value=answer) as run:
                found = doctor.dashboard_probes_check(directory, hosts={'worker-a': {}})
        self.assertEqual(run.call_args.args[0], ['systemctl', '--user', 'is-active', self.UNIT])
        self.assertEqual(found['status'], 'problem')
        self.assertIn('inactive', found['evidence'])

    def test_without_a_user_manager_the_record_answers_and_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_deployment(directory, ['worker-a'])
            with mock.patch.object(doctor.subprocess, 'run', side_effect=OSError('no systemctl')):
                found = doctor.dashboard_probes_check(directory, hosts={'worker-a': {}})
        self.assertEqual(found['status'], 'ok')
        self.assertIn('as recorded', found['evidence'])

    def test_the_summary_carries_the_check(self):
        with tempfile.TemporaryDirectory() as directory:
            found = {item['id']: item for item in report([], root=directory)['checks']}
        self.assertEqual(found['dashboard.probes']['status'], 'ok')


class GitTests(unittest.TestCase):
    def test_it_only_reads(self):
        calls = []

        def fake_run(argv, **_):
            calls.append(argv)
            return mock.Mock(stdout='0\t0' if 'rev-list' in argv else 'main' if 'rev-parse' in argv else '')

        with mock.patch.object(doctor.subprocess, 'run', fake_run):
            found = doctor.git_check('/repo')
        self.assertEqual(found['status'], 'ok')
        self.assertFalse([argv for argv in calls if {'fetch', 'pull', 'push'} & set(argv)])

    def test_a_dirty_checkout_is_a_note(self):
        def fake_run(argv, **_):
            return mock.Mock(stdout='0\t2' if 'rev-list' in argv else 'main' if 'rev-parse' in argv else ' M a\n')

        with mock.patch.object(doctor.subprocess, 'run', fake_run):
            found = doctor.git_check('/repo')
        self.assertEqual(found['status'], 'note')
        self.assertIn('2 ahead', found['evidence'])


class NoLoadTests(unittest.TestCase):
    """PA §10: doctor loads no model, and that is a test and not a promise."""

    def test_the_real_drift_and_preflight_never_send_the_load_header(self):
        requests, commands = [], []

        def fake_urlopen(request, timeout=None):
            requests.append(request)
            raise OSError('nothing is listening in this test')

        def fake_run(argv, **_):
            commands.append(list(argv) if not isinstance(argv, str) else [argv])
            return mock.Mock(stdout='', stderr='', returncode=255)

        hosts = {name: {**host} for name, host in borrow.hosts().items()}
        with mock.patch('urllib.request.urlopen', fake_urlopen), mock.patch('subprocess.run', fake_run), \
                mock.patch.object(borrow, 'endpoint', return_value=(1, 'key')):
            record = doctor.summary(ROOT, hosts, HEALTHY, drift=drift_script, preflight=preflight_script)
        self.assertTrue(record['checks'])
        for request in requests:
            self.assertNotIn('x-ondemand-load', {name.lower() for name in request.headers})
            self.assertEqual(request.get_method(), 'GET')
            self.assertTrue(request.full_url.endswith('/ondemand/status'), request.full_url)
        for argv in commands:
            self.assertFalse({'claim', 'warm', 'reclaim', 'restart', 'start', 'stop'} & set(argv), argv)

    def test_none_of_the_sources_name_the_load_header(self):
        for name in ('src/agentiac/doctor.py', 'scripts/drift.py', 'scripts/preflight.py', 'scripts/storage.py'):
            self.assertNotIn('X-Ondemand-Load', (ROOT / name).read_text(), name)


class ScreenTests(unittest.TestCase):
    def test_the_screen_shows_what_is_not_ok_and_counts_the_rest(self):
        text = doctor.screen(report([[('ok', 'quiet', 'nothing to see'), ('drift', 'route', 'down')]]))
        self.assertIn('route', text)
        self.assertNotIn('nothing to see', text)
        self.assertIn('1 problem', text)


class SnapshotPlaceTests(unittest.TestCase):
    def test_the_real_place_is_the_gitignored_closures_directory(self):
        # The tests below replace this path, so nothing else would notice it moving out of artifacts/.
        self.assertEqual(command.CLOSURES, SITE / 'artifacts/closures')


class SnapshotTests(unittest.TestCase):
    """T8.5: --snapshot keeps the --json document in one new file. It never touches the real artifacts/."""

    HEALTHY_RECORD = {'schema_version': 1, 'summary': {'problem': 0, 'unknown': 0, 'note': 0, 'ok': 0}, 'checks': []}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.closures = self.root / 'artifacts/closures'
        patcher = mock.patch.object(command, 'CLOSURES', self.closures)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_doctor(self, *argv, record=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(doctor, 'summary', return_value=record or self.HEALTHY_RECORD), \
                mock.patch.object(borrow, 'hosts', return_value={}), \
                mock.patch('sys.stdout', out), mock.patch('sys.stderr', err):
            code = command.main(['doctor', *argv])
        return code, out.getvalue(), err.getvalue()

    def files(self):
        return sorted(self.closures.iterdir())

    def test_one_file_that_reads_as_the_json_document(self):
        code, out, _ = self.run_doctor('--json', '--snapshot')
        self.assertEqual(code, 0)
        self.assertEqual(len(self.files()), 1)
        saved = json.loads(self.files()[0].read_text())
        self.assertEqual(saved['schema_version'], 1)
        self.assertEqual(saved, json.loads(out))

    def test_the_path_goes_to_stderr_so_stdout_stays_one_document(self):
        _, out, err = self.run_doctor('--json', '--snapshot')
        json.loads(out)
        self.assertEqual(err.strip(), str(self.files()[0]))

    def test_the_table_is_still_the_screen_and_the_file_is_still_json(self):
        _, out, _ = self.run_doctor('--snapshot')
        with self.assertRaises(ValueError):
            json.loads(out)
        self.assertEqual(json.loads(self.files()[0].read_text()), self.HEALTHY_RECORD)

    def test_nothing_is_written_without_the_flag(self):
        self.run_doctor('--json')
        self.assertFalse(self.closures.exists())

    def test_the_name_is_a_utc_time_and_only_the_owner_reads_it(self):
        self.run_doctor('--snapshot')
        path = self.files()[0]
        self.assertRegex(path.name, r'^\d{8}T\d{6}Z\.json$')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_the_time_is_utc_and_not_the_local_clock(self):
        with mock.patch.dict('os.environ', {'TZ': 'Asia/Tokyo'}):
            time.tzset()
            self.addCleanup(time.tzset)  # runs after the patch has restored TZ
            stem = command.save_snapshot(self.HEALTHY_RECORD).stem
        utc = command.datetime.timezone.utc
        written = command.datetime.datetime.strptime(stem, '%Y%m%dT%H%M%SZ').replace(tzinfo=utc)
        self.assertLess(abs((command.datetime.datetime.now(utc) - written).total_seconds()), 60)

    def test_the_exit_code_stays_the_doctors(self):
        record = {'schema_version': 1, 'summary': {'problem': 1, 'unknown': 0, 'note': 0, 'ok': 0}, 'checks': []}
        self.assertEqual(self.run_doctor('--json', '--snapshot', record=record)[0], 1)

    def fail_to_write(self, record=None):
        self.closures.parent.mkdir(parents=True)
        self.closures.write_text('a file where the directory belongs')
        return self.run_doctor('--json', '--snapshot', record=record)

    def test_a_healthy_run_that_saved_nothing_does_not_exit_0(self):
        code, out, err = self.fail_to_write()
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)['schema_version'], 1)
        self.assertIn('no closure snapshot was written', err.splitlines()[0])
        self.assertIn(f'  check: ls -ld {self.closures}', err)
        self.assertIn(f'  next:  fix that, then run {errors.command()} doctor --snapshot again', err)

    def test_a_failed_save_does_not_hide_a_problem(self):
        record = {'schema_version': 1, 'summary': {'problem': 2, 'unknown': 0, 'note': 0, 'ok': 0}, 'checks': []}
        self.assertEqual(self.fail_to_write(record)[0], 1)

    def test_a_snapshot_already_there_is_not_overwritten(self):
        moment = command.datetime.datetime(2026, 11, 2, 9, 30, 5, tzinfo=command.datetime.timezone.utc)
        self.closures.mkdir(parents=True)
        kept = self.closures / '20261102T093005Z.json'
        kept.write_text('earlier')
        with mock.patch.object(command, 'datetime', wraps=command.datetime) as clock:
            clock.datetime.now.return_value = moment
            code, _, err = self.run_doctor('--snapshot')
        self.assertEqual((code, kept.read_text()), (3, 'earlier'))
        self.assertIn('same second', err)


class CommandContractTests(unittest.TestCase):
    """T8.2: `--json` is one document with a schema_version, for every command the plan names."""

    def capture(self, *argv):
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            code = command.main(list(argv))
        return code, out.getvalue()

    def test_doctor_prints_one_document_and_returns_its_exit_code(self):
        record = {'schema_version': 1, 'summary': {'problem': 0, 'unknown': 1}, 'checks': []}
        with mock.patch.object(doctor, 'summary', return_value=record), mock.patch.object(borrow, 'hosts', return_value={}):
            code, out = self.capture('doctor', '--json')
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)['schema_version'], 1)

    def test_status_json(self):
        with mock.patch.object(command, 'inventory', return_value={}), \
                mock.patch.object(command, 'gateway_state', return_value=(False, 'down')), \
                mock.patch.object(command, 'chatgpt_state', return_value='signed out'), \
                mock.patch.object(command, 'deploy_state', return_value='abc'):
            code, out = self.capture('status', '--json')
        record = json.loads(out)
        self.assertEqual((code, record['schema_version'], record['healthy']), (1, 1, False))
        self.assertEqual(record['rows'][0], {'name': 'gateway', 'state': 'down'})

    def test_the_ops_commands_pass_the_flag_to_the_script_that_answers(self):
        worker = next(name for name, host in borrow.hosts().items() if host.get('ansible_connection') != 'local')
        for argv, script in ((['drift', '--json'], 'drift.py'), (['storage', '--json'], 'storage.py'),
                             (['preflight', worker, '--json'], 'preflight.py')):
            with mock.patch.object(command, 'run') as ran:
                command.main(['ops', *argv])
            words = [str(word) for word in ran.call_args.args[0]]
            self.assertTrue(words[1].endswith(script), words)
            self.assertIn('--json', words)


class PreflightContractTests(unittest.TestCase):
    OK = mock.Mock(stdout='docker=ok\nfree_gib=100\n', stderr='', returncode=0)

    def run_main(self, results, argv):
        out = io.StringIO()
        hosts = {name: {} for name in results}
        with mock.patch.object(borrow, 'hosts', return_value=hosts), \
                mock.patch.object(borrow, 'on_host', side_effect=lambda host, *a, **k: results[host['name']]), \
                mock.patch('sys.stdout', out):
            for name, host in hosts.items():
                host['name'] = name
            code = preflight_script.main(argv)
        return code, out.getvalue()

    def test_json_is_one_document(self):
        code, out = self.run_main({'a': self.OK}, ['a', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['hosts'][0]['host'], 'a')

    def test_the_line_per_host_form_is_unchanged(self):
        out = self.run_main({'a': self.OK}, ['a'])[1]
        self.assertEqual(json.loads(out.splitlines()[0]), {'host': 'a', 'failed': [], 'free_gib': 100, 'ok': True})

    def test_a_failed_check_is_1_and_a_host_nobody_could_ask_is_3(self):
        bad = mock.Mock(stdout='docker=fail\nfree_gib=100\n', stderr='', returncode=0)
        gone = mock.Mock(stdout='', stderr='refused', returncode=255)
        self.assertEqual(self.run_main({'a': bad, 'b': gone}, ['a', 'b'])[0], 1)
        self.assertEqual(self.run_main({'a': self.OK, 'b': gone}, ['a', 'b'])[0], 3)


class StorageContractTests(unittest.TestCase):
    NODE = {'host': 'a', 'reachable': True, 'filesystem': '/', 'free_gib': 1.0, 'roots': [], 'missing': [], 'basis': 'root',
            'update_needs_gib': 2.0, 'scratch_gib': 1, 'spare_needs_gib': None, 'route': [], 'unregistered': [], 'images': []}

    def run_main(self, nodes, argv):
        out = io.StringIO()
        with mock.patch.object(storage_script, 'report', return_value=nodes), \
                mock.patch.object(storage_script.borrow, 'hosts', return_value={}), \
                mock.patch.object(sys, 'argv', ['storage.py', *argv]), mock.patch('sys.stdout', out):
            code = storage_script.main()
        return code, out.getvalue()

    def test_json_is_one_document(self):
        code, out = self.run_main([self.NODE], ['--json'])
        self.assertEqual((code, json.loads(out)['nodes'][0]['host']), (0, 'a'))

    def test_an_unreachable_node_is_3_in_both_forms(self):
        for argv in (['--json'], []):
            self.assertEqual(self.run_main([{'host': 'a', 'reachable': False}], argv)[0], 3)


if __name__ == '__main__':
    unittest.main()
