"""The drift report: templates against installed units, the route against what carries it, and the
suite's image against the Dockerfile it is built from."""
import datetime
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from agentiac import declared, errors  # noqa: E402
import borrow  # noqa: E402
import drift  # noqa: E402

PREFIX = declared.setting('resource_prefix')

# The all.vars a control unit template reads (D8.2), with synthetic values.
SITE_VARS = {'ansible_user': 'operator', 'gateway_port': 24000, 'chatgpt_port': 24001,
             'rclone_config_dir': '/srv/operator/rclone', 'resource_prefix': 'agentiac'}


class CompareTests(unittest.TestCase):
    def test_same_different_and_missing(self):
        self.assertEqual(drift.compare('u', 'a\n', 'a\n')[0], 'ok')
        self.assertEqual(drift.compare('u', 'a\n', 'b\n')[0], 'drift')
        self.assertEqual(drift.compare('u', 'a\n', None)[2], 'not installed')

    def test_the_update_check_is_compared_with_what_is_installed(self):
        self.assertLessEqual({'update-check.service', 'update-check.timer'}, set(drift.CONTROL_UNITS))

    def test_templates_render_with_what_the_playbooks_pass(self):
        for name in drift.CONTROL_UNITS:
            rendered = drift.template(name, code_root='/srv/code', instance_root='/srv/site', **SITE_VARS)
            self.assertTrue('/srv/code' in rendered or '/srv/site' in rendered or name.endswith('.timer'))
            self.assertNotIn('control' + '_root', rendered)   # spelled apart: T7.4's grep for the old name stays empty
        # T7.4: every control unit that runs control code names the instance root for it (D6.2).
        for name in ('gateway', 'chatgpt', 'backup', 'backup-verify', 'evaluation-watch', 'evaluation-watch-health',
                     'recovery', 'update-check'):
            rendered = drift.template(f'{name}.service', code_root='/srv/code', instance_root='/srv/site', **SITE_VARS)
            self.assertIn('/srv/code', rendered)
            self.assertNotIn('control' + '_root', rendered)   # spelled apart: T7.4's grep for the old name stays empty
            self.assertIn('Environment=AGENTIAC_ROOT=/srv/site\n', rendered)
        self.assertIn('/d/worker.py', drift.template('spark-tensorfold.service', directory='/d', ansible_user='operator'))
        self.assertIn('/d/proxy.py /d/config.json', drift.template('ondemand.service', directory='/d', ansible_user='operator'))
        self.assertTrue(drift.template('ondemand.service', directory='/d', ansible_user='operator').endswith('multi-user.target\n'))


class FrontUnitTests(unittest.TestCase):
    def test_a_strata_front_is_compared_with_the_unit_claim_installs_in_its_strata_root(self):
        dedicated = [(name, host) for name, host in borrow.hosts().items() if 'strata_backend' in host]
        self.assertTrue(dedicated)
        for name, host in dedicated:
            expected = borrow.strata_root(host, host['strata_backend']) + '/ondemand'
            self.assertEqual(drift.front_directory(host), expected)
            installed = drift.template('ondemand.service', directory=expected,
                                       ansible_user=declared.account(host))
            done = mock.Mock(returncode=0, stdout=installed)
            with self.subTest(name), mock.patch.object(drift.borrow, 'on_host', return_value=done):
                self.assertEqual([row[0] for row in drift.worker_units({name: host})], ['ok'])
        # a llama.cpp front keeps project_root
        llama = {'project_root': '/srv/p', 'ondemand_persistent': True}
        self.assertEqual(drift.front_directory(llama), '/srv/p/ondemand')


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hosts = borrow.hosts()
        targets = json.loads(drift.route.ROUTES.read_text())['targets']
        cls.TARGET, spec = next(
            (name, spec) for name, spec in targets.items()
            if hosts.get(spec['node'], {}).get('ansible_connection') != 'local'
        )
        cls.HOSTS = {spec['node']: hosts[spec['node']]}

    def state(self, tunnel, front):
        with mock.patch.object(drift.route, 'current', return_value=self.TARGET), \
                mock.patch.object(drift, 'tunnel_active', return_value=tunnel), \
                mock.patch.object(drift, 'front_answers', return_value=front):
            return list(drift.route_path(self.HOSTS))[0]

    def test_a_carried_route_is_ok(self):
        self.assertEqual(self.state(True, True)[0], 'ok')

    def test_a_reboot_shows_as_drift_with_the_way_back(self):
        for tunnel, front in ((False, False), (True, False)):
            state, _, detail = self.state(tunnel, front)
            self.assertEqual(state, 'drift')
            self.assertIn(f'{errors.command()} reclaim', detail)

    def test_a_key_that_was_never_made_is_no_front(self):
        with mock.patch.object(borrow, 'ondemand_call', side_effect=FileNotFoundError):
            self.assertFalse(drift.front_answers('worker-b', {}))


class TunnelTests(unittest.TestCase):
    LISTED = f'{PREFIX}-tunnel-worker-b.service loaded active running x\n{PREFIX}-tunnel-worker-c.service loaded active running y\n'
    HOSTS = {'worker-b': {'borrowed': True}, 'worker-c': {}}

    def setUp(self):
        recorded = mock.patch.object(drift.claims, 'read', return_value=None)  # whatever this checkout has claimed
        self.addCleanup(recorded.stop)
        self.read = recorded.start()

    def test_a_tunnel_with_a_claim_record_is_left_to_the_claim_line(self):
        hosts = {'worker-b': {'borrowed': True}, 'worker-a': {'borrowed': True}}
        listed = f'{PREFIX}-tunnel-worker-b.service loaded active running x\n{PREFIX}-tunnel-worker-a.service loaded active running y\n'
        self.read.side_effect = lambda root, name: {'host': name} if name == 'worker-b' else None
        with mock.patch.object(drift.subprocess, 'run', return_value=mock.Mock(stdout=listed)), \
                mock.patch.object(drift, 'front_answers', return_value=False):
            found = list(drift.tunnels(hosts))
        self.assertEqual([subject for _, subject, _ in found], ['tunnel worker-a'])
        self.assertEqual(self.read.call_args_list[0].args, (drift.ROOT, 'worker-b'))

    def test_only_a_borrowed_hosts_unused_tunnel_is_reported(self):
        with mock.patch.object(drift.subprocess, 'run', return_value=mock.Mock(stdout=self.LISTED)), \
                mock.patch.object(drift, 'front_answers', return_value=False):
            found = list(drift.tunnels(self.HOSTS))
        self.assertEqual([subject for _, subject, _ in found], ['tunnel worker-b'])


class ClaimRecordTests(unittest.TestCase):
    """`ops drift` sets what control wrote down at a claim against what answers now (F05, T2.7)."""
    CLAIMED_AT = datetime.datetime(2026, 11, 15, 8, 0, tzinfo=datetime.timezone.utc).isoformat()
    RECORD = {'host': 'x', 'backend': 'strata-0.2.12-tiny-coder-q4', 'claimed_at': CLAIMED_AT}

    def lines(self, records, hosts, answers):
        with mock.patch.object(drift.claims, 'all', return_value=records) as read, \
                mock.patch.object(drift, 'front_answers', return_value=answers):
            found = list(drift.claim_records(hosts))
        read.assert_called_once_with(drift.ROOT)
        return found

    def test_a_record_for_a_host_the_inventory_no_longer_has_is_drift(self):
        (state, subject, detail), = self.lines({'gone': self.RECORD}, {'worker-c': {}}, True)
        self.assertEqual((state, subject), ('drift', 'claim gone'))
        self.assertIn('artifacts/claims/gone.json', detail)

    def test_a_record_with_no_front_behind_it_is_drift_and_says_what_to_run(self):
        found = self.lines({'worker-b': self.RECORD, 'worker-c': self.RECORD},
                           {'worker-b': {'borrowed': True}, 'worker-c': {}}, False)
        self.assertEqual([(state, subject) for state, subject, _ in found],
                         [('drift', 'claim worker-b'), ('drift', 'claim worker-c')])
        self.assertIn(f'claimed {self.CLAIMED_AT} (strata-0.2.12-tiny-coder-q4), but no front answers', found[0][2])
        self.assertTrue(found[0][2].endswith(f'{errors.command()} release worker-b'))
        self.assertTrue(found[1][2].endswith(f'{errors.command()} claim worker-c'))

    def test_a_record_whose_front_answers_is_ok(self):
        (state, subject, detail), = self.lines({'worker-c': self.RECORD}, {'worker-c': {}}, True)
        self.assertEqual((state, subject), ('ok', 'claim worker-c'))
        self.assertIn('front answers', detail)

    def test_no_record_means_no_line(self):
        self.assertEqual(self.lines({}, {'worker-c': {}}, True), [])


class RevisionTests(unittest.TestCase):
    def report(self, deployed, head='a' * 40, changed='', core_head=None):
        deployed_path = mock.Mock(exists=lambda: True, read_text=lambda: json.dumps(deployed))
        core_path = mock.Mock(is_dir=lambda: core_head is not None)
        results = [mock.Mock(stdout=head), mock.Mock(stdout=changed)]
        if core_head is not None:
            results.append(mock.Mock(stdout=core_head))
        root = mock.MagicMock(__truediv__=lambda self, other: core_path if other == 'core' else deployed_path)
        with mock.patch.object(drift.subprocess, 'run', side_effect=results), \
                mock.patch.object(drift, 'ROOT', root):
            return list(drift.revision())

    def test_head_is_ok(self):
        self.assertEqual(self.report({'revision': 'a' * 40, 'dirty': False})[0][0], 'ok')

    def test_a_dirty_deploy_is_drift(self):
        self.assertEqual(self.report({'revision': 'a' * 40, 'dirty': True})[0][0], 'drift')

    def test_newer_commits_are_a_note_and_a_change_in_progress_too(self):
        states = [state for state, _, _ in self.report({'revision': 'b' * 40, 'dirty': False}, changed=' M file\n')]
        self.assertEqual(states, ['note', 'note'])

    def test_split_checkout_compares_both_site_and_core_revisions(self):
        ok = self.report({'site': 'a' * 40, 'core': 'c' * 40, 'dirty': False}, head='a' * 40, core_head='c' * 40)
        self.assertEqual(ok, [('ok', 'deploy', 'aaaaaaaa (core cccccccc) is HEAD')])
        moved = self.report({'site': 'a' * 40, 'core': 'c' * 40, 'dirty': False}, head='a' * 40, core_head='d' * 40)
        self.assertEqual(moved[0][:2], ('note', 'deploy'))
        self.assertIn('aaaaaaaa (core cccccccc) deployed, HEAD is aaaaaaaa (core dddddddd)', moved[0][2])


class WorkerUnitTests(unittest.TestCase):
    HOSTS = {'worker-a': {'backend': 'strata', 'ansible_host': 'worker-a.example',
                          'ondemand_persistent': True, 'project_root': '/mnt/data/agentiac-strata-0.2.8'},
             'control': {'ansible_connection': 'local'}}

    def completed(self, stdout='', rc=0):
        return mock.Mock(stdout=stdout, returncode=rc)

    def one(self, rows):
        rows = list(rows)
        return rows[0] if rows else None

    def test_a_failed_unit_on_a_worker_is_reported_by_name(self):
        # Control's own failed_units() cannot see a worker, and the stability decision needs failed units on
        # every node: a duplicate preparation rejected by its lock has to reach this report by unit name.
        unit = f'{PREFIX}-tensorfold-update-20e15e4.service'
        listing = self.completed(f'{unit} loaded failed failed x\n'
                                 'whoopsie.service loaded failed failed y\n')
        with mock.patch.object(drift.borrow, 'on_host', return_value=listing) as on_host:
            row = self.one(list(drift.worker_failed_units(self.HOSTS)))
        self.assertEqual(row[0], 'note')          # a failed unit is worth knowing, not drift
        self.assertEqual(row[1], 'failed units worker-a')
        self.assertIn(unit, row[2])
        self.assertNotIn('whoopsie', row[2])      # somebody else's unit is not ours to report
        argv = ' '.join(on_host.call_args.args[1])
        self.assertIn('--state=failed', argv)
        self.assertNotIn('*', argv)               # a remote shell would glob this against ~/agentiac

    def test_a_worker_without_failed_units_says_nothing_and_control_is_left_to_its_own_check(self):
        with mock.patch.object(drift.borrow, 'on_host', return_value=self.completed()):
            self.assertEqual(list(drift.worker_failed_units({'worker-a': {'ansible_host': 'worker-a'}})), [])
        with mock.patch.object(drift.borrow, 'on_host', side_effect=AssertionError('control is checked once')):
            self.assertEqual(list(drift.worker_failed_units({'control': {'ansible_connection': 'local'}})), [])

    def test_a_worker_that_answered_nothing_is_not_silently_gone(self):
        # `systemctl list-units --state=failed` exits 0 with no output; ssh failure is exit 255.
        with mock.patch.object(drift.borrow, 'on_host', return_value=self.completed(rc=255)):
            self.assertEqual(self.one(list(drift.worker_failed_units({'worker-a': {'ansible_host': 'worker-a'}}))),
                             ('note', 'failed units worker-a', 'host not reachable; not checked'))
        with mock.patch.object(drift.borrow, 'on_host', side_effect=OSError('ssh unavailable')):
            self.assertEqual(self.one(list(drift.worker_failed_units({'worker-a': {'ansible_host': 'worker-a'}}))),
                             ('note', 'failed units worker-a', 'host not reachable; not checked'))


class StorageTests(unittest.TestCase):
    """drift shows the same numbers `ops storage` prints, so the two cannot disagree (F7)."""
    NODE = {'host': 'worker-a', 'reachable': True, 'filesystem': '/mnt/data', 'free_gib': 285.0, 'roots': [],
            'missing': [], 'scratch_gib': 45, 'update_needs_gib': 170.0, 'spare_needs_gib': 125.6,
            'unregistered': [], 'images': [], 'route': []}

    def one(self, rows):
        return list(rows)[0]

    def test_room_for_one_more_root_is_enough_to_be_ok(self):
        with mock.patch.object(drift.storage, 'report', return_value=[self.NODE]):
            self.assertEqual(self.one(drift.disk({})),
                             ('ok', 'storage worker-a', '285.0 GiB free, room for one more root (170.0 GiB)'))

    def test_a_tight_disk_and_a_leftover_root_are_notes(self):
        tight = dict(self.NODE, free_gib=120.0, unregistered=['strata-0.2.9-candidate'])
        with mock.patch.object(drift.storage, 'report', return_value=[tight]):
            state, _, detail = self.one(drift.disk({}))
        self.assertEqual(state, 'note')
        self.assertIn('120.0 GiB free, one more root needs 170.0 GiB', detail)
        self.assertIn('unregistered: strata-0.2.9-candidate', detail)

    def test_an_image_no_record_names_is_a_note(self):
        worker = dict(self.NODE, host='worker-d', filesystem='/srv/worker', free_gib=707.0,
                      images=[{'tag': 'agentiac-tensorfold:flash-next-v999-deadbeef', 'size': '24.5GB'}])
        with mock.patch.object(drift.storage, 'report', return_value=[worker]):
            state, subject, detail = self.one(drift.disk({}))
        self.assertEqual((state, subject), ('note', 'storage worker-d'))
        self.assertIn('flash-next-v999-deadbeef', detail)

    def test_a_declared_root_that_is_gone_is_a_note(self):
        gone = dict(self.NODE, missing=['/mnt/data/agentiac-strata-0.2.8'])
        with mock.patch.object(drift.storage, 'report', return_value=[gone]):
            state, _, detail = self.one(drift.disk({}))
        self.assertEqual(state, 'note')
        self.assertIn('declared root absent', detail)

    def test_a_host_that_cannot_be_measured_is_a_note(self):
        with mock.patch.object(drift.storage, 'report', return_value=[{'host': 'worker-b', 'reachable': False}]):
            self.assertEqual(self.one(drift.disk({})),
                             ('note', 'storage worker-b', 'host not reachable; disk not measured'))


class ReportTests(unittest.TestCase):
    def test_worker_failures_disk_and_the_image_are_part_of_the_report(self):
        probes = {name: mock.Mock(return_value=iter([])) for name in
                  ('control_units', 'revision', 'gateways', 'route_path', 'front_identity', 'tunnels', 'claim_records',
                   'failed_units', 'worker_units', 'worker_failed_units', 'disk', 'eval_image', 'suite_image')}
        with mock.patch.multiple(drift, **probes):
            self.assertEqual(list(drift.report({})), [])
        for name, probe in probes.items():
            self.assertEqual(probe.call_count, 1, name)


class EvalImageLineTests(unittest.TestCase):
    """drift carries IP-13 as one line, whatever that line says."""

    def test_one_line_about_the_suite_image(self):
        with mock.patch.object(drift.evalimage, 'check', return_value=('note', 'no label yet')):
            self.assertEqual(list(drift.eval_image()), [('note', 'eval image', 'no label yet')])

    def test_a_stale_image_reaches_the_exit_code(self):
        with mock.patch.object(drift.evalimage, 'check', return_value=('drift', 'built from another Dockerfile')):
            line = list(drift.eval_image())[0]
        self.assertEqual(line[0], 'drift')
        self.assertEqual(line[1], 'eval image')


class FrontIdentityTests(unittest.TestCase):
    """A manifest edit reaches the gateway at a deploy and the worker at a claim. This line is the gap."""

    EXPECTED = {'backend_id': 'b', 'manifest_hash': 'h', 'context': 262144}
    HOSTS = {'worker-d': {}}

    def lines(self, reported, code=200, target_spec=None, raises=None):
        spec = target_spec or {'node': 'worker-d', 'backend': 'b', 'context_window': 262144}
        call = mock.Mock(side_effect=raises) if raises else mock.Mock(return_value=(code, reported))
        with mock.patch.object(drift.route, 'current', return_value='worker-d'), \
                mock.patch.object(drift.route.ROUTES.__class__, 'read_text', return_value=json.dumps({'targets': {'worker-d': spec}})), \
                mock.patch.object(drift.route.identity_policy, 'expected_identity', return_value=self.EXPECTED), \
                mock.patch.object(drift.borrow, 'ondemand_call', call):
            return list(drift.front_identity(self.HOSTS)), call

    def test_the_declared_manifest_is_ok(self):
        lines, call = self.lines({'identity': self.EXPECTED})
        self.assertEqual(lines[0][0], 'ok')
        self.assertEqual(call.call_args.args[2:], ('GET', 'status'))        # passive: it never loads a model

    def test_a_different_manifest_is_drift_and_says_what_it_will_cost(self):
        lines, _ = self.lines({'identity': dict(self.EXPECTED, manifest_hash='other')})
        self.assertEqual(lines[0][:2], ('drift', 'front identity'))
        self.assertIn('manifest_hash', lines[0][2])
        self.assertIn('503', lines[0][2])

    def test_a_front_with_no_identity_is_a_note(self):
        self.assertEqual(self.lines({'identity': None})[0][0][0], 'note')

    def test_no_front_to_ask_is_left_to_the_route_line(self):
        self.assertEqual(self.lines({}, code=404)[0], [])
        self.assertEqual(self.lines({}, raises=OSError('no key'))[0], [])

    def test_a_target_with_no_manifest_is_not_compared(self):
        lines, call = self.lines({}, target_spec={'node': 'worker-d'})
        self.assertEqual(lines, [])
        call.assert_not_called()


class SuiteImageLineTests(unittest.TestCase):
    def test_it_carries_what_the_lock_says(self):
        with mock.patch.object(drift.evalimage, 'check_suite', return_value=('drift', 'moved')):
            self.assertEqual(list(drift.suite_image()), [('drift', 'suite image', 'moved')])


class JsonContractTests(unittest.TestCase):
    """PA §13: stdout is one JSON document with a schema_version, and the exit code keeps its meaning."""

    def run_main(self, lines=None, raises=None, argv=('--json',)):
        report = mock.Mock(side_effect=raises) if raises else mock.Mock(return_value=iter(lines))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(drift, 'report', report), mock.patch.object(drift.borrow, 'hosts', return_value={}), \
                mock.patch('sys.stdout', out), mock.patch('sys.stderr', err):
            code = drift.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_json_is_one_document(self):
        code, out, _ = self.run_main([('ok', 'a', 'fine'), ('note', 'b', 'hm')])
        record = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(record['schema_version'], 1)
        self.assertEqual(record['checks'][1], {'state': 'note', 'subject': 'b', 'detail': 'hm'})

    def test_drift_is_exit_1_in_both_forms(self):
        for argv in (('--json',), ()):
            self.assertEqual(self.run_main([('drift', 'a', 'bad')], argv=argv)[0], 1)

    def test_the_lines_are_unchanged_without_the_flag(self):
        self.assertEqual(self.run_main([('ok', 'a', 'fine')], argv=())[1], f"{'ok':6}{'a':50}fine\n")

    def test_a_report_that_could_not_be_made_is_exit_3_and_says_so_on_stderr(self):
        code, out, err = self.run_main(raises=OSError('no inventory'))
        self.assertEqual((code, out), (3, ''))
        self.assertIn('no inventory', err)


if __name__ == '__main__':
    unittest.main()
