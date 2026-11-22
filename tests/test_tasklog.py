"""T8.7: one JSONL line per real task, judged on whether it can still be read in a year.

Three claims, one per way a usage log dies.

*Append-only.* A log someone can edit is an opinion, so a second call adds a second line and a
refused call leaves the file byte for byte as it was.

*No body.* Every field is declared, most from a vocabulary, and the record cannot carry a sentence.
The manual equivalent PA L8 complains about drifted into prose; this one refuses to.

*Categories are declared.* T6.4 asks for the top three failure categories, which only works if the
same failure gets the same name twice, so the vocabulary is in the code and the roadmap is the source
of the four outcomes.
"""
import datetime
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from agentiac import command, paths, tasklog  # noqa: E402
SITE = paths.root()

GOOD = dict(repository='sample-service', type='coding', outcome='rejected', minutes=23,
            failure='output-capped', agent='pi')


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write_route(self, target):
        (self.root / 'artifacts/gateway').mkdir(parents=True, exist_ok=True)
        (self.root / 'artifacts/gateway/route.json').write_text(json.dumps({'target': target}))

    def write_declared_default(self, target):
        (self.root / 'gateway').mkdir(parents=True, exist_ok=True)
        (self.root / 'gateway/local-coder.json').write_text(json.dumps({'default': target}))

    def file(self):
        return self.root / tasklog.PATH

    def logged(self):
        return self.file().read_text().splitlines() if self.file().exists() else []


class AppendTests(Fixture):
    """The file only ever gets longer, and a call that is refused leaves it exactly as it was."""

    def test_one_call_appends_exactly_one_line_at_the_documented_path(self):
        tasklog.record(self.root, **GOOD)
        self.assertEqual(len(self.logged()), 1)
        self.assertEqual(self.file(), self.root / 'artifacts/tasks/tasks.jsonl')

    def test_a_second_call_adds_a_second_line_rather_than_replacing_the_first(self):
        tasklog.record(self.root, **GOOD)
        tasklog.record(self.root, **dict(GOOD, outcome='accepted', failure=None, minutes=4))
        self.assertEqual([json.loads(line)['outcome'] for line in self.logged()], ['rejected', 'accepted'])

    def test_a_refused_call_leaves_the_file_byte_for_byte_as_it_was(self):
        tasklog.record(self.root, **GOOD)
        before = self.file().read_bytes()
        with self.assertRaises(ValueError):
            tasklog.record(self.root, **dict(GOOD, repository='/srv/user/work/sample-service'))
        self.assertEqual(self.file().read_bytes(), before)

    def test_the_line_carries_exactly_the_declared_keys_because_an_extra_one_is_a_body_in_waiting(self):
        line = tasklog.record(self.root, **GOOD)
        self.assertEqual(set(line), set(tasklog.FIELDS))
        self.assertEqual(set(json.loads(self.logged()[0])), set(tasklog.FIELDS))

    def test_no_value_in_the_record_can_hold_a_sentence(self):
        tasklog.record(self.root, **dict(GOOD, failure='other', type='summarising'))
        for value in json.loads(self.logged()[0]).values():
            self.assertNotIn('\n', str(value))
            self.assertLessEqual(len(str(value)), 80)

    def test_the_time_is_stamped_and_a_test_can_pin_it(self):
        stamp = datetime.datetime(2026, 11, 1, 9, 0, tzinfo=datetime.timezone.utc).isoformat()
        line = tasklog.record(self.root, now=stamp, **GOOD)
        self.assertEqual(line['time'], stamp)


class VocabularyTests(Fixture):
    """The words are chosen once, here, and the roadmap is where the four outcomes come from."""

    def test_the_outcomes_are_the_four_the_roadmap_names(self):
        roadmap = SITE / 'docs/roadmap.md'
        if roadmap.exists():
            stated = next(block for block in roadmap.read_text().split('\n\n')
                          if 'log-task' in block and 'outcome' in block)
            for outcome in tasklog.OUTCOMES:
                self.assertIn(outcome, stated)
            for field in ('repository', 'task type', 'minutes', 'failure category'):
                self.assertIn(field, stated)
        else:
            self.assertEqual(set(tasklog.OUTCOMES), {'accepted', 'edited', 'rejected', 'abandoned'})
            self.assertLessEqual({'repository', 'type', 'minutes', 'failure'}, set(tasklog.FIELDS))

    def test_every_category_is_a_single_token_because_two_words_never_aggregate_with_two_other_words(self):
        for name in tasklog.FAILURES + tasklog.TYPES + tasklog.OUTCOMES:
            self.assertRegex(name, r'^[a-z0-9-]{1,40}$')

    def test_other_exists_so_that_something_new_stays_visible_instead_of_forced_into_a_wrong_bucket(self):
        self.assertIn('other', tasklog.FAILURES)
        self.assertIn('other', tasklog.TYPES)

    def test_two_categories_the_suite_already_measures_are_in_the_vocabulary(self):
        """`output-capped` and `timeout` are the two largest classes of the real suite; a log that cannot
        name them would push the same failures into `other` and lose the count."""
        self.assertIn('output-capped', tasklog.FAILURES)
        self.assertIn('timeout', tasklog.FAILURES)


class ValidationTests(Fixture):
    """A bad line is refused with every problem at once, because fixing one at a time is a negotiation."""

    def problems(self, **over):
        return tasklog.validate(**dict(GOOD, **over))

    def test_a_target_held_to_the_rule_a_repository_is_held_to(self):
        for prose in ('the whole prompt with spaces', '/srv/user/models/x', 'worker-a\nthe prompt was: fix it'):
            self.assertTrue(self.problems(target=prose), prose)
        self.assertFalse(self.problems(target='worker-a-strata-0.2.8-tiny-coder-q4-128k'))
        self.assertFalse(self.problems(target=None))

    def test_a_target_that_is_a_sentence_writes_nothing(self):
        with self.assertRaises(ValueError):
            tasklog.record(self.root, **dict(GOOD, target='the whole prompt with spaces'))
        self.assertFalse((self.root / tasklog.PATH).exists())

    def test_a_repository_that_is_a_path_is_refused_because_the_log_is_read_by_other_people(self):
        self.assertTrue(self.problems(repository='/srv/user/work/sample-service'))
        self.assertTrue(self.problems(repository='my work repo'))
        self.assertTrue(self.problems(repository='repo\nthe prompt was: fix the parser'))
        self.assertFalse(self.problems(repository='sample-service'))

    def test_a_repository_longer_than_a_name_is_refused(self):
        self.assertFalse(self.problems(repository='x' * 80))
        self.assertTrue(self.problems(repository='x' * 81))

    def test_a_failure_is_required_when_the_task_did_not_survive(self):
        for outcome in ('rejected', 'abandoned'):
            with self.subTest(outcome=outcome):
                self.assertTrue(self.problems(outcome=outcome, failure=None))
                self.assertFalse(self.problems(outcome=outcome, failure='wrong-code'))

    def test_a_failure_is_refused_on_a_task_that_succeeded_because_both_cannot_be_true(self):
        self.assertTrue(self.problems(outcome='accepted', failure='style'))
        self.assertFalse(self.problems(outcome='accepted', failure=None))

    def test_an_edited_task_may_carry_a_category_because_it_worked_with_a_caveat(self):
        self.assertFalse(self.problems(outcome='edited', failure='style'))
        self.assertFalse(self.problems(outcome='edited', failure=None))

    def test_a_category_nobody_declared_is_refused_rather_than_counted_apart_forever(self):
        self.assertTrue(self.problems(failure='the-model-was-tired-today'))

    def test_minutes_is_a_positive_number_and_a_day_is_not_one_task(self):
        for minutes in (0, -3, 'soon', None, 1441):
            with self.subTest(minutes=minutes):
                self.assertTrue(self.problems(minutes=minutes))
        self.assertFalse(self.problems(minutes=0.5))
        self.assertFalse(self.problems(minutes=1440))

    def test_an_agent_that_is_not_declared_is_refused(self):
        self.assertTrue(self.problems(agent='hermes'))
        self.assertFalse(self.problems(agent='pi'))

    def test_every_problem_arrives_at_once(self):
        problems = self.problems(repository='/tmp/x', type='daydreaming', outcome='maybe', minutes='soon')
        self.assertEqual(len(problems), 4)

    def test_the_message_says_what_to_type_rather_than_only_that_something_is_wrong(self):
        joined = ' '.join(self.problems(outcome='rejected', failure=None))
        self.assertIn('--failure', joined)


class TargetTests(Fixture):
    """The target is read, not assumed, and the two possible sources are not interchangeable."""

    def test_the_rendered_route_is_what_is_being_served_and_says_so(self):
        self.write_route('worker-a')
        self.assertEqual(tasklog.route_target(self.root), ('worker-a', 'route'))

    def test_the_declared_default_is_not_reported_as_what_is_being_served(self):
        """`gateway/local-coder.json` says what to serve when nothing was rendered. Calling that the
        live route would let a declared value answer an observed question."""
        self.write_declared_default('worker-b')
        self.assertEqual(tasklog.route_target(self.root), ('worker-b', 'declared-default'))

    def test_the_rendered_route_wins_over_the_declared_default(self):
        self.write_route('worker-a')
        self.write_declared_default('worker-b')
        self.assertEqual(tasklog.route_target(self.root), ('worker-a', 'route'))

    def test_nothing_is_invented_when_neither_file_can_be_read(self):
        self.assertEqual(tasklog.route_target(self.root), (None, 'unavailable'))
        line = tasklog.build(self.root, **GOOD)
        self.assertIsNone(line['target'])
        self.assertEqual(line['target_source'], 'unavailable')

    def test_a_told_target_is_marked_as_given_and_the_route_is_not_consulted(self):
        self.write_route('worker-a')
        line = tasklog.build(self.root, target='worker-c', **GOOD)
        self.assertEqual((line['target'], line['target_source']), ('worker-c', 'given'))


class ReadTests(Fixture):
    def test_a_log_that_does_not_exist_yet_is_empty_and_not_an_error(self):
        self.assertEqual(tasklog.lines(self.root), [])

    def test_a_line_that_does_not_parse_is_shown_as_broken_rather_than_dropped(self):
        self.file().parent.mkdir(parents=True)
        self.file().write_text('{"outcome": "accepted"}\nnot json at all\n')
        self.assertEqual(tasklog.lines(self.root),
                         [{'outcome': 'accepted'}, {'broken': 2, 'line': 'not json at all'}])


class CommandTests(unittest.TestCase):
    """PA §13: a misuse of the command exits 2, and a refused line never reaches the file."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / 'site'
        shutil.copytree(ROOT / 'examples/site', self.root)

    def file(self):
        return self.root / tasklog.PATH

    def logged(self):
        return self.file().read_text().splitlines() if self.file().exists() else []

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.stdout', out), patch('sys.stderr', err), patch.object(command, 'ROOT', self.root):
            code = command.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_prints_the_line_and_writes_nothing(self):
        code, out, _ = self.call('log-task', '--repo', 'sample-service', '--type', 'coding',
                                 '--outcome', 'rejected', '--minutes', '23', '--failure', 'output-capped',
                                 '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('output-capped', out)
        self.assertFalse(self.file().exists())

    def test_a_written_line_is_reported_with_the_path_it_went_to(self):
        code, out, _ = self.call('log-task', '--repo', 'sample-service', '--type', 'coding',
                                 '--outcome', 'accepted', '--minutes', '4')
        self.assertEqual(code, 0)
        self.assertIn(tasklog.PATH, out)
        self.assertEqual(len(self.logged()), 1)

    def test_a_misuse_exits_2_and_the_file_never_appears(self):
        code, _, err = self.call('log-task', '--repo', 'sample-service', '--type', 'coding',
                                 '--outcome', 'rejected', '--minutes', '23')
        self.assertEqual(code, 2)
        self.assertIn('--failure', err)
        self.assertFalse(self.file().exists())

    def test_json_prints_the_record_a_script_can_read(self):
        self.call('log-task', '--repo', 'sample-service', '--type', 'coding', '--outcome', 'accepted',
                  '--minutes', '4', '--json')
        self.assertEqual(json.loads(self.logged()[0])['minutes'], 4.0)


class EdgeTests(Fixture):
    def problems(self, **over):
        return tasklog.validate(**dict(GOOD, **over))

    def test_a_trailing_newline_is_whitespace(self):
        self.assertTrue(self.problems(repository='repo\n'))
        self.assertTrue(self.problems(target='worker-a\n'))

    def test_a_time_that_would_be_stored_as_zero_is_refused(self):
        self.assertTrue(self.problems(minutes=0.01))
        self.assertFalse(self.problems(minutes=0.1))

    def test_a_line_with_no_agent_is_written_rather_than_a_keyerror(self):
        fields = {key: value for key, value in GOOD.items() if key != 'agent'}
        self.assertIsNone(tasklog.build(self.root, **fields)['agent'])


if __name__ == '__main__':
    unittest.main()
