"""T13.2: the observation side of PA §14, read in one call, with no message body in any of it.

PA §14's loop starts at 観測 — `log-task`, the Suite, the gateway metadata, the router log and the
loop-guard log — and hands off to `eval report --failures` before anything is written down as a
proposal. This module reads those five together, because the question that starts an Improvement
Proposal is always cross-source: *the task failed, did the router send it somewhere different from
usual, did the guard intervene, did the gateway see it at all.*

Two rules, both from the completion condition.

**No body.** Each source names the keys it may surface, and the counts are built from those and
nothing else. The router log carries `chars` and not the prompt; the gateway line is built key by key
from `LOG_FIELDS` because `kwargs` carries `messages` and the response; the compaction guard stores
token counts. A source that gained a `prompt` key would contribute nothing to the screen, and
`tests/test_observe.py` puts a sentinel in exactly such a key of every source and fails if it appears
anywhere in the output.

**Retention is stated, not assumed.** Nothing prunes these files today, so the screen prints the
oldest line and the size of each and says what keeps it, rather than quoting a retention period
nobody configured. `absent` (never written) and `empty` (written, no lines) are different answers
from `unavailable` (could not be read): a zero where there is no file claims nobody asked, which is
the same kind of lie as a gate read as passed.
"""
from __future__ import annotations

import collections
import datetime
import json
from pathlib import Path
import sqlite3
import subprocess

from agentiac import declared, failures, paths

ROOT = paths.root()

# pi's own state directory, where its extensions append their logs. The writer is `scripts/agent.py`
# (`STATE`), which sets the three `AGENTIAC_*_LOG` variables from it; a test compares the two.
PI_STATE = 'artifacts/agents/pi-daily'

SUITE = 'artifacts/suite/state.sqlite'
GATEWAY_LOGGER = 'agentiac_metadata'

# What the gateway line may carry (IP-06, T4.2). Duplicated from `gateway/gateway_callbacks.py`
# because that module imports litellm; tests/test_observe.py compares the two texts.
GATEWAY_FIELDS = ('event', 'status', 'alias', 'node', 'model', 'stream',
                  'latency_s', 'ttft_s', 'input_tokens', 'output_tokens')

JOURNAL_TIMEOUT_S = 20


class Source:
    """One place an observation lives, and the only keys of it this command is allowed to show."""

    def __init__(self, source, kind, ref, what, surface, retention):
        self.source, self.kind, self.ref = source, kind, ref
        self.what, self.surface, self.retention = what, tuple(surface), retention


def gateway_unit():
    """The gateway's systemd unit: `<resource_prefix>-gateway` (D6.8)."""
    return declared.resource('gateway')


def all_sources():
    """Every source the screen reads, in screen order."""
    return (
        Source('suite', 'sqlite', SUITE, 'the task suite: what passed, under which keys',
               ('passed', 'wall_s', 'tool_errors', 'timed_out'),
               'kept: it is the primary record, and the Evidence DB is derived from it'),
        Source('tasks', 'jsonl', 'artifacts/tasks/tasks.jsonl', 'real usage, one line per task (T8.7)',
               ('time', 'repository', 'type', 'outcome', 'minutes', 'failure', 'agent', 'target', 'target_source'),
               'kept: nothing prunes it, and it is the evidence Phase 3 asks for'),
        Source('router', 'jsonl', f'{PI_STATE}/router-log.jsonl', 'the pi router: which model each turn went to',
               ('time', 'repo', 'tier', 'model', 'why', 'score'),
               'appended by the router extension; nothing prunes it'),
        Source('loop-guard', 'jsonl', f'{PI_STATE}/loop-guard-log.jsonl', 'the loop guard: when it nudged or stopped',
               ('time', 'verdict', 'streak'),
               'appended by the loop guard extension; nothing prunes it'),
        Source('compaction', 'jsonl', f'{PI_STATE}/compaction-guard-log.jsonl', 'the compaction guard: when a session was summarised',
               ('time', 'event', 'reason', 'split_turn', 'tokens_before'),
               'appended by the compaction guard extension; nothing prunes it'),
        Source('gateway', 'journal', f'journalctl -u {gateway_unit()}', 'the gateway: who served each request, and how long',
               GATEWAY_FIELDS, "journald's own retention on control; nothing here prunes it"),
    )


def __getattr__(name):
    """`observe.SOURCES` and `observe.GATEWAY_UNIT`, resolved on first use rather than at import (PEP 562)."""
    if name == 'SOURCES':
        return all_sources()
    if name == 'GATEWAY_UNIT':
        return gateway_unit()
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')

BY = {'suite': ('passed', 'timed_out'), 'tasks': ('outcome', 'failure', 'type', 'agent'),
      'router': ('tier', 'model', 'why'), 'loop-guard': ('verdict',), 'compaction': ('event', 'reason'),
      'gateway': ('status', 'node', 'alias')}


# --- reading one source ------------------------------------------------------------------------------

def _moment(text):
    try:
        return datetime.datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def _span(times):
    """The oldest and newest stamp, as strings. `None` when no line carried a readable time."""
    stamps = sorted(t.isoformat(timespec='seconds') for t in times if t)
    return (stamps[0], stamps[-1]) if stamps else (None, None)


def _split(record, keys):
    """The declared keys and their values, and the *names* of everything else.

    The names are kept and shown, the values are not: a reader who sees that the router log carries a
    `prompt` key learns something useful (there is more in there than this screen shows), and a reader
    who sees its contents is reading a trace. This is the function the no-body rule lives in, so the
    screen prints what it dropped — otherwise the rule would be a list nothing consults.
    """
    return ({key: record[key] for key in keys if key in record},
            [key for key in record if key not in keys])


def _counts(records, by):
    """Counts of the declared values. A record with none of them contributes nothing, which is the point."""
    counts = collections.Counter()
    for record in records:
        for field in by:
            if field in record and record[field] is not None:
                counts[f'{field}:{record[field]}'] += 1
    return dict(counts)


def _read_jsonl(path, surface):
    """Lines as dicts, the parse failures, and the keys that were not declared."""
    if not path.exists():
        return None, 0, [], 0, {}
    records, broken, times, dropped = [], 0, [], collections.Counter()
    for text in path.read_text(errors='replace').splitlines():
        if not text.strip():
            continue
        try:
            record = json.loads(text)
        except ValueError:
            broken += 1
            continue
        if not isinstance(record, dict):
            broken += 1
            continue
        surfaced, hidden = _split(record, surface)
        records.append(surfaced)
        dropped.update(hidden)
        times.append(_moment(record.get('time')))
    return records, len(records) + broken, times, broken, dict(dropped)


def _read_suite(path, surface, root=ROOT):
    """The latest attempt of every case that still stands: the suite report's denominator.

    A case of an edited manifest is left out, as `eval report --failures` leaves it out. An
    infrastructure failure stays a line but says nothing about passing: nothing was measured.
    """
    if not path.exists():
        return None, 0, [], 0, {}
    current = failures.manifests(root)
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('''SELECT c.spec spec, a.result result, a.finished finished, a.started started
                             FROM cases c JOIN attempts a
                             ON a.case_id = c.id AND a.number = c.attempts
                             WHERE a.status IN ('complete', 'failed')''').fetchall()
    records, broken, times, dropped = [], 0, [], collections.Counter()
    for row in rows:
        try:
            spec, result = json.loads(row['spec']), json.loads(row['result'] or '{}')
        except ValueError:
            broken += 1
            continue
        if not isinstance(result, dict):
            broken += 1
            continue
        if current.get(spec.get('task')) != spec.get('task_sha256'):
            continue
        if 'infrastructure_error' in result:
            result = {key: value for key, value in result.items() if key != 'passed'}
        surfaced, hidden = _split(result, surface)
        records.append(surfaced)
        dropped.update(hidden)
        times.append(_epoch(row['finished'] or row['started']))
    return records, len(records) + broken, times, broken, dict(dropped)


def _epoch(seconds):
    """The suite stores an epoch float; the logs store an ISO stamp. Both become a datetime."""
    try:
        return datetime.datetime.fromtimestamp(float(seconds), datetime.timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _journal_moment(entry):
    """journald stamps microseconds since the epoch; the line itself carries no time."""
    try:
        return datetime.datetime.fromtimestamp(int(entry['__REALTIME_TIMESTAMP']) / 1e6, datetime.timezone.utc)
    except (KeyError, TypeError, ValueError, OSError):
        return None


def read_journal(unit, since=None, timeout_s=JOURNAL_TIMEOUT_S):
    """The gateway's own metadata lines. `None` when journalctl cannot answer, which is not the same as no traffic."""
    argv = ['journalctl', '-u', unit, '--no-pager', '-o', 'json']
    if since:
        argv += ['--since', since]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
        return None, 0, [], 0, {}
    if done.returncode != 0:
        return None, 0, [], 0, {}
    records, broken, times, dropped = [], 0, [], collections.Counter()
    for text in done.stdout.splitlines():
        try:
            entry = json.loads(text)
        except ValueError:
            broken += 1
            continue
        try:
            message = json.loads(entry.get('MESSAGE', ''))
        except (ValueError, TypeError):
            continue                                     # the unit logs plenty that is not one of our lines
        if not isinstance(message, dict) or message.get('event') != 'inference':
            continue
        surfaced, hidden = _split(message, GATEWAY_FIELDS)
        records.append(surfaced)
        dropped.update(hidden)
        times.append(_journal_moment(entry))
    return records, len(records) + broken, times, broken, dict(dropped)


def read(source, root=ROOT, journal=read_journal, since=None):
    """One source: what it is, whether it could be read, how much of it there is, and the declared counts."""
    found = {'source': source.source, 'kind': source.kind, 'ref': source.ref, 'what': source.what,
             'retention': source.retention, 'surface': list(source.surface),
             'lines': 0, 'broken': 0, 'oldest': None, 'newest': None, 'bytes': None, 'counts': {}, 'dropped': {}}
    if source.kind == 'journal':
        records, lines, times, broken, dropped = journal(gateway_unit(), since)
        if records is None:
            found['status'] = 'unavailable'
            found['note'] = 'journalctl did not answer; that is not the same as no traffic'
            return found
    else:
        path = Path(root) / source.ref
        try:
            found['bytes'] = path.stat().st_size if path.exists() else None
            if source.kind == 'sqlite':
                records, lines, times, broken, dropped = _read_suite(path, source.surface, root)
            else:
                records, lines, times, broken, dropped = _read_jsonl(path, source.surface)
        except (OSError, ValueError, sqlite3.Error) as error:   # one bad source must not cost the other five
            found['status'] = 'unavailable'
            found['note'] = f'{type(error).__name__} reading it; that is not the same as nothing there'
            return found
        if records is None:
            found['status'] = 'absent'
            found['note'] = 'never written; a zero here would claim nobody asked'
            return found
    found['status'] = 'ok' if lines else 'empty'
    found['lines'], found['broken'] = lines, broken
    found['oldest'], found['newest'] = _span(times)
    found['counts'] = _counts(records, BY.get(source.source, ()))
    found['dropped'] = dropped
    if broken:
        found['note'] = f'{broken} line(s) did not parse and are counted but not counted in'
    return found


# --- the screen ----------------------------------------------------------------------------------------

def summary(root=ROOT, sources=None, journal=read_journal, since=None):
    sources = all_sources() if sources is None else sources
    return {'schema_version': 1, 'generated': datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec='seconds'), 'since': since,
            'sources': [read(source, root, journal, since) for source in sources]}


def markdown(report):
    """One table. Every row says whether it could be read at all, and what keeps the file."""
    lines = ['| source | status | lines | broken | oldest | newest | size | counts | keys not shown |',
             '|---|---|---:|---:|---|---|---:|---|---|']
    for source in report['sources']:
        counts = ', '.join(f'{name} × {count}' for name, count in
                           sorted(source['counts'].items(), key=lambda item: (-item[1], item[0]))[:6]) or '-'
        hidden = ', '.join(f'{name} × {count}' for name, count in sorted(source['dropped'].items())) or '-'
        size = '-' if source['bytes'] is None else f'{source["bytes"] / 1024:.0f} KiB'
        lines.append(f'| {source["source"]} | {source["status"]} | {source["lines"]} | {source["broken"]} | '
                     f'{source["oldest"] or "-"} | {source["newest"] or "-"} | {size} | {counts} | {hidden} |')
    lines += ['', 'The last column names the keys each source carries that this screen refuses to read — '
                  'names only, never values (the router log stores `chars` and not the prompt, the gateway '
                  'line is built from `LOG_FIELDS`, the compaction guard stores token counts).', '']
    for source in report['sources']:
        note = f' — {source["note"]}' if source.get('note') else ''
        lines.append(f'- **{source["source"]}** (`{source["ref"]}`): {source["retention"]}{note}')
    return '\n'.join(lines)
