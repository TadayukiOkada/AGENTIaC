"""T13.2: the five observation sources read together, judged on the two things PA asks of them.

**No body.** Every source is fed a line carrying a sentinel in a key it is not allowed to surface —
a `prompt` on a router line, a `messages` on a gateway line, a `diff_stat` on a suite result — and the
sentinel must appear in neither the screen nor the JSON. The test asserts the sentinel really is in
the file first, so the assertion cannot pass by the fixture being empty.

**Retention stated, not assumed.** `absent` (never written), `empty` (written, no lines) and
`unavailable` (could not be read) are three answers and the screen gives all three; each source says
what keeps it and how old its oldest line is, because nothing prunes any of these today.
"""
import datetime
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from agentiac import command, observe, state  # noqa: E402

SENTINEL = 'THE-PROMPT-BODY-MUST-NEVER-LEAVE-HERE'
START = datetime.datetime(2026, 10, 30, 14, 0, tzinfo=datetime.timezone.utc)


def stamp(offset_minutes):
    return (START + datetime.timedelta(minutes=offset_minutes)).timestamp()


def utc_z(year, month, day, hour, minute=0):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def utc_iso(year, month, day, hour, minute=0):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc).isoformat()


def day_str(year, month, day):
    return datetime.date(year, month, day).isoformat()


class Checkout:
    """Minimal suite checkout: task manifests and a suite SQLite store."""

    def __init__(self, case, manifests=False):
        self.manifests = manifests
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shutil.copytree(ROOT / 'examples/site', self.root, dirs_exist_ok=True)

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
            if self.manifests and not (self.root / 'benchmarks/suite/tasks' / f'{task}.json').exists():
                self.task(task)
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


# One line per source, each with the declared keys it should be counted on and a body-shaped key it must not.
LINES = {
    'router': 'router-log.jsonl', 'loop-guard': 'loop-guard-log.jsonl',
    'compaction': 'compaction-guard-log.jsonl',
}
RECORDS = {
    'router': [{'time': utc_z(2026, 11, 1, 9, 0), 'repo': 'agentiac', 'tier': 'high',
                'model': 'cloud-alias', 'why': 'rules', 'score': 6, 'prompt': SENTINEL},
               {'time': utc_z(2026, 11, 1, 9, 1), 'repo': 'plain', 'tier': 'low',
                'model': 'local-coder', 'why': 'rules', 'score': 0, 'messages': [SENTINEL]}],
    'loop-guard': [{'time': utc_z(2026, 11, 1, 9, 2), 'verdict': 'nudge', 'streak': 11, 'text': SENTINEL}],
    'compaction': [{'time': utc_z(2026, 11, 1, 9, 3), 'event': 'summarized', 'reason': 'threshold',
                    'split_turn': True, 'tokens_before': 221372, 'usage': {'text': SENTINEL}}],
    'tasks': [{'time': utc_z(2026, 11, 1, 9, 4), 'repository': 'sample-service', 'type': 'coding',
               'outcome': 'rejected', 'minutes': 23, 'failure': 'output-capped', 'agent': 'pi',
               'target': 'worker-a', 'target_source': 'route', 'summary': SENTINEL}],
}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.checkout = Checkout(self, manifests=True)   # a real suite DB and the declared files
        self.root = self.checkout.root
        self.state = self.root / observe.PI_STATE
        self.state.mkdir(parents=True, exist_ok=True)
        for name, filename in LINES.items():
            (self.state / filename).write_text('\n'.join(json.dumps(line) for line in RECORDS[name]) + '\n')
        (self.root / 'artifacts/tasks').mkdir(parents=True, exist_ok=True)
        (self.root / 'artifacts/tasks/tasks.jsonl').write_text(
            json.dumps(RECORDS['tasks'][0]) + '\n')
        self.checkout.suite([('local-coder-worker-a', 'sample-m6-strict-config', 0, attempt(passed=True)),
                             ('local-coder-worker-a', 'sample-m6-strict-config', 1,
                              attempt(passed=False, timed_out=True, wall_s=1800.0, diff_stat=SENTINEL))])

    def report(self, journal=None, since=None, sources=observe.SOURCES):
        return observe.summary(self.root, sources=sources,
                               journal=journal or (lambda unit, since: ([], 0, [], 0, {})), since=since)

    def source(self, name, **kwargs):
        return next(item for item in self.report(**kwargs)['sources'] if item['source'] == name)


class StatusTests(Fixture):
    """`absent`, `empty` and `unavailable` are three different answers, and none of them is zero."""

    def test_a_file_that_was_never_written_is_absent_and_says_what_a_zero_would_have_claimed(self):
        (self.root / 'artifacts/tasks/tasks.jsonl').unlink()
        found = self.source('tasks')
        self.assertEqual(found['status'], 'absent')
        self.assertEqual(found['lines'], 0)
        self.assertIn('nobody asked', found['note'])

    def test_a_file_with_nothing_in_it_is_empty_rather_than_absent(self):
        (self.root / 'artifacts/tasks/tasks.jsonl').write_text('')
        self.assertEqual(self.source('tasks')['status'], 'empty')

    def test_a_journal_that_cannot_be_read_is_unavailable_and_not_a_quiet_day(self):
        found = self.source('gateway', journal=lambda unit, since: (None, 0, [], 0, {}))
        self.assertEqual(found['status'], 'unavailable')
        self.assertIn('not the same as no traffic', found['note'])

    def test_a_source_that_was_read_reports_its_lines_and_its_size(self):
        found = self.source('router')
        self.assertEqual(found['status'], 'ok')
        self.assertEqual(found['lines'], 2)
        self.assertGreater(found['bytes'], 0)

    def test_a_line_that_does_not_parse_is_counted_and_said_rather_than_vanishing(self):
        path = self.state / 'loop-guard-log.jsonl'
        path.write_text(path.read_text() + 'this is not json\n')
        found = self.source('loop-guard')
        self.assertEqual(found['lines'], 2)
        self.assertEqual(found['broken'], 1)
        self.assertIn('did not parse', found['note'])


class NoBodyTests(Fixture):
    """The completion condition: no message body leaves the files. Proved by a sentinel in hidden keys."""

    def test_the_sentinels_really_are_in_the_files_the_command_reads(self):
        """Without this the test below could pass because the fixture was empty rather than because the rule held."""
        for path in list(self.state.glob('*.jsonl')) + [self.root / 'artifacts/tasks/tasks.jsonl']:
            self.assertIn(SENTINEL, path.read_text(), str(path))
        import sqlite3
        with sqlite3.connect(self.root / observe.SUITE) as db:
            stored = db.execute('SELECT result FROM attempts').fetchall()
        self.assertIn(SENTINEL, str(stored))

    def test_no_body_reaches_the_screen_from_any_source(self):
        screen = observe.markdown(self.report())
        self.assertNotIn(SENTINEL, screen)

    def test_no_body_reaches_the_json_from_any_source(self):
        self.assertNotIn(SENTINEL, json.dumps(self.report()))

    def test_a_source_only_contributes_the_keys_it_declared(self):
        """`streak` is declared for the loop guard and not for the router; a router line carrying one is silent."""
        path = self.state / 'router-log.jsonl'
        path.write_text(path.read_text() + json.dumps({'time': utc_z(2026, 11, 1, 9, 5), 'streak': 99}) + '\n')
        self.assertNotIn('streak:99', self.source('router')['counts'])

    def test_the_keys_a_source_refuses_to_read_are_named_on_the_screen(self):
        """Names, never values. A reader who sees that the router log carries a `prompt` key learns there is
        more in there than this screen shows; a reader who sees its contents is reading a trace."""
        for name, key in (('router', 'prompt'), ('loop-guard', 'text'), ('compaction', 'usage'), ('tasks', 'summary')):
            with self.subTest(source=name):
                self.assertEqual(self.source(name)['dropped'].get(key), 1)
                self.assertIn(key, observe.markdown(self.report(sources=(
                    next(s for s in observe.SOURCES if s.source == name),))))

    def test_every_file_source_actually_carries_something_the_screen_drops(self):
        """If the surface list ever stopped filtering, `dropped` would go empty and this would say so —
        otherwise the allowlist would be a list nothing consults."""
        for name in ('router', 'loop-guard', 'compaction', 'tasks', 'suite'):
            with self.subTest(source=name):
                self.assertTrue(self.source(name)['dropped'], f'{name} dropped nothing, so nothing was filtered')

    def test_a_dropped_key_is_named_without_its_value_ever_appearing(self):
        found = self.source('router')
        self.assertIn('messages', found['dropped'])
        self.assertNotIn(SENTINEL, json.dumps(found))

    def test_a_record_with_none_of_the_counted_fields_contributes_nothing(self):
        path = self.state / 'router-log.jsonl'
        path.write_text(json.dumps({'time': utc_z(2026, 11, 1, 9, 6)}) + '\n')
        self.assertEqual(self.source('router')['counts'], {})
        self.assertEqual(self.source('router')['lines'], 1)

    def test_the_gateway_line_is_limited_to_the_fields_the_gateway_is_allowed_to_write(self):
        """The list lives in gateway_callbacks.py, which imports litellm and so cannot be imported here;
        the two texts are compared instead, or the allowlist would drift apart from the writer."""
        stated = (ROOT / 'gateway/gateway_callbacks.py').read_text()
        declared_fields = stated.split('LOG_FIELDS = (', 1)[1].split(')', 1)[0]
        self.assertEqual([name.strip().strip("'") for name in declared_fields.split(',') if name.strip()],
                         list(observe.GATEWAY_FIELDS))


class JournalTests(unittest.TestCase):
    """`read_journal` is the only part of this command that runs a program, so it is tested against a faked one."""

    def run_journal(self, stdout, since=None, returncode=0, error=None):
        self.argv = None

        def fake(argv, **kwargs):
            self.argv = argv
            if error:
                raise error
            return subprocess.CompletedProcess(argv, returncode, stdout, '')

        with patch.object(observe.subprocess, 'run', fake):
            return observe.read_journal(observe.GATEWAY_UNIT, since)

    @staticmethod
    def lines(*messages, stamp=1762000000):
        """Raw journald records: our JSON in MESSAGE, plus whatever else the unit logged."""
        return '\n'.join(json.dumps({'MESSAGE': json.dumps(message) if isinstance(message, dict) else message,
                                     '__REALTIME_TIMESTAMP': str(stamp * 10 ** 6)}) for message in messages)

    def test_only_the_inference_lines_of_the_unit_are_counted(self):
        """The unit logs plenty around our lines; a startup notice is not a request nobody made."""
        records, lines, times, broken, dropped = self.run_journal(self.lines(
            {'event': 'inference', 'status': 'success', 'node': 'worker-a'},
            'litellm started ok',
            {'event': 'healthcheck', 'status': 'ok'}))
        self.assertEqual(lines, 1)
        self.assertEqual(records[0]['node'], 'worker-a')

    def test_the_since_the_caller_asked_for_reaches_journalctl(self):
        since = day_str(2026, 10, 31)
        self.run_journal('', since=since)
        self.assertEqual(self.argv[-2:], ['--since', since])
        self.run_journal('')
        self.assertNotIn('--since', self.argv)

    def test_a_journalctl_that_cannot_be_run_or_that_fails_is_none_and_not_an_empty_journal(self):
        self.assertIsNone(self.run_journal('', error=OSError('journalctl not found'))[0])
        self.assertIsNone(self.run_journal('', returncode=1)[0])

    def test_a_body_key_on_a_gateway_line_is_named_and_never_shown(self):
        records, lines, times, broken, dropped = self.run_journal(self.lines(
            {'event': 'inference', 'status': 'success', 'messages': [SENTINEL]}))
        self.assertEqual(dropped, {'messages': 1})
        self.assertNotIn(SENTINEL, json.dumps(records))


class CountTests(Fixture):
    def test_the_router_is_counted_by_the_three_things_that_explain_a_choice(self):
        counts = self.source('router')['counts']
        self.assertEqual(counts['tier:high'], 1)
        self.assertEqual(counts['model:cloud-alias'], 1)
        self.assertEqual(counts['why:rules'], 2)

    def test_the_suite_is_counted_by_what_it_actually_stores(self):
        counts = self.source('suite')['counts']
        self.assertEqual(counts['passed:True'], 1)
        self.assertEqual(counts['passed:False'], 1)
        self.assertEqual(counts['timed_out:True'], 1)

    def test_the_task_log_is_counted_by_outcome_and_category_because_phase_3_asks_for_the_top_three(self):
        counts = self.source('tasks')['counts']
        self.assertEqual(counts['outcome:rejected'], 1)
        self.assertEqual(counts['failure:output-capped'], 1)

    def test_the_guards_are_counted_by_verdict_and_by_why_a_session_was_summarised(self):
        self.assertEqual(self.source('loop-guard')['counts'], {'verdict:nudge': 1})
        self.assertEqual(self.source('compaction')['counts'], {'event:summarized': 1, 'reason:threshold': 1})

    def test_the_gateway_counts_the_lines_the_journal_gave_and_only_those(self):
        records = [{'event': 'inference', 'status': 'success', 'node': 'worker-a', 'alias': 'local-coder'},
                   {'event': 'inference', 'status': 'error', 'node': 'worker-b'}]
        times = [datetime.datetime(2026, 11, 1, 9, 0, tzinfo=datetime.timezone.utc),
                 datetime.datetime(2026, 11, 1, 10, 0, tzinfo=datetime.timezone.utc)]
        found = self.source('gateway', journal=lambda unit, since: (records, 2, times, 0, {}))
        self.assertEqual(found['counts']['status:error'], 1)
        self.assertEqual(found['oldest'], utc_iso(2026, 11, 1, 9, 0))
        self.assertEqual(found['newest'], utc_iso(2026, 11, 1, 10, 0))


class SpanTests(Fixture):
    def test_the_oldest_and_newest_line_are_reported_because_retention_is_a_question_about_age(self):
        found = self.source('router')
        self.assertEqual(found['oldest'], utc_iso(2026, 11, 1, 9, 0))
        self.assertEqual(found['newest'], utc_iso(2026, 11, 1, 9, 1))

    def test_the_suite_span_comes_from_the_attempt_time_not_from_a_guess(self):
        found = self.source('suite')
        self.assertNotEqual(found['oldest'], found['newest'])
        self.assertTrue(found['oldest'].startswith(str(START.year) + '-'))

    def test_a_line_with_an_unreadable_time_is_counted_and_left_out_of_the_span(self):
        path = self.state / 'loop-guard-log.jsonl'
        path.write_text(path.read_text() + json.dumps({'time': 'yesterday', 'verdict': 'stop'}) + '\n')
        found = self.source('loop-guard')
        self.assertEqual(found['lines'], 2)
        self.assertEqual(found['oldest'], utc_iso(2026, 11, 1, 9, 2))


class RetentionTests(Fixture):
    def test_every_source_says_what_keeps_it_because_a_retention_nobody_configured_is_not_a_policy(self):
        for source in observe.SOURCES:
            with self.subTest(source=source.source):
                self.assertTrue(source.retention)
                self.assertRegex(source.retention.lower(), r'kept|prunes|retention')

    def test_the_screen_prints_the_retention_of_every_source_it_shows(self):
        screen = observe.markdown(self.report())
        for source in observe.SOURCES:
            self.assertIn(source.retention, screen)
            self.assertIn(f'`{source.ref}`', screen)

    def test_the_command_states_that_nothing_above_is_a_message_body(self):
        self.assertIn('refuses to read', observe.markdown(self.report()))


class PathTests(Fixture):
    def test_the_log_directory_is_the_one_the_writer_writes_to(self):
        """`scripts/agent.py` sets PI_CODING_AGENT_DIR and the three guard log paths from its STATE.
        If the two ever disagree, the screen reports a source as absent while the file fills up."""
        import agent
        self.assertEqual(str(Path(agent.STATE).relative_to(agent.ROOT)), observe.PI_STATE)

    def test_the_six_sources_are_the_ones_PA_14_lists(self):
        self.assertEqual([source.source for source in observe.SOURCES],
                         ['suite', 'tasks', 'router', 'loop-guard', 'compaction', 'gateway'])


class CommandTests(Fixture):
    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.stdout', out), patch('sys.stderr', err), patch.object(command, 'ROOT', self.root):
            code = command.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_the_screen_needs_no_network_and_no_journalctl(self):
        code, out, _ = self.call('observe', '--no-gateway')
        self.assertEqual(code, 0)
        self.assertIn('| gateway | unavailable |', out)

    def test_one_source_can_be_asked_for_and_the_others_are_not_read(self):
        code, out, _ = self.call('observe', '--no-gateway', '--source', 'router')
        self.assertEqual(code, 0)
        self.assertIn('| router |', out)
        self.assertNotIn('| suite |', out)

    def test_json_carries_the_same_numbers_the_screen_prints(self):
        code, out, _ = self.call('observe', '--no-gateway', '--source', 'router', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['sources'][0]['lines'], 2)

    def test_days_bounds_the_journal_query_and_not_the_files(self):
        asked = {}

        def journal(unit, since):
            asked['unit'], asked['since'] = unit, since
            return [], 0, [], 0, {}

        since = day_str(2026, 10, 31)
        report = observe.summary(self.root, sources=observe.SOURCES, journal=journal, since=since)
        self.assertEqual(asked, {'unit': observe.GATEWAY_UNIT, 'since': since})
        self.assertEqual(report['since'], since)
        self.assertGreater(next(s for s in report['sources'] if s['source'] == 'router')['lines'], 0)

    def test_days_becomes_a_date_the_journal_understands(self):
        expected = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
        with patch.object(observe, 'read_journal', lambda unit, since: ([], 0, [], 0, {})):
            code, out, _ = self.call('observe', '--days', '7', '--source', 'gateway', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['since'], expected)

    def test_an_unknown_source_is_a_misuse_of_the_command_and_exits_2(self):
        with self.assertRaises(SystemExit) as raised, patch('sys.stderr', io.StringIO()):
            self.call('observe', '--source', 'dreams')
        self.assertEqual(raised.exception.code, 2)


class SuiteDenominatorTests(unittest.TestCase):
    def test_superseded_cases_are_left_out_and_infrastructure_says_nothing_about_passing(self):
        checkout = Checkout(self, manifests=True)
        checkout.suite([('worker-a', 'kept', 0, attempt(passed=True)),
                        ('worker-a', 'kept', 1, attempt(passed=False, infrastructure_error='docker is down')),
                        ('worker-a', 'edited', 0, attempt(passed=False))])
        checkout.task('edited', changed=True)
        source = next(item for item in observe.SOURCES if item.source == 'suite')
        found = observe.read(source, checkout.root)
        self.assertEqual(found['lines'], 2)
        self.assertEqual(found['counts'].get('passed:True'), 1)
        self.assertNotIn('passed:False', found['counts'])


class UnreadableSourceTests(Fixture):
    def only(self, name):
        return next(item for item in observe.SOURCES if item.source == name)

    def test_a_truncated_multibyte_tail_is_read_rather_than_fatal(self):
        path = self.state / 'router-log.jsonl'
        path.write_bytes(path.read_bytes() + b'\xe3\x81')
        self.assertEqual(observe.read(self.only('router'), self.root)['status'], 'ok')

    def test_a_corrupt_database_is_unavailable_and_the_other_sources_still_report(self):
        database = self.root / 'artifacts/suite/state.sqlite'
        for leftover in database.parent.glob('state.sqlite*'):     # the fixture's WAL would answer for the file
            leftover.unlink()
        database.write_bytes(b'not a database file, and long enough to be read as a header ' * 4)
        report = observe.summary(self.root, journal=lambda *a: (None, 0, [], 0, {}))
        statuses = {source['source']: source['status'] for source in report['sources']}
        self.assertEqual(statuses['suite'], 'unavailable')
        self.assertEqual(statuses['router'], 'ok')

    def test_a_source_that_cannot_be_opened_is_unavailable(self):
        with patch.object(observe, '_read_jsonl', side_effect=PermissionError('no')):
            self.assertEqual(observe.read(self.only('router'), self.root)['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
