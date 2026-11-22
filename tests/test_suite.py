"""Phase 2 suite: pytest outcome parsing, grading decision, pi event summary and Wilson interval."""
import contextlib
import datetime
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import declared, hardware, identities, paths, suite  # noqa: E402

SITE = paths.root()

spec = importlib.util.spec_from_file_location('suite_cli', ROOT / 'scripts/suite.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class OutcomeTests(unittest.TestCase):
    def test_summary_lines_map_node_ids_and_ignore_other_output(self):
        text = '\n'.join(['....F', 'PASSED tests/test_a.py::test_ok',
                          'FAILED tests/test_a.py::TestX::test_bad[1-2] - AssertionError: no',
                          'ERROR tests/test_b.py - ImportError: x', 'SKIPPED [1] tests/test_c.py:3: board',
                          '1 failed, 1 passed in 0.1s'])
        self.assertEqual(suite.parse_outcomes(text), {
            'tests/test_a.py::test_ok': ('passed', ''),
            'tests/test_a.py::TestX::test_bad[1-2]': ('failed', 'AssertionError: no'),
            'tests/test_b.py': ('error', 'ImportError: x')})


class GradeTests(unittest.TestCase):
    def grade(self, outcomes, returncode=0):
        task = {'reference': 'r', 'acceptance': ['t::new'], 'regression': ['t::old1', 't::old2']}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(suite, 'overlay_tests'), \
                patch.object(suite, 'pytest', return_value=(outcomes, returncode, 'out')):
            (Path(directory) / 'src.py').write_text('x = 1\n')
            return suite.grade(Path(directory), Path('/nonexistent'), task)[0]

    def test_acceptance_and_regressions_must_all_pass(self):
        ok = {'t::new': ('passed', ''), 't::old1': ('passed', ''), 't::old2': ('passed', '')}
        self.assertTrue(self.grade(ok)['passed'])

    def test_passing_expected_tests_cannot_hide_a_failed_pytest_process(self):
        ok = {'t::new': ('passed', ''), 't::old1': ('passed', ''), 't::old2': ('passed', '')}
        for code in (1, 2, 3, 4, 5, 137):
            with self.subTest(returncode=code):
                result = self.grade(ok, returncode=code)
                self.assertFalse(result['passed'])
                self.assertEqual(result['pytest_returncode'], code)

    def test_a_missing_acceptance_test_fails_and_regressions_are_listed(self):
        result = self.grade({'t::old1': ('failed', 'boom'), 't::old2': ('passed', '')})
        self.assertFalse(result['passed'])
        self.assertEqual(result['acceptance_failed'], ['t::new'])
        self.assertEqual(result['regression_failed'], ['t::old1'])

    def test_derive_splits_acceptance_regression_and_broken(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'repo'
            source.mkdir()
            git = lambda *a: subprocess.run(
                ['git', '-C', str(source), '-c', 'user.name=t', '-c', 'user.email=t@t'] + list(a),
                check=True, capture_output=True)
            git('init', '-q')
            (source / 'tests').mkdir()
            (source / 'tests/test_x.py').write_text('')
            git('add', '-A')
            git('commit', '-q', '-m', 'base')
            (source / 'tests/test_x.py').write_text('# fixed\n')
            git('add', '-A')
            git('commit', '-q', '-m', 'fix')
            runs = iter([({'a': ('passed', ''), 'b': ('failed', 'E'), 'c': ('passed', '')}, 1, ''),
                         ({'a': ('passed', ''), 'b': ('passed', ''), 'c': ('failed', 'E')}, 1, '')])
            with patch.object(suite, 'pytest', side_effect=lambda tree, image: next(runs)):
                derived = suite.derive(source, 'HEAD')
        self.assertEqual((derived['acceptance'], derived['regression'], derived['broken_by_reference']),
                         (['b'], ['a'], ['c']))
        self.assertEqual(derived['changed_paths'], ['tests/test_x.py'])


class ReportTests(unittest.TestCase):
    def test_pi_events_count_tools_errors_turns_and_usage(self):
        events = [{'type': 'tool_execution_end', 'toolName': 'read'},
                  {'type': 'tool_execution_end', 'toolName': 'bash', 'isError': True},
                  {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'toolUse',
                                                      'usage': {'input': 10, 'output': 2}}},
                  {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop',
                                                      'usage': {'input': 30, 'output': 5}}}]
        observed = cli.pi_events('\n'.join(json.dumps(e) for e in events) + '\nnot json')
        self.assertEqual(observed, {'tools': {'read': 1, 'bash': 1}, 'tool_errors': 1, 'assistant_turns': 2,
                                    'stop_reason': 'stop', 'usage': {'input': 40, 'output': 7}})

    def test_wilson_interval(self):
        self.assertEqual(cli.wilson(0, 0), (0.0, 0.0))
        low, high = cli.wilson(9, 10)
        self.assertTrue(0.59 < low < 0.6 and 0.98 < high < 0.99)


class ProviderTests(unittest.TestCase):
    def test_private_provider_overrides_endpoint_without_changing_tracked_config(self):
        model = {'id': 'private-eval', 'contextWindow': 131072, 'maxTokens': 16384, 'reasoning': True}
        provider = {'baseUrl': 'http://127.0.0.1:18884/v1', 'api': 'openai-completions',
                    'apiKey': 'evaluation-only', 'models': [model]}
        tracked = (SITE / 'agents/pi/models.json').read_bytes()
        self.assertEqual(cli.evaluation_model('private-eval', True, provider), model)
        configured = cli.provider_config({'provider': provider, 'model': model})
        self.assertEqual(configured['providers']['local-infra'], provider)
        self.assertEqual((SITE / 'agents/pi/models.json').read_bytes(), tracked)
        with self.assertRaises(ValueError):
            cli.evaluation_model('other-alias', True, provider)
        with self.assertRaises(ValueError):
            cli.evaluation_model('private-eval', True, {**provider, 'models': [model, model]})

    def test_default_provider_preserves_gateway_endpoint(self):
        model = {'id': 'existing'}
        configured = cli.provider_config({'model': model})['providers']['local-infra']
        tracked = json.loads((SITE / 'agents/pi/models.json').read_text())['providers']['local-infra']
        self.assertEqual(configured['baseUrl'], tracked['baseUrl'])
        self.assertEqual(configured['models'], [model])


class RunHarness(unittest.TestCase):
    """Drives `suite.py run` end to end with the container, the tasks and the state file in tempdirs."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.data = self.root / 'state'
        tasks = self.root / 'tasks'
        tasks.mkdir()
        (tasks / 'task.json').write_text('{}\n')
        self.provider = self.root / 'provider.json'
        self.provider.write_text(json.dumps({
            'baseUrl': 'http://127.0.0.1:18884/v1', 'api': 'openai-completions',
            'apiKey': 'evaluation-only', 'models': [{'id': 'eval', 'reasoning': True}]}))
        self.calls = []
        self.failing = set()
        for item in [patch.object(cli, 'DATA', self.data), patch.object(cli, 'TASKS', tasks),
                     patch.object(cli, 'load_task', return_value={}),
                     patch.object(cli.subprocess, 'check_output', side_effect=lambda command, **kw:
                                  'sha256:test\n' if command[0] == 'docker' else '1.3.0\n'),
                     patch.object(cli.adapters.PiAdapter, 'installed', return_value=True),  # no real install in CI
                     patch.object(cli, 'run_case', side_effect=self.case_result)]:
            item.start()
            self.addCleanup(item.stop)

    def case_result(self, case, spec, task, key, folder):
        self.calls.append(spec['trial'])
        return {'passed': spec['trial'] not in self.failing, 'wall_s': 1.0}

    def invoke(self, *options):
        argv = ['suite.py', 'run', '--alias', 'eval', '--task', 'task',
                '--provider-config', str(self.provider), *options]
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
            cli.main()

    def cases(self):
        return {json.loads(row['spec'])['trial']: row
                for row in cli.Store(self.data / 'state.sqlite').summary()}


class TrialSelectionTests(RunHarness):
    def test_selected_trial_avoids_zero_and_keeps_legacy_case_identity(self):
        self.invoke('--trial', '1')
        self.assertEqual(self.calls, [1])
        selected = self.cases()[1]
        self.assertEqual(set(self.cases()), {1})
        self.invoke('--k', '2')
        self.assertEqual(self.calls, [1, 0])
        self.assertEqual(self.cases()[1], selected)
        self.assertEqual(set(self.cases()), {0, 1})

    def test_unselected_failed_attempt_is_preserved_and_duplicate_trials_run_once(self):
        self.failing = {0, 2}
        self.invoke('--k', '2')
        before = self.cases()
        store = cli.Store(self.data / 'state.sqlite')
        with store.connect() as db:
            attempts = [tuple(row) for row in db.execute('SELECT * FROM attempts ORDER BY case_id')]
        self.invoke('--trial', '1')
        self.invoke('--trial', '2', '--trial', '2', '--retry-failed')
        self.assertEqual(self.calls, [0, 1, 2])
        after = self.cases()
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[1], before[1])
        self.assertEqual(after[2]['attempts'], 1)
        with store.connect() as db:
            for old in attempts:
                self.assertEqual(tuple(db.execute('SELECT * FROM attempts WHERE case_id=? AND number=?',
                                                  old[:2]).fetchone()), old)

    def test_cli_preserves_default_range_and_explicit_selection_order(self):
        for options, expected in [((), [0, 1, 2]), (('--k', '2'), [0, 1]),
                                  (('--trial', '2', '--trial', '1', '--trial', '2'), [2, 1]),
                                  (('--trial', '0'), [0])]:
            with self.subTest(options=options), patch.object(cli, 'run') as run:
                self.invoke(*options)
                self.assertEqual(list(run.call_args.args[0].trials), expected)

    def test_invalid_or_ambiguous_selection_fails_before_running(self):
        for options in [('--trial', '-1'), ('--k', '0'), ('--k', '-1'),
                        ('--trial', '1', '--k', '2')]:
            with self.subTest(options=options), patch.object(cli, 'run') as run, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.invoke(*options)
            self.assertEqual(error.exception.code, 2)
            run.assert_not_called()
        self.assertFalse((self.data / 'state.sqlite').exists())

    def test_a_root_run_is_refused_before_anything_is_recorded(self):
        # A bare `sudo systemd-run` runs the suite as root: git calls the task mirror a dubious ownership,
        # tar hands back an empty archive, and every attempt lands in the database as a 0-second
        # infrastructure_error over a real token spend. It has to stop before the first row.
        with patch.object(cli.os, 'geteuid', return_value=0), patch.object(cli, 'run_case') as run_case, \
                self.assertRaises(SystemExit) as error:
            self.invoke()
        self.assertIn('cannot run as root', str(error.exception))
        run_case.assert_not_called()
        self.assertFalse((self.data / 'state.sqlite').exists())


PROBES = {'resources': {'stdout': 'Mem:     130661138432 1 2\n'},
          'cpu_facts': {'stdout': 'arch: aarch64\ncpus: 20\ndtmodel: NVIDIA GB10\nsocid: \nsocfamily: \n'},
          'gpu_facts': {'stdout': 'NVIDIA GB10, 580.178.04, 126995\n'},
          'gpu_list': {'stdout': ''}, 'gpu': {'stdout': ''}, 'meminfo': {'stdout': ''}}
TARGETS = {'worker-a': {'node': 'worker-a', 'source_alias': 'worker-a-tf', 'backend': 'worker-a-tf',
                        'context_window': 131072, 'max_tokens': 16384, 'thinking': 'chat-template'},
           'worker-b': {'node': 'worker-b', 'source_alias': 'worker-b-worker', 'context_window': 65536, 'max_tokens': 8192}}
BACKENDS = {'worker-a-tf': {'id': 'worker-a-tf-v1', 'files': [{'sha256': 'a' * 64}], 'quantization': 'fp4',
                            'repository': 'nvidia/flash', 'source_version': '0.2.8', 'source_revision': 'deadbeef',
                            'class_args': ['--temperature=0'], 'config_args': ['--parallel=1'],
                            'kv_cache_type': 'bf16', 'workers': {'worker-a': {'slots': 1}}}}


class ProvenanceTests(RunHarness):
    """T10.3: a result says what served it, and the case id does not move (X18)."""

    def setUp(self):
        super().setUp()
        for item in (patch.object(cli, 'ROOT', self.root),
                     patch.object(cli, 'revision', return_value=('abc1234', False)),
                     patch.object(cli.declared, 'load', side_effect=lambda root=None: self.graph()),
                     patch.object(cli.route, 'current', return_value='worker-a')):
            item.start()
            self.addCleanup(item.stop)
        self.inventory = self.root / 'artifacts/inventory'
        self.record = hardware.record('worker-a', PROBES)
        hardware.write(self.inventory, self.record)

    def graph(self, alias='eval'):
        # nodes, profiles, backends, route, gateway_models, components, retention, agents, pi_models, router
        return declared.Declared(self.root, {}, {}, BACKENDS,
                                 {'alias': alias, 'default': 'worker-a', 'targets': TARGETS},
                                 [], [], {}, [], [], {})

    def results(self):
        return list(cli.latest_results(cli.Store(self.data / 'state.sqlite')))

    def test_a_run_records_the_class_the_config_the_hardware_and_the_code(self):
        self.invoke('--trial', '1')
        prov = self.results()[0][2]['provenance']
        expected = identities.compute_target(self.graph(), 'worker-a').as_dict()
        self.assertEqual(prov['class_id'], expected['class_id'])
        self.assertEqual(prov['config_id'], expected['config_id'])
        self.assertEqual((prov['target'], prov['node']), ('worker-a', 'worker-a'))
        self.assertEqual(prov['hw_fp'], self.record['hw_fp'])
        self.assertEqual(prov['agent'], {'name': 'pi', 'version': '1.3.0'})
        self.assertEqual((prov['revision'], prov['revision_dirty']), ('abc1234', False))
        self.assertEqual(prov['unknowns'], {})

    def test_provenance_never_touches_the_case_spec(self):
        # suite v1 case ids are the hash of the spec: a new field there would orphan every
        # stored result, so the only place provenance may appear is the result
        self.invoke('--trial', '1')
        spec, _, result = self.results()[0]
        self.assertNotIn('provenance', spec)
        self.assertEqual(set(spec), {'suite', 'frontend', 'alias', 'model', 'image', 'task',
                                     'task_sha256', 'trial', 'timeout_s', 'provider'})
        self.assertEqual(cli.stable_id(spec), self.cases()[1]['id'])
        self.assertIn('provenance', result)

    def test_an_infrastructure_failure_still_says_what_was_being_run(self):
        with patch.object(cli, 'run_case', side_effect=OSError('docker is down')):
            self.invoke('--trial', '1')
        _, _, result = self.results()[0]
        self.assertIn('infrastructure_error', result)
        self.assertEqual(result['provenance']['config_id'],
                         identities.compute_target(self.graph(), 'worker-a').as_dict()['config_id'])

    def test_an_infrastructure_failure_stores_the_sentence_and_not_the_advice(self):
        # failures.py prints this field in one row: advice for a terminal must not reach a stored result.
        error = cli.adapters.AdapterError(cli.explain('pi is not pinned at /x', ['gone'], 'look', 'fix'))
        with patch.object(cli, 'run_case', side_effect=error):
            self.invoke('--trial', '1')
        self.assertEqual(self.results()[0][2]['infrastructure_error'], 'AdapterError: pi is not pinned at /x')

    def test_a_host_nobody_inventoried_costs_the_hardware_identity_and_nothing_else(self):
        (self.inventory / 'worker-a.json').unlink()
        self.invoke('--trial', '1')
        prov = self.results()[0][2]['provenance']
        self.assertEqual(prov['hw_fp'], identities.UNKNOWN)
        self.assertEqual(prov['unknowns'], {'hw_fp': 'no inventory record'})
        self.assertNotEqual(prov['config_id'], identities.UNKNOWN)

    def test_a_stale_inventory_record_is_not_passed_off_as_the_machine(self):
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=hardware.MAX_AGE_S + 10)
        hardware.write(self.inventory, dict(self.record, collected_at=old.isoformat(timespec='seconds')))
        self.invoke('--trial', '1')
        prov = self.results()[0][2]['provenance']
        self.assertEqual(prov['hw_fp'], identities.UNKNOWN)
        self.assertTrue(prov['hardware']['stale'])

    def test_a_graph_that_cannot_be_read_is_reported_and_never_stops_the_run(self):
        with patch.object(cli.declared, 'load', side_effect=OSError('backends is gone')):
            prov = cli.provenance('eval', '1.3.0', root=self.root)
        self.assertEqual(prov['unknowns']['provenance'], 'OSError: backends is gone')
        self.assertNotIn('class_id', prov)
        self.invoke('--trial', '1')  # the run itself still finishes
        self.assertEqual(len(self.results()), 1)

    def test_an_alias_outside_the_declared_graph_claims_no_identity(self):
        prov = cli.provenance('some-other-alias', '1.3.0', root=ROOT)  # ROOT so git can answer
        self.assertEqual(list(prov['unknowns']), ['target'])
        for field in ('class_id', 'config_id', 'hw_fp', 'node', 'target'):
            self.assertNotIn(field, prov)

    def test_the_bare_alias_takes_the_target_the_route_is_live_on(self):
        with patch.object(cli.route, 'current', return_value='worker-b'):
            self.assertEqual(cli.target_of('eval', self.graph()), 'worker-b')
        self.assertEqual(cli.target_of('eval-worker-b', self.graph()), 'worker-b')
        self.assertEqual(cli.target_of('eval-worker-a', self.graph()), 'worker-a')
        self.assertIsNone(cli.target_of('eval-nowhere', self.graph()))
        with patch.object(cli.route, 'current', return_value='deleted-target'):
            self.assertIsNone(cli.target_of('eval', self.graph()))

    def test_a_target_whose_manifest_is_missing_says_which_inputs_are_missing(self):
        with patch.object(cli.route, 'current', return_value='worker-b'):
            prov = cli.provenance('eval', '1.3.0', root=self.root)
        self.assertEqual(prov['node'], 'worker-b')
        self.assertIn('backend', prov['unknowns']['identity'])  # worker-b-worker has no manifest
        self.assertNotEqual(prov['config_id'], identities.UNKNOWN)  # the id is stable and says so

    def test_two_aliases_of_one_run_get_their_own_identity(self):
        # W12 compares targets in one run: if provenance were computed once and shared, every
        # alias would report the target the route happens to be live on
        with patch.object(cli, 'evaluation_model',
                          side_effect=lambda alias, thinking, provider, agent_id=None: {'id': alias}):
            self.invoke('--alias', 'eval-worker-b', '--trial', '1')
        provenances = {spec['alias']: result['provenance'] for spec, _, result in self.results()}
        self.assertEqual(set(provenances), {'eval', 'eval-worker-b'})
        self.assertEqual(provenances['eval']['target'], 'worker-a')
        self.assertEqual(provenances['eval-worker-b']['target'], 'worker-b')
        self.assertNotEqual(provenances['eval']['config_id'], provenances['eval-worker-b']['config_id'])
        self.assertEqual(provenances['eval-worker-b']['hw_fp'], identities.UNKNOWN)  # worker-b was never inventoried

    def test_a_checkout_git_cannot_answer_says_so(self):
        with patch.object(cli, 'revision', return_value=(None, None)):
            prov = cli.provenance('eval-worker-a', '1.3.0', root=self.root)
        self.assertEqual(prov['unknowns']['revision'], 'git did not answer')
        self.assertIsNone(prov['revision'])

    def test_the_revision_of_this_checkout_is_readable(self):
        commit, dirty = cli.revision(ROOT)
        self.assertRegex(commit, r'^[0-9a-f]{7,40}$')
        self.assertIsInstance(dirty, bool)


class AttemptIdentityTests(RunHarness):
    """What a stored attempt says about who ran it and what served it."""

    def test_the_provenance_names_the_agent_that_ran(self):
        with patch.object(cli.declared, 'load', side_effect=OSError('no graph')), \
                patch.object(cli, 'revision', return_value=('abc', False)):
            self.assertEqual(cli.provenance('eval', '0.165.0', root=self.root, agent='codex')['agent'],
                             {'name': 'codex', 'version': '0.165.0'})

    def test_provenance_is_read_for_each_attempt_so_a_route_change_is_seen(self):
        seen = iter(['first', 'first', 'second', 'third'])
        with patch.object(cli, 'provenance', side_effect=lambda *a, **k: {'target': next(seen)}):
            self.invoke('--trial', '0', '--trial', '1')
        stored = sorted(cli.latest_results(cli.Store(self.data / 'state.sqlite')), key=lambda row: row[0]['trial'])
        self.assertEqual([row[2]['provenance']['target'] for row in stored], ['first', 'second'])

    def test_a_capability_that_cannot_be_written_does_not_cost_the_attempt(self):
        with patch.object(cli.adapters, 'record_capability', side_effect=OSError('disk full')), \
                patch.object(cli, 'provenance', return_value={}):
            self.invoke('--trial', '0')
        self.assertEqual(len(cli.latest_results(cli.Store(self.data / 'state.sqlite'))), 1)


class ReportLabelTests(unittest.TestCase):
    def test_two_agents_on_one_alias_are_two_rows(self):
        self.assertEqual(cli.label({'alias': 'a'}), 'a')          # pi's rows keep the label they always had
        self.assertEqual(cli.label({'alias': 'a', 'agent': 'claude-code'}), 'a [claude-code]')
        self.assertEqual(cli.label({'alias': 'a', 'agent': 'claude-code', 'thinking': 'low'}),
                         'a [claude-code] +thinking-low')


if __name__ == '__main__':
    unittest.main()
