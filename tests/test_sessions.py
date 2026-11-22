"""T11.1: pi's transcripts counted, and nothing that was said in them printed.

The module reads 26 MB of full conversation — every prompt, every reply, every command line and its
output — to produce a screen of numbers. Two things are tested, and the second is the one that matters.

**The counts are the counts.** Turns, calls, errors, compactions, tokens, seconds; a line that does not
parse is counted and excluded rather than silently dropped; a rate with no denominator is `None` and
not zero.

**No body reaches the screen.** One sentinel is planted in every place a body can live — a user prompt,
a thinking block, an assistant reply, a bash command line, a tool result, a compaction summary, and
the lines of a heredoc — and it must appear in neither the screen nor the JSON. The heredoc case is not
a variation of the others: it is the one the first version of this module got wrong, counting each
line of a `python - <<'PY'` block as a *program* and so reading what the Agent typed instead of what it
ran. The quote-aware splitter and the stop at `<<` are what the `other` bucket being 3% of segments on
the real corpus depends on.

The declared lists are tested too. `PROGRAMS` decides what the screen is able to say, so a list that
drifts from the parser it names, or that contains a word the skipper would have skipped anyway, is a
bug the counts would hide.
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
sys.path.insert(0, str(ROOT / 'scripts'))

from agentiac import command, sessions  # noqa: E402

SENTINEL = 'THE-TRANSCRIPT-BODY-MUST-NEVER-LEAVE-HERE'
CWD = '/srv/work/project'
BASE = datetime.datetime(2026, 11, 1, 9, 0, tzinfo=datetime.timezone.utc)


def stamp(minutes):
    """A stamp `minutes` after the fixture's first record, ISO with an offset like pi writes."""
    return (BASE + datetime.timedelta(minutes=minutes)).isoformat()


def session_filename(index):
    return f'{BASE.strftime("%Y-%m-%d")}T09-0{index}-00000Z_session{index}.jsonl'


def session_record(cwd=CWD, minutes=0):
    return {'type': 'session', 'version': 3, 'id': 's1', 'timestamp': stamp(minutes), 'cwd': cwd}


def user(text, minutes=1):
    return {'type': 'message', 'id': f'u{minutes}', 'parentId': None, 'timestamp': stamp(minutes),
            'message': {'role': 'user', 'timestamp': stamp(minutes),
                        'content': [{'type': 'text', 'text': text}]}}


def call(name, arguments, index=0, minutes=2):
    return {'type': 'toolCall', 'id': f'c{minutes}-{index}', 'name': name, 'arguments': arguments}


def assistant(calls=(), minutes=2, thinking=None, text=None, duration_ms=1000, usage='default'):
    content = []
    if thinking is not None:
        content.append({'type': 'thinking', 'thinking': thinking})
    if text is not None:
        content.append({'type': 'text', 'text': text})
    content.extend(call(*item, index=index, minutes=minutes) for index, item in enumerate(calls))
    return {'type': 'message', 'id': f'a{minutes}', 'parentId': None, 'timestamp': stamp(minutes),
            'message': {'role': 'assistant', 'content': content, 'timestamp': stamp(minutes),
                        'durationMs': duration_ms, 'stopReason': 'toolUse',
                        'usage': {'input': 10, 'output': 5, 'reasoning': 2, 'cacheRead': 7,
                                  'cacheWrite': 1, 'cost': 0.02, 'totalTokens': 25}
                        if usage == 'default' else usage}}


def tool_result(name='bash', minutes=3, error=False, content='fine'):
    return {'type': 'message', 'id': f't{minutes}', 'parentId': None, 'timestamp': stamp(minutes),
            'message': {'role': 'toolResult', 'toolName': name, 'toolCallId': 'c2-0', 'isError': error,
                        'timestamp': stamp(minutes), 'content': [{'type': 'text', 'text': content}]}}


def compaction(minutes=4, tokens_before=200000, summary=''):
    return {'type': 'compaction', 'id': f'k{minutes}', 'parentId': None, 'timestamp': stamp(minutes),
            'tokensBefore': tokens_before, 'summary': summary, 'firstKeptEntryId': 'a2'}


def transcript(*records):
    return '\n'.join(json.dumps(record) for record in records) + '\n'


class Fixture(unittest.TestCase):
    """One session directory, one folder per working directory, exactly the transcripts a test names."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.directory = self.root / sessions.SESSIONS

    def write(self, *transcripts, folder='--srv-work-project--'):
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / folder
        target.mkdir(parents=True, exist_ok=True)
        for index, text in enumerate(transcripts):
            (target / session_filename(index)).write_text(text)
        return self.directory


# --- naming: the only strings the screen can show ----------------------------------------------------------

class NamingTests(unittest.TestCase):
    def test_a_tool_name_is_a_category_and_anything_shaped_like_a_sentence_is_not(self):
        for name in sessions.TOOLS:
            self.assertEqual(sessions.tool_name(name), name)
        for invented in ('notebook-edit', 'x', 'a' * 32, 'sk-abc123', 'bash\n'):
            self.assertEqual(sessions.tool_name(invented), 'unnamed', invented)   # one word is still not a declared tool
        for name in ('', None, 'Bash', 'BASH ', 'has space', 'has/slash', 'has.dot', 'a' * 33,
                     'the prompt was: fix the parser', '9lives', '—dash'):
            self.assertEqual(sessions.tool_name(name), 'unnamed', name)

    def test_the_first_word_of_each_segment_is_the_thing_that_was_run(self):
        self.assertEqual(sessions.programs('cd /x && git status'), ['cd', 'git status'])
        self.assertEqual(sessions.programs('git add -A ; git commit -m "fix the parser"'),
                         ['git add', 'git commit'])
        self.assertEqual(sessions.programs(''), [])
        self.assertEqual(sessions.programs(None), [])

    def test_a_pipe_inside_quotes_is_not_two_commands(self):
        """`grep -E "ok|failed"` is one command. Splitting on the `|` inside the quotes would count
        `failed"` as a program — a fragment of a search pattern wearing a program's name."""
        self.assertEqual(sessions.programs('grep -E "ok|failed" log.txt | head -5'), ['grep', 'head'])

    def test_a_heredoc_is_the_body_of_one_command_and_is_never_read(self):
        """The twenty lines after `python - <<'PY'` are what the Agent typed, not what it ran."""
        self.assertEqual(sessions.programs(f"python3 - <<'PY'\nimport os\nprint('{SENTINEL}')\nPY"),
                         ['python3'])

    def test_a_loop_runs_what_comes_after_the_in_and_a_comment_runs_nothing(self):
        self.assertEqual(sessions.programs('for f in *.jsonl; do wc -l $f; done'), ['wc'])
        self.assertEqual(sessions.programs('# check the route\ngit status'), ['git status'])

    def test_a_wrapper_is_not_the_thing_the_agent_was_doing(self):
        self.assertEqual(sessions.programs('sudo systemctl restart agentiac-gateway'), ['systemctl'])
        self.assertEqual(sessions.programs('timeout 900 python -m unittest discover -s tests'), ['python'])
        self.assertEqual(sessions.programs('PYTHONPATH=src .venv/bin/python -c pass'), ['python'])

    def test_a_program_that_is_not_declared_is_counted_and_not_named(self):
        self.assertEqual(sessions.programs('frobnicate --the-prompt-was-fix-it'), ['other'])
        self.assertEqual(sessions.programs('$py -c pass'), ['other'])

    def test_a_subcommand_is_reported_only_from_the_closed_list_and_never_with_its_argument(self):
        self.assertEqual(sessions.programs('git commit -m "the prompt was: fix the parser"'), ['git commit'])
        self.assertEqual(sessions.programs('git checkout feature/the-prompt-was-fix-it'), ['git checkout'])
        self.assertEqual(sessions.programs('agentiac ops drift worker-a'), ['agentiac ops'])
        self.assertEqual(sessions.programs('agentiac not-a-command /srv/user/secret'), ['agentiac'])
        self.assertEqual(sessions.programs('git'), ['git'])

    def test_only_the_two_programs_whose_subcommand_is_the_work_get_one(self):
        """`docker ps` ran docker, not a `docker ps` subcommand. Consulting the subcommand list for
        every program would invent categories the screen then ranks, and the top ten would be a list
        of things nobody can run."""
        self.assertEqual(sessions.programs('docker ps'), ['docker'])
        self.assertEqual(sessions.programs('make test'), ['make'])
        self.assertEqual(sessions.programs('python install'), ['python'])

    def test_no_command_argument_ever_survives_the_naming(self):
        for line in (f'git commit -m "{SENTINEL}"', f"python3 - <<'PY'\n{SENTINEL}\nPY",
                     f'grep "{SENTINEL}" file', f'echo {SENTINEL}'):
            self.assertNotIn(SENTINEL, ' '.join(sessions.programs(line)), line)


class DeclaredListTests(unittest.TestCase):
    def test_the_agentiac_list_is_the_commands_the_parser_actually_has(self):
        """The screen names subcommands; a list that has drifted from the parser would name ones that
        do not exist and stay silent about ones that do. Same reason T13.2 compares the gateway fields."""
        parser = command.build_parser()
        choices = next(action for action in parser._actions if getattr(action, 'dest', '') == 'command').choices
        self.assertEqual(sorted(sessions.AGENTIAC_SUBCOMMANDS), sorted(choices))

    def test_the_program_list_is_a_set_of_names_and_not_a_list_that_grew_twice(self):
        self.assertEqual(list(sessions.PROGRAMS), sorted(sessions.PROGRAMS))
        self.assertEqual(len(set(sessions.PROGRAMS)), len(sessions.PROGRAMS))
        for name in sessions.PROGRAMS:
            self.assertTrue(sessions.NAME.match(name), name)

    def test_no_wrapper_is_also_a_program_because_then_the_skip_would_be_dead_code(self):
        self.assertEqual(set(sessions.WRAPPERS) & set(sessions.PROGRAMS), set())

    def test_the_subcommand_lists_can_be_matched_at_all(self):
        for name in sessions.GIT_SUBCOMMANDS + sessions.AGENTIAC_SUBCOMMANDS:
            self.assertTrue(sessions.NAME.match(name), name)

    def test_the_session_directory_is_the_one_the_writer_writes(self):
        import agent
        self.assertEqual(sessions.PI_STATE, str(agent.STATE.relative_to(agent.ROOT)))
        self.assertEqual(sessions.SESSIONS, f'{sessions.PI_STATE}/sessions')


# --- counting one transcript -------------------------------------------------------------------------------

class CountingTests(Fixture):
    def count(self, *records):
        self.write(transcript(*records))
        return sessions.read_session(sorted(self.directory.rglob('*.jsonl'))[0])

    def test_every_kind_of_line_contributes_its_own_number(self):
        found = self.count(session_record(), user('fix the parser'),
                           assistant(calls=[('bash', {'command': 'git status'}),
                                            ('read', {'path': '/etc/passwd'})], text='looking'),
                           assistant(calls=[('edit', {'path': 'a.py'})]),
                           tool_result('bash', error=True), tool_result('read', error=False),
                           compaction(tokens_before=221372, summary='the whole conversation'))
        self.assertEqual(found['cwd'], CWD)
        self.assertEqual(found['turns'], 1)
        self.assertEqual(found['assistant'], 2)
        self.assertEqual(found['tool_calls'], 3)
        self.assertEqual(found['tool_errors'], 1)
        self.assertEqual(found['compactions'], 1)
        self.assertEqual(found['compacted_tokens'], 221372)
        self.assertEqual(dict(found['tools']), {'bash': 1, 'read': 1, 'edit': 1})
        self.assertEqual(dict(found['programs']), {'git status': 1})

    def test_the_tokens_are_the_ones_the_adapter_reported_and_a_price_is_not_a_measurement(self):
        found = self.count(session_record(), assistant(usage={'input': 100, 'output': 40, 'cost': 9.99}))
        self.assertEqual(dict(found['tokens']), {'input': 100, 'output': 40})
        self.assertNotIn('cost', json.dumps(found))

    def test_seconds_come_from_the_turn_and_not_from_the_wall(self):
        found = self.count(session_record(), assistant(duration_ms=1500), assistant(duration_ms=2500))
        self.assertAlmostEqual(found['seconds'], 4.0)

    def test_the_span_is_the_first_and_last_stamp_the_file_carries(self):
        found = self.count(session_record(minutes=0), user('a', minutes=5), assistant(minutes=9))
        self.assertEqual(found['started'], stamp(0))
        self.assertEqual(found['ended'], stamp(9))

    def test_a_line_that_does_not_parse_is_counted_and_excluded_rather_than_vanishing(self):
        self.write(transcript(session_record(), user('a')) + 'not json at all\n' + '[1, 2]\n' + '\n')
        found = sessions.read_session(sorted(self.directory.rglob('*.jsonl'))[0])
        self.assertEqual(found['broken'], 2)
        self.assertEqual(found['turns'], 1)

    def test_a_file_that_cannot_be_read_says_so_instead_of_reading_as_an_empty_session(self):
        self.write(transcript(session_record(), user('a')))
        path = sorted(self.directory.rglob('*.jsonl'))[0]
        with patch.object(Path, 'read_text', side_effect=OSError('device gone')):
            found = sessions.read_session(path)
        self.assertTrue(found['unreadable'])
        self.assertEqual(found['turns'], 0)

    def test_a_record_that_is_not_an_object_and_a_message_that_is_not_one_are_both_broken_and_neither_crashes(self):
        found = self.count(session_record(), {'type': 'message', 'message': 'not a dict'},
                           {'type': 'message'}, [1, 2, 3])
        self.assertEqual(found['broken'], 1)
        self.assertEqual(found['assistant'], 0)

    def test_a_tool_call_without_arguments_counts_as_a_call_and_yields_no_program(self):
        found = self.count(session_record(), assistant(calls=[('bash', None)]))
        self.assertEqual(found['tool_calls'], 1)
        self.assertEqual(dict(found['programs']), {})

    def test_a_tool_name_that_is_not_a_name_is_bucketed_where_the_counting_happens_and_not_only_in_the_helper(self):
        """`tool_name` is tested on its own; this is the call site, where a name arriving from a file
        either goes through the check or does not, and the report is the only place that would show it."""
        found = self.count(session_record(), assistant(calls=[(f'the prompt was {SENTINEL}', None)]))
        self.assertEqual(dict(found['tools']), {'unnamed': 1})
        self.assertEqual(found['tool_calls'], 1)


# --- the inventory -------------------------------------------------------------------------------------------

class SummaryTests(Fixture):
    def test_a_directory_that_was_never_written_is_answered_as_absent_and_not_as_a_quiet_week(self):
        report = sessions.summary(self.root)
        self.assertEqual(report['status'], 'absent')
        self.assertIn('not the same as nobody used pi', report['note'])
        self.assertEqual(report['baseline']['sessions'], 0)

    def test_a_directory_holding_nothing_is_empty_and_says_which_of_the_two_it_is(self):
        (self.directory).mkdir(parents=True)
        report = sessions.summary(self.root)
        self.assertEqual(report['status'], 'empty')
        self.assertIn('no transcript', report['note'])

    def test_the_top_ten_is_the_most_frequent_first_and_ties_broken_by_name_so_the_screen_is_reproducible(self):
        self.write(transcript(session_record(),
                              assistant(calls=[('bash', {'command': 'git status'}),
                                               ('bash', {'command': 'git status'}),
                                               ('bash', {'command': 'grep x y'}),
                                               ('bash', {'command': 'cat z'}),
                                               ('read', {'path': 'a'})])))
        report = sessions.summary(self.root)
        self.assertEqual(report['top_programs'], [{'name': 'git status', 'count': 2},
                                                  {'name': 'cat', 'count': 1},    # tied with grep, first by name
                                                  {'name': 'grep', 'count': 1}])
        self.assertEqual(report['top_tools'][0], {'name': 'bash', 'count': 4})

    def test_the_top_list_is_limited_and_the_rest_is_still_counted(self):
        calls = [('bash', {'command': f'{name} x'}) for name in ('ls', 'cat', 'wc', 'du', 'df')]
        self.write(transcript(session_record(), assistant(calls=calls)))
        report = sessions.summary(self.root, top=2)
        self.assertEqual(len(report['top_programs']), 2)
        self.assertEqual(sum(report['programs'].values()), 5)

    def test_days_keeps_a_session_by_the_time_it_started_and_not_by_the_time_it_was_written(self):
        old_stamp = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.timezone.utc).isoformat()
        old = transcript(dict(session_record(), timestamp=old_stamp), user('a'))
        new = transcript(session_record(minutes=0), user('b'))
        self.write(old, new)
        report = sessions.summary(self.root, days=1, now=BASE)
        self.assertEqual([s['turns'] for s in report['sessions']], [1])
        self.assertEqual(report['baseline']['turns'], 1)

    def test_a_session_that_never_asked_a_question_has_no_rate_per_turn_rather_than_a_zero(self):
        self.write(transcript(session_record(), assistant(calls=[('bash', {'command': 'ls'})])))
        baseline = sessions.summary(self.root)['baseline']
        self.assertIsNone(baseline['tool_calls_per_turn'])
        self.assertEqual(baseline['tool_calls'], 1)
        self.assertIn('| tool calls per turn | - |', sessions.markdown(sessions.summary(self.root)))

    def test_the_rates_name_the_thing_they_are_divided_by(self):
        self.write(transcript(session_record(), user('a'),
                              assistant(calls=[('bash', {'command': 'ls'})], duration_ms=2000),
                              assistant(calls=[('bash', {'command': 'ls'})]),
                              tool_result(error=True)))
        baseline = sessions.summary(self.root)['baseline']
        self.assertEqual(baseline['tool_calls_per_turn'], 2.0)
        self.assertEqual(baseline['assistant_turns_per_turn'], 2.0)
        self.assertEqual(baseline['tool_errors_per_call'], 0.5)
        self.assertEqual(baseline['seconds_per_assistant_turn'], 1.5)   # 2000 ms and the fixture's 1000 ms, over two turns
        self.assertEqual(baseline['compactions_per_session'], 0.0)

    def test_an_unreadable_transcript_is_reported_and_does_not_stop_the_others(self):
        self.write(transcript(session_record(), user('a')), transcript(session_record(), user('b')))
        first = sorted(self.directory.rglob('*.jsonl'))[0]
        with patch.object(sessions, 'read_session',
                          lambda path: dict(sessions.blank(path), unreadable=True) if path == first
                          else sessions.blank(path) | {'turns': 1}):
            report = sessions.summary(self.root)
        self.assertEqual(report['unreadable'], 1)
        self.assertEqual(report['baseline']['turns'], 1)

    def test_the_totals_are_the_sum_of_the_sessions_and_survive_a_second_call(self):
        self.write(transcript(session_record(), user('a'), assistant()),
                   transcript(session_record(), user('b'), assistant()))
        first = sessions.summary(self.root)
        self.assertEqual(first['baseline']['sessions'], 2)
        self.assertEqual(first['baseline']['turns'], 2)
        self.assertEqual(first['bytes'], sum(s['bytes'] for s in first['sessions']))
        self.assertEqual(sessions.summary(self.root), first)      # the same corpus, the same numbers


# --- the rule ------------------------------------------------------------------------------------------------

class NoBodyTests(Fixture):
    """One sentinel in every place a body can live, and neither output may contain it."""

    def setUp(self):
        super().setUp()
        self.write(transcript(
            session_record(),
            user(f'please {SENTINEL} now'),
            assistant(calls=[('bash', {'command': f'echo {SENTINEL}'})],
                      thinking=f'thinking about {SENTINEL}', text=f'I will {SENTINEL}'),
            assistant(calls=[('bash', {'command': f"python3 - <<'PY'\nprint('{SENTINEL}')\nPY"})]),
            tool_result(content=f'the output said {SENTINEL}'),
            compaction(summary=f'the conversation was about {SENTINEL}')))

    def test_the_sentinel_really_is_in_the_files_the_module_reads(self):
        """Without this the suite could pass by the fixture having written nothing."""
        self.assertIn(SENTINEL, '\n'.join(path.read_text() for path in self.directory.rglob('*.jsonl')))

    def test_no_prompt_reply_thinking_block_command_line_or_tool_output_reaches_the_screen(self):
        report = sessions.summary(self.root)
        self.assertNotIn(SENTINEL, sessions.markdown(report))
        self.assertNotIn(SENTINEL, json.dumps(report))

    def test_the_heredoc_is_counted_as_the_command_it_is_and_its_lines_are_never_read(self):
        report = sessions.summary(self.root)
        self.assertEqual(report['programs'].get('python3'), 1)
        self.assertIsNone(report['programs'].get('print'), 'a line of the heredoc was read as a program')
        self.assertIsNone(report['programs'].get(f'print(\'{SENTINEL}\')'.lower()))

    def test_the_screen_says_it_kept_nothing(self):
        self.assertIn('No prompt, reply, thinking block, command line or tool output',
                      sessions.markdown(sessions.summary(self.root)))

    def test_the_screen_says_which_window_of_time_the_numbers_cover(self):
        """T11.5 compares before and after a Skill, and a baseline without its span cannot be compared
        with anything: the same numbers over a week and over a year are not the same measurement."""
        screen = sessions.markdown(sessions.summary(self.root))
        self.assertIn(f'Span {stamp(0)} → {stamp(4)}', screen)

    def test_the_working_directory_is_shown_because_it_is_metadata_and_not_a_body(self):
        """Which repository the routine work happens in is half the answer; the choice is pinned here so
        it stays a decision and does not become an accident someone "fixes" by deleting the column."""
        self.assertIn(CWD, sessions.markdown(sessions.summary(self.root)))


# --- the command ---------------------------------------------------------------------------------------------

class CommandTests(unittest.TestCase):
    """The command over a real checkout: `build_parser` reads the declared files, so the fixture is one."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / 'site'
        shutil.copytree(ROOT / 'examples/site', self.root)
        self.directory = self.root / sessions.SESSIONS

    def write(self, *transcripts, folder='--srv-work-project--'):
        target = self.directory / folder
        target.mkdir(parents=True, exist_ok=True)
        for index, text in enumerate(transcripts):
            (target / session_filename(index)).write_text(text)

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.stdout', out), patch('sys.stderr', err), patch.object(command, 'ROOT', self.root):
            code = command.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_the_screen_needs_no_network_and_no_journalctl(self):
        self.write(transcript(session_record(), user('a'), assistant(calls=[('bash', {'command': 'ls'})])))
        code, out, _ = self.call('sessions')
        self.assertEqual(code, 0)
        self.assertIn('## Baseline', out)
        self.assertIn('| ls | 1 |', out)

    def test_json_carries_the_same_numbers_the_screen_prints(self):
        self.write(transcript(session_record(), user('a'), user('b')))
        code, out, _ = self.call('sessions', '--json')
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report['baseline']['turns'], 2)
        self.assertIn('user turns | 2', sessions.markdown(report))

    def test_a_window_of_zero_days_or_zero_rows_is_a_misuse_and_not_an_empty_report(self):
        self.write(transcript(session_record(), user('a')))
        for argv in (('--days', '0'), ('--days', '-3'), ('--top', '0')):
            with self.subTest(argv=argv):
                code, out, err = self.call('sessions', *argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertTrue(err.strip())

    def test_a_missing_session_directory_is_a_fact_and_exits_zero(self):
        code, out, _ = self.call('sessions')
        self.assertEqual(code, 0)
        self.assertIn('does not exist', out)

    def test_the_default_top_is_the_ten_the_completion_condition_asks_for(self):
        self.write(transcript(session_record(),
                              assistant(calls=[('bash', {'command': f'{n} x'}) for n in
                                               ('ls', 'cat', 'wc', 'du', 'df', 'pwd', 'env', 'true', 'test', 'sort', 'tr', 'tee')])))
        code, out, _ = self.call('sessions')
        self.assertEqual(len(json.loads(self.call('sessions', '--json')[1])['top_programs']), 10)


class ShellFormTests(unittest.TestCase):
    """The inventory counts what ran, in the forms the Agent actually writes."""

    def test_wrappers_with_a_duration_or_an_option_value_reach_the_program(self):
        self.assertEqual(sessions.programs('timeout 30s git status'), ['git status'])
        self.assertEqual(sessions.programs('sudo -u root git status'), ['git status'])
        self.assertEqual(sessions.programs('nice -n 5 make check'), ['make'])

    def test_a_background_ampersand_separates_and_a_redirect_does_not(self):
        self.assertEqual(sessions.programs('sleep 1 & git status'), ['sleep', 'git status'])
        self.assertEqual(sessions.programs('make check 2>&1 | tail -5'), ['make', 'tail'])
        self.assertEqual(sessions.programs('make check &> log; ls'), ['make', 'ls'])

    def test_a_comment_does_not_open_a_quote(self):
        self.assertEqual(sessions.programs("# don't stop here\ngit status"), ['git status'])

    def test_an_escaped_quote_does_not_close_a_double_quote(self):
        self.assertEqual(sessions.programs('echo "a \\" | b" && git status'), ['echo', 'git status'])

    def test_a_here_string_is_not_a_heredoc(self):
        self.assertEqual(sessions.programs('grep x <<< "$y" && git push'), ['grep', 'git push'])

    def test_what_follows_a_heredoc_is_counted_and_its_body_never_is(self):
        command = "cat <<EOF > note\nrm -rf everything\ngit reset --hard\nEOF\ngit commit -m x"
        self.assertEqual(sessions.programs(command), ['cat', 'git commit'])

    def test_a_heredoc_with_no_end_keeps_its_body_out(self):
        self.assertEqual(sessions.programs("python - <<'PY'\nimport os\ndef f(): pass"), ['python'])


class WindowCountTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.folder = self.root / sessions.SESSIONS / 'repo'
        self.folder.mkdir(parents=True)

    def test_a_file_nobody_could_read_is_not_a_session_of_zeros(self):
        (self.folder / 'a.jsonl').write_text('{"type": "session"}\n')
        with patch.object(sessions, 'read_session', return_value={'unreadable': True, 'broken': 0}):
            report = sessions.summary(self.root)
        self.assertEqual(report['unreadable'], 1)
        self.assertEqual(report['baseline']['sessions'], 0)

    def test_a_session_with_no_time_is_counted_when_a_window_cannot_place_it(self):
        (self.folder / 'a.jsonl').write_text('{"type": "session"}\n')
        report = sessions.summary(self.root, days=1)
        self.assertEqual(report['unstamped'], 1)
        self.assertEqual(report['baseline']['sessions'], 0)


if __name__ == '__main__':
    unittest.main()
