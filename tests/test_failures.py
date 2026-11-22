"""T11.2: the failure classes, judged on whether each one is reachable, checkable, and earned.

Three properties, one per class of mistake this command could make.

*Every class is reached by the field it names, and nothing else.* A rule that fires on a field it does
not print would be a guess with a label on it, so each class is produced by a result carrying that one
field and no earlier one, and the reason is required to contain the value.

*Absence is not a symptom.* An adapter that reports fewer fields must not look like a worse Agent, so
the same verdicts are produced from results that carry no `assistant_turns`, no `diff_stat` and no
`stop_reason` at all.

*The trace is not read.* The private checkout has no `artifacts/suite/runs/` directory, so any attempt
to open a trace would be an attempt to open a file that does not exist.
"""
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import command, failures, state  # noqa: E402

TASK = 'sample-m6-strict-config'
START = datetime.datetime(2026, 10, 30, 14, 0, tzinfo=datetime.timezone.utc)


def stamp(offset_minutes):
    return (START + datetime.timedelta(minutes=offset_minutes)).timestamp()


class Checkout:
    """Minimal suite checkout: task manifests and a suite SQLite store."""

    def __init__(self, case):
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def task(self, name, **manifest):
        folder = self.root / 'benchmarks/suite/tasks'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{name}.json'
        path.write_text(json.dumps(manifest, sort_keys=True))
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def sha256_of(self, name):
        path = self.root / 'benchmarks/suite/tasks' / f'{name}.json'
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else 'sha'

    def suite(self, attempts):
        path = self.root / 'artifacts/suite/state.sqlite'
        store = state.Store(path)
        cases = []
        for alias, task, trial, stored in attempts:
            spec = {'suite': 'v1', 'frontend': 'pi 1.3.0', 'alias': alias, 'model': {'id': alias},
                    'image': 'sha256:abc', 'task': task, 'task_sha256': self.sha256_of(task), 'trial': trial,
                    'timeout_s': 1800}
            cases.append((store.register(spec), trial, stored))
        db = store.connect()
        try:
            for case, trial, stored in cases:
                db.execute('UPDATE cases SET status=?, attempts=1 WHERE id=?',
                           ('complete' if stored.get('passed') else 'failed', case))
                db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)',
                           (case, 1, stamp(trial), stamp(trial), 'complete' if stored.get('passed') else 'failed',
                            json.dumps(stored)))
            db.commit()
        finally:
            db.close()
        return path


def attempt(passed=True, wall=100.0, tool_errors=1, regression_total=100, regression_failed=(), **extra):
    return {'passed': passed, 'wall_s': wall, 'tool_errors': tool_errors, 'assistant_turns': 4,
            'regression_total': regression_total, 'regression_failed': list(regression_failed),
            'timed_out': False, **extra}


def result(**fields):
    """A stored result carrying exactly what the test is about, and none of the fields an earlier rule reads."""
    return dict(fields, passed=fields.pop('passed', False))


class ClassifyTests(unittest.TestCase):
    """One rule, one field, one verdict. The order of RULES is part of the claim, so it is tested as such."""

    CASES = (
        (result(infrastructure_error='TimeoutExpired: docker stop'), 'infrastructure', 'TimeoutExpired'),
        (result(timed_out=True, wall_s=1800.2), 'timeout', '1800.2'),
        ({}, 'no-result', None),
        (result(frontend_returncode=137), 'agent-crashed', '137'),
        (result(assistant_turns=0, frontend_returncode=0), 'no-answer', None),
        (result(pytest_returncode=4), 'grader-error', '4'),
        (result(diff_stat='   \n'), 'no-change', None),
        (result(stop_reason='length'), 'output-capped', None),
        (result(acceptance_failed=['tests/test_a.py::one'], acceptance_total=3), 'acceptance-missed', '3'),
        (result(regression_failed=['tests/test_b.py::two'], regression_total=10), 'regression-broken', '10'),
        (result(acceptance_failed=['tests/test_a.py::one'], regression_failed=['tests/test_b.py::two']),
         'both-failed', None),
        (result(), 'unexplained', None),
    )

    def test_every_class_is_reached_by_the_field_it_names(self):
        for stored, name, _ in self.CASES:
            with self.subTest(cls=name):
                self.assertEqual(failures.classify(stored)['class'], name)

    def test_the_reason_carries_the_value_the_rule_read(self):
        for stored, name, value in self.CASES:
            if value is None:
                continue
            with self.subTest(cls=name):
                self.assertIn(value, failures.classify(stored)['reason'])

    def test_the_fields_are_printed_so_the_reader_needs_no_trace(self):
        classified = failures.classify(result(frontend_returncode=137))
        self.assertEqual(classified['fields']['frontend_returncode'], 137)

    def test_a_long_diff_is_one_line_because_this_is_a_screen_and_not_a_log(self):
        classified = failures.classify(result(diff_stat='src/a.py | 12 ++++++\nsrc/b.py | 3 +--\n'))
        self.assertEqual(classified['fields']['diff_stat'], ['src/a.py | 12 ++++++'])

    def test_an_attempt_that_passed_has_no_class_at_all(self):
        self.assertIsNone(failures.classify({'passed': True, 'acceptance_failed': []})['class'])

    # --- the order -----------------------------------------------------------------------------------

    def test_a_crashed_agent_is_not_reported_as_an_agent_that_changed_nothing(self):
        """The order of the rules is the argument: a dead process leaves no diff, no turns and no pytest run."""
        dead = result(frontend_returncode=137, assistant_turns=0, diff_stat='', pytest_returncode=1,
                      acceptance_failed=['tests/test_a.py::one'])
        self.assertEqual(failures.classify(dead)['class'], 'agent-crashed')

    def test_a_killed_attempt_is_not_reported_as_a_crash_even_though_the_runner_recorded_no_exit_code(self):
        killed = result(timed_out=True, wall_s=1800.0, frontend_returncode=None, assistant_turns=0, diff_stat='')
        self.assertEqual(failures.classify(killed)['class'], 'timeout')

    def test_a_broken_grader_outranks_what_the_tests_did_not_do(self):
        """pytest exiting 4 says the grading itself failed; the failing names below it are not evidence of anything."""
        broken = result(pytest_returncode=4, diff_stat='src/a.py | 1 +\n',
                        acceptance_failed=['tests/test_a.py::one'], regression_failed=['tests/test_b.py::two'])
        self.assertEqual(failures.classify(broken)['class'], 'grader-error')

    def test_every_pytest_exit_that_is_not_a_test_failure_is_the_graders_and_not_the_agents(self):
        """pytest says 1 for "a test failed" and 2-5 for "the run itself broke".

        The distinction is worth the table: 2 and 5 are what a suite that cannot even import produces,
        which is the case that names 700 tests and blames the model for a broken harness.
        """
        for code in (2, 3, 4, 5):
            with self.subTest(pytest_returncode=code):
                self.assertEqual(failures.classify(result(pytest_returncode=code,
                                                          acceptance_failed=['tests/test_a.py::one']))['class'],
                                 'grader-error')
        for code in (0, 1):
            with self.subTest(pytest_returncode=code):
                self.assertEqual(failures.classify(result(pytest_returncode=code,
                                                          acceptance_failed=['tests/test_a.py::one']))['class'],
                                 'acceptance-missed')

    def test_an_answer_cut_off_at_the_cap_is_not_reported_as_a_model_that_missed_the_acceptance_tests(self):
        capped = result(stop_reason='length', acceptance_failed=['tests/test_a.py::one'])
        self.assertEqual(failures.classify(capped)['class'], 'output-capped')

    # --- absence -------------------------------------------------------------------------------------

    def test_a_field_the_adapter_never_reported_is_not_a_symptom(self):
        """The same verdict from a result with nothing in it but the grading: an adapter that parses less is not worse."""
        thin = result(acceptance_failed=['tests/test_a.py::one'])
        self.assertNotIn('assistant_turns', thin)
        self.assertNotIn('diff_stat', thin)
        self.assertNotIn('stop_reason', thin)
        self.assertEqual(failures.classify(thin)['class'], 'acceptance-missed')

    def test_a_field_present_but_empty_is_not_the_same_as_a_field_that_is_missing(self):
        self.assertEqual(failures.classify(result(assistant_turns=None,
                                                  acceptance_failed=['tests/test_a.py::one']))['class'],
                         'acceptance-missed')
        self.assertEqual(failures.classify(result(assistant_turns=0))['class'], 'no-answer')

    def test_unexplained_is_reached_only_when_the_verdict_and_the_fields_disagree(self):
        self.assertEqual(failures.classify(result())['class'], 'unexplained')
        self.assertIn('disagree', failures.classify(result())['reason'])

    def test_the_classes_the_command_can_give_are_the_rules_plus_the_one_that_admits_ignorance(self):
        self.assertEqual(failures.CLASSES, tuple(name for name, _, _ in failures.RULES) + ('unexplained',))
        self.assertEqual({name for _, name, _ in self.CASES}, set(failures.CLASSES))


class VerdictTests(unittest.TestCase):
    """The rules explain a failure. They do not overrule a pass, and a pass is not counted twice."""

    def test_an_attempt_the_grader_passed_has_no_failure_class(self):
        for symptom in ({'timed_out': True}, {'stop_reason': 'length'}, {'frontend_returncode': 137},
                        {'diff_stat': ''}, {'assistant_turns': 0}):
            self.assertIsNone(failures.classify({'passed': True, **symptom})['class'], symptom)

    def test_the_same_symptoms_still_classify_a_failed_attempt(self):
        self.assertEqual(failures.classify({'passed': False, 'timed_out': True})['class'], 'timeout')
        self.assertEqual(failures.classify({'passed': False, 'stop_reason': 'length'})['class'], 'output-capped')

    def test_an_attempt_with_no_verdict_is_still_read_from_its_fields(self):
        self.assertEqual(failures.classify({'timed_out': True})['class'], 'timeout')

    def test_infrastructure_outranks_a_pass_because_nothing_was_measured(self):
        self.assertEqual(failures.classify({'passed': True, 'infrastructure_error': 'x'})['class'], 'infrastructure')

    def test_the_counts_add_up_when_a_passed_attempt_timed_out(self):
        checkout = Checkout(self)
        checkout.task('t')
        checkout.suite([('worker-a', 't', 0, attempt(passed=True, timed_out=True)),
                        ('worker-a', 't', 1, attempt(passed=False)),
                        ('worker-a', 't', 2, attempt(passed=True))])
        report = failures.summarise(checkout.root)
        self.assertEqual((report['attempts'], report['passed'], report['failed']), (3, 2, 1))
        self.assertEqual(report['passed'] + report['failed'] + report['infrastructure'], report['attempts'])


class ReportTests(unittest.TestCase):
    """`eval suite report --failures`: the counts, from the database, with no trace on disk."""

    def setUp(self):
        self.checkout = Checkout(self)
        self.root = self.checkout.root
        self.checkout.task(TASK, id=TASK, acceptance=['tests/test_a.py::one'],
                           regression=['tests/test_b.py::two'])

    def run_suite(self, *cases):
        self.checkout.suite([(alias, TASK, trial, stored) for alias, trial, stored in cases])

    def test_the_counts_add_up_and_infrastructure_is_counted_apart_from_the_model(self):
        self.run_suite(('worker-a', 0, attempt(passed=True)),
                       ('worker-a', 1, attempt(acceptance_failed=['tests/test_a.py::one'], passed=False)),
                       ('worker-a', 2, result(infrastructure_error='FileNotFoundError: git')))
        report = failures.summarise(self.root)
        self.assertEqual(report['attempts'], 3)
        self.assertEqual(report['passed'], 1)
        self.assertEqual(report['failed'], 1)
        self.assertEqual(report['infrastructure'], 1)
        self.assertEqual([entry['class'] for entry in report['classes']], ['acceptance-missed'])
        self.assertEqual(report['classes'][0]['share'], 1.0)

    def test_an_infrastructure_error_is_never_a_share_of_the_failures_it_is_not_part_of(self):
        """Two infrastructure errors and one real failure: a 200% share would be the arithmetic of a lie."""
        self.run_suite(('worker-a', 0, result(infrastructure_error='OSError: docker')),
                       ('worker-a', 1, result(infrastructure_error='OSError: docker')),
                       ('worker-a', 2, attempt(acceptance_failed=['tests/test_a.py::one'], passed=False)))
        report = failures.summarise(self.root)
        self.assertEqual(report['infrastructure'], 2)
        self.assertEqual([entry['class'] for entry in report['classes']], ['acceptance-missed'])
        self.assertEqual(report['classes'][0]['share'], 1.0)

    def test_a_case_whose_manifest_was_edited_since_is_kept_and_reported_as_hidden(self):
        self.run_suite(('worker-a', 0, attempt(acceptance_failed=['tests/test_a.py::one'], passed=False)))
        self.checkout.task(TASK, id=TASK, acceptance=['tests/test_a.py::one'],
                           regression=['tests/test_b.py::two', 'tests/test_c.py::three'])
        report = failures.summarise(self.root)
        self.assertEqual(report['attempts'], 0)
        self.assertEqual(report['superseded'], 1)

    def test_the_failing_names_are_grouped_because_the_aggregate_cannot_answer_them(self):
        self.run_suite(('worker-a', 0, attempt(acceptance_failed=['tests/test_a.py::one'], passed=False)),
                       ('worker-a', 1, attempt(acceptance_failed=['tests/test_a.py::one'], passed=False)),
                       ('worker-b', 0, attempt(regression_failed=['tests/test_b.py::two'], passed=False)))
        grouped = {item['test']: item for item in failures.summarise(self.root)['test_ids']}
        self.assertEqual(grouped['tests/test_a.py::one']['attempts'], 2)
        self.assertEqual(grouped['tests/test_a.py::one']['kind'], 'acceptance')
        self.assertEqual(grouped['tests/test_b.py::two']['kind'], 'regression')
        self.assertEqual(grouped['tests/test_b.py::two']['aliases'], ['worker-b'])

    def test_a_name_that_fails_as_both_kinds_is_named_once_and_says_so(self):
        self.run_suite(('worker-a', 0, attempt(acceptance_failed=['tests/test_a.py::one'],
                                               regression_failed=['tests/test_a.py::one'], passed=False)))
        self.assertEqual(failures.summarise(self.root)['test_ids'],
                         [{'test': 'tests/test_a.py::one', 'attempts': 1, 'kind': 'acceptance+regression',
                           'aliases': ['worker-a']}])

    def test_the_report_needs_no_trace_on_disk_because_reading_one_is_the_thing_it_replaces(self):
        self.run_suite(('worker-a', 0, attempt(acceptance_failed=['tests/test_a.py::one'], passed=False)))
        self.assertFalse((self.root / 'artifacts/suite/runs').exists())
        self.assertIn('acceptance-missed', failures.markdown(failures.summarise(self.root)))

    def test_the_screen_shows_the_reason_and_where_the_trace_would_be_if_anyone_needed_it(self):
        self.run_suite(('worker-a', 0, attempt(frontend_returncode=137, passed=False)))
        screen = failures.markdown(failures.summarise(self.root))
        self.assertIn('the Agent process exited 137', screen)
        self.assertIn('artifacts/suite/runs/', screen)

    def test_a_run_with_nothing_to_classify_says_that_rather_than_printing_an_empty_table(self):
        self.run_suite(('worker-a', 0, attempt(passed=True)))
        self.assertIn('no failures to classify', failures.markdown(failures.summarise(self.root)))

    def test_one_alias_and_one_task_are_askable_because_the_database_holds_every_alias_ever_run(self):
        self.run_suite(('worker-a', 0, attempt(acceptance_failed=['tests/test_a.py::one'], passed=False)),
                       ('worker-b', 0, attempt(frontend_returncode=2, passed=False)))
        self.assertEqual(failures.summarise(self.root, alias='worker-b')['failed'], 1)
        self.assertEqual(failures.summarise(self.root, alias='nobody')['attempts'], 0)
        self.assertEqual(failures.summarise(self.root, task='another-task')['attempts'], 0)

    def test_the_json_carries_the_same_numbers_the_screen_prints(self):
        self.run_suite(('worker-a', 0, attempt(acceptance_failed=['tests/test_a.py::one'], passed=False)))
        report = failures.summarise(self.root)
        self.assertEqual(json.loads(json.dumps(report))['failed'], report['failed'])
        self.assertEqual(report['schema_version'], 1)

    def test_a_flood_of_names_is_grouped_by_file_rather_than_printed(self):
        """A suite that failed at import names 700 tests and says one thing; the record still holds them all."""
        many = [f'tests/test_flood.py::t{index}' for index in range(failures.TOP_NAMES + 5)]
        self.run_suite(('worker-a', 0, attempt(regression_failed=many, passed=False)))
        report = failures.summarise(self.root)
        self.assertEqual(len(report['test_ids']), failures.TOP_NAMES + 5)
        screen = failures.markdown(report)
        self.assertEqual(screen.count('| `tests/test_flood.py::'), failures.TOP_NAMES)
        self.assertIn('and 5 more names, in tests/test_flood.py (5)', screen)


class ComparisonTests(unittest.TestCase):
    """The name-to-name comparison PA §9.3 could not make from aggregates: `--against`."""

    def setUp(self):
        self.checkout = Checkout(self)
        self.root = self.checkout.root
        self.checkout.task(TASK, id=TASK, acceptance=['tests/test_a.py::one'],
                           regression=['tests/test_b.py::two', 'tests/test_c.py::three'])

    def compare(self, candidate='candidate', against='incumbent'):
        return failures.new_regressions(self.root, candidate, against)

    def test_a_name_that_breaks_for_the_candidate_and_not_for_the_incumbent_is_the_new_class(self):
        self.checkout.suite([('incumbent', TASK, 0, attempt(passed=True)),
                             ('candidate', TASK, 0, attempt(regression_failed=['tests/test_b.py::two'],
                                                            passed=False))])
        comparison = self.compare()
        self.assertEqual(comparison['verdict'], 'new-regression')
        self.assertEqual(comparison['new'], [{'test': 'tests/test_b.py::two', 'task': TASK}])

    def test_a_name_both_aliases_fail_is_not_new(self):
        broken = attempt(regression_failed=['tests/test_b.py::two'], passed=False)
        self.checkout.suite([('incumbent', TASK, 0, broken), ('candidate', TASK, 0, broken)])
        comparison = self.compare()
        self.assertEqual(comparison['verdict'], 'no-new-regression')
        self.assertEqual(comparison['new'], [])
        self.assertIn('no test id fails for the candidate that the incumbent passes',
                      failures.markdown_comparison(comparison))

    def test_a_task_the_incumbent_never_ran_is_said_to_be_uncompared_rather_than_clean(self):
        """A candidate may fail a name nobody compared: that is a gap in the record, not a clean bill."""
        other = 'sample-m7-other'
        self.checkout.task(other, id=other, acceptance=[], regression=['tests/test_d.py::four'])
        self.checkout.suite([('incumbent', TASK, 0, attempt(passed=True)),
                             ('candidate', other, 0, attempt(regression_failed=['tests/test_d.py::four'],
                                                             passed=False))])
        comparison = self.compare()
        self.assertEqual(comparison['unknown_tasks'], [other])
        self.assertEqual(comparison['verdict'], 'insufficient-evidence')
        self.assertIn('never ran for the incumbent', failures.markdown_comparison(comparison))

    def test_an_incumbent_whose_only_attempt_was_an_infrastructure_error_did_not_run_the_task(self):
        """A row that measured nothing cannot be the baseline that says a test id is clean."""
        self.checkout.suite([('incumbent', TASK, 0, result(infrastructure_error='OSError: docker')),
                             ('candidate', TASK, 0, attempt(regression_failed=['tests/test_b.py::two'],
                                                            passed=False))])
        comparison = self.compare()
        self.assertEqual(comparison['unknown_tasks'], [TASK])
        self.assertEqual(comparison['verdict'], 'insufficient-evidence')

    def test_a_side_that_never_ran_at_all_is_insufficient_evidence_and_not_a_clean_bill(self):
        self.checkout.suite([('candidate', TASK, 0, attempt(passed=True))])
        self.assertEqual(self.compare()['verdict'], 'insufficient-evidence')
        self.assertEqual(self.compare(candidate='nobody')['verdict'], 'insufficient-evidence')

    def test_the_clean_bill_is_not_printed_when_nothing_was_compared(self):
        """The sentence is the whole product of this command; printing it without a comparison is the lie it exists to avoid."""
        self.checkout.suite([('candidate', TASK, 0, attempt(passed=True))])
        screen = failures.markdown_comparison(self.compare())
        self.assertNotIn('no test id fails for the candidate that the incumbent passes', screen)
        self.assertIn('nothing new to name', screen)

    def test_the_verdict_is_one_of_three_values_and_the_screen_leads_with_it(self):
        self.checkout.suite([('incumbent', TASK, 0, attempt(passed=True)),
                             ('candidate', TASK, 0, attempt(passed=True))])
        comparison = self.compare()
        self.assertIn(comparison['verdict'], ('new-regression', 'no-new-regression', 'insufficient-evidence'))
        self.assertTrue(failures.markdown_comparison(comparison).startswith('candidate against incumbent: **'))

    def test_a_flood_of_new_names_is_counted_and_grouped_rather_than_dumped(self):
        many = [f'tests/test_flood.py::t{index}' for index in range(failures.TOP_NAMES + 30)]
        self.checkout.suite([('incumbent', TASK, 0, attempt(passed=True)),
                             ('candidate', TASK, 0, attempt(regression_failed=many, passed=False))])
        comparison = self.compare()
        self.assertEqual(len(comparison['new']), failures.TOP_NAMES + 30)
        screen = failures.markdown_comparison(comparison)
        self.assertEqual(screen.count('| `tests/test_flood.py::'), failures.TOP_NAMES)
        self.assertIn('and 30 more new names, in tests/test_flood.py (30)', screen)


class AgainstUsageTests(unittest.TestCase):
    def test_against_without_an_alias_is_refused_because_no_alias_means_all_of_them(self):
        root = Path(failures.__file__).resolve().parents[2]
        done = subprocess.run([sys.executable, str(root / 'scripts/suite.py'), 'report', '--failures',
                               '--against', 'worker-b'], capture_output=True, text=True,
                              env=dict(os.environ, PYTHONPATH=str(root / 'src')))
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn('--against needs --alias', done.stderr)
        self.assertEqual(done.stdout, '')


class CommandTests(unittest.TestCase):
    """PA §10 and §14 spell the command `eval report --failures`; ADR 0006 keeps the logic out of command.py."""

    def args(self, *argv):
        return command.build_parser().parse_args(list(argv))

    def test_the_spelling_the_plan_uses_reaches_the_suite_report(self):
        argv = command.evaluate(self.args('eval', 'report', '--failures', '--alias', 'worker-a',
                                          '--against', 'worker-b', '--json'))
        self.assertEqual(argv[1:], [str(command.CODE / 'scripts/suite.py'), 'report', '--failures',
                                    '--alias', 'worker-a', '--against', 'worker-b', '--json'])

    def test_a_benchmark_report_still_works_because_additions_are_all_PA_13_allows(self):
        argv = command.evaluate(self.args('eval', 'report', 'artifacts/benchmarks/worker-c/state.sqlite'))
        self.assertIn('aggregate-results.py', argv[1])
        self.assertEqual(argv[2:], ['artifacts/benchmarks/worker-c/state.sqlite', '--format', 'markdown'])

    def test_a_report_with_neither_a_database_nor_the_flag_is_refused_rather_than_guessing(self):
        with self.assertRaises(SystemExit) as raised, patch('sys.stderr', io.StringIO()):
            command.evaluate(self.args('eval', 'report'))
        self.assertIn('state.sqlite', str(raised.exception))


class RuleOrderTests(unittest.TestCase):
    def test_an_answer_cut_off_at_the_cap_is_the_cap_even_with_no_diff(self):
        self.assertEqual(failures.classify({'passed': False, 'stop_reason': 'length', 'diff_stat': ''})['class'],
                         'output-capped')

    def test_no_change_still_answers_when_nothing_cut_the_agent_off(self):
        self.assertEqual(failures.classify({'passed': False, 'stop_reason': 'stop', 'diff_stat': ''})['class'],
                         'no-change')

    def test_a_grader_error_does_not_say_the_grader_is_to_blame(self):
        found = failures.classify({'passed': False, 'pytest_returncode': 2})
        self.assertEqual(found['class'], 'grader-error')
        self.assertIn("Agent's edit", found['reason'])


class LikeForLikeTests(unittest.TestCase):
    """`--against` compares the same Agent at the same settings, or it says nothing."""

    def setUp(self):
        self.checkout = Checkout(self)
        self.checkout.task('t')

    def respec(self, path, **extra):
        with sqlite3.connect(path) as db:
            for row_id, spec in db.execute('SELECT id, spec FROM cases').fetchall():
                spec = json.loads(spec)
                if spec['alias'] == 'incumbent':
                    db.execute('UPDATE cases SET spec=? WHERE id=?', (json.dumps({**spec, **extra}), row_id))

    def test_a_failure_in_another_thinking_level_does_not_hide_a_regression(self):
        name = 'tests/test_a.py::one'
        self.checkout.suite([('candidate', 't', 0, attempt(passed=False, regression_failed=[name]))])
        path = self.checkout.suite([('incumbent', 't', 0, attempt(passed=False, regression_failed=[name]))])
        self.respec(path, thinking='medium')
        comparison = failures.new_regressions(self.checkout.root, 'candidate', 'incumbent')
        self.assertEqual(comparison['new'], [])
        self.assertEqual(comparison['compared_tasks'], [])
        self.assertEqual(comparison['unknown_tasks'], ['t'])
        self.assertEqual(comparison['verdict'], 'insufficient-evidence')

    def test_an_attempt_with_no_test_verdict_names_no_regression(self):
        self.checkout.suite([('candidate', 't', 0, attempt(passed=False, pytest_returncode=2,
                                                           regression_failed=['tests/test_a.py::one'])),
                             ('incumbent', 't', 0, attempt(passed=True))])
        self.assertEqual(failures.new_regressions(self.checkout.root, 'candidate', 'incumbent')['new'], [])


class InfrastructureCountTests(unittest.TestCase):
    def test_a_passed_attempt_with_an_infrastructure_error_is_counted_once(self):
        checkout = Checkout(self)
        checkout.task('t')
        checkout.suite([('worker-a', 't', 0, attempt(passed=True, infrastructure_error='docker is down'))])
        report = failures.summarise(checkout.root)
        self.assertEqual((report['passed'], report['infrastructure'], report['attempts']), (0, 1, 1))


class AgentReportTests(unittest.TestCase):
    """T12.5: two Agents on one alias are two rows, and each can be asked about alone."""

    def setUp(self):
        self.checkout = Checkout(self)
        self.checkout.task('t')
        path = self.checkout.suite([('worker-a', 't', 0, attempt(passed=False)),
                                    ('worker-a', 't', 1, attempt(passed=True))])
        with sqlite3.connect(path) as db:
            for row_id, spec in db.execute('SELECT id, spec FROM cases').fetchall():
                spec = json.loads(spec)
                if spec['trial'] == 1:
                    db.execute('UPDATE cases SET spec=? WHERE id=?',
                               (json.dumps({**spec, 'agent': 'claude-code'}), row_id))

    def test_the_baseline_has_no_agent_key_and_is_still_pi(self):
        report = failures.summarise(self.checkout.root, agent='pi')
        self.assertEqual((report['attempts'], report['failed']), (1, 1))

    def test_another_agent_is_filtered_by_its_name(self):
        report = failures.summarise(self.checkout.root, agent='claude-code')
        self.assertEqual((report['attempts'], report['passed']), (1, 1))

    def test_no_filter_still_sees_both(self):
        self.assertEqual(failures.summarise(self.checkout.root)['attempts'], 2)

    def test_a_cap_is_output_capped_under_either_agents_word_for_it(self):
        for reason in ('length', 'max_tokens'):
            self.assertEqual(failures.classify({'passed': False, 'stop_reason': reason})['class'], 'output-capped')


if __name__ == '__main__':
    unittest.main()
