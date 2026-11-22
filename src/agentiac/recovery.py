"""The record of the recovery-only failure automation: what it last did, and what stands down now.

The scope is **recovery-only** — re-claim the host the route already points at, never move the route. `scripts/recover.py`
is the actor and writes this record; `agentiac doctor` and `agentiac ops recover status` read it. The
record lives in the package rather than only in the script for the same reason `audit` and `backup` do: a
session must be able to see the automation's state without running it, and `doctor` reads only.

What a reader needs is one answer — *is a person required now?* — so the record names the gate that stopped
it, or what the reclaim cost, and nothing more.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from agentiac.errors import command as cli_name

STATE = 'artifacts/recovery/state.json'
PAUSE = 'artifacts/recovery/pause.json'
LOG = 'artifacts/recovery/log.jsonl'
SCHEMA_VERSION = 1

# A reclaim installs a front, restarts the control-side forward and loads a model: it is a repair, not a retry.
COOLDOWN_S = 20 * 60
# After this many failed reclaims in a row the host needs a person, not a fourth attempt.
MAX_FAILURES = 3
DECISIONS = ('idle', 'stand_down', 'unreachable', 'reclaimed', 'reclaim_failed', 'paused', 'resumed', 'reset')


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def stamp(at=None):
    return (at or now()).isoformat(timespec='seconds')


def _parse(at):
    """The timestamp of a record, or None when it carries none or an unreadable one."""
    try:
        return datetime.datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return None


def _read(root, name):
    try:
        saved = json.loads((Path(root) / name).read_text())
    except (OSError, ValueError):
        return None
    return saved if isinstance(saved, dict) else None


def load(root):
    """The last run, or None when none has been written here yet."""
    return _read(root, STATE)


def save(root, record):
    path = Path(root) / STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    record.setdefault('schema_version', SCHEMA_VERSION)
    record['at'] = stamp()
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
    os.chmod(path, 0o600)
    path = Path(root) / LOG
    with path.open('a') as handle:
        handle.write(json.dumps(record, sort_keys=True) + '\n')
    return record


def pause(root, minutes, reason='', at=None):
    """Stand down for MINUTES. `ops stop`, a release or hands-on repair are exactly the deliberate acts a
    timer must not undo, and a stand-down has to be written down rather than remembered."""
    path = Path(root) / PAUSE
    path.parent.mkdir(parents=True, exist_ok=True)
    at = at or now()
    record = {'schema_version': SCHEMA_VERSION, 'set_at': stamp(at), 'minutes': float(minutes),
              'until': stamp(at + datetime.timedelta(seconds=float(minutes) * 60)), 'reason': reason}
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
    os.chmod(path, 0o600)
    return record


def paused(root, at=None):
    """The pause that is in force now, or None."""
    record = _read(root, PAUSE)
    if record is None:
        return None
    until = _parse(record.get('until'))
    return None if until is None or until <= (at or now()) else record


def resume(root):
    try:
        (Path(root) / PAUSE).unlink()
    except FileNotFoundError:
        pass


def failures(record):
    return int((record or {}).get('consecutive_failures') or 0)


def breaker_open(record):
    return failures(record) >= MAX_FAILURES


def cooldown_left(record, at=None):
    """Seconds before the next reclaim may be attempted; 0 when nothing has been attempted yet."""
    last = _parse((record or {}).get('last_action_at'))
    if last is None:
        return 0.0
    return max(0.0, COOLDOWN_S - ((at or now()) - last).total_seconds())


def age_hours(record, at=None):
    at_record = _parse((record or {}).get('at'))
    return None if at_record is None else ((at or now()) - at_record).total_seconds() / 3600


def line(record, at=None):
    """One line: what it last decided, in the words the record itself carries.

    An open breaker is said first, because it is the fact that changes what a reader does next: the automation
    is not trying again, whatever the last attempt's reason was.
    """
    body = _line(record)
    return (f'it stands down after {failures(record)} failed reclaims in a row: {body}'
            if breaker_open(record) else body)


def _line(record):
    decision = (record or {}).get('decision')
    node = (record or {}).get('node') or 'the route host'
    action = (record or {}).get('action') or {}
    if decision == 'reclaimed':
        return f"re-claimed {node} in {action.get('seconds', '?')} s; the route answers"
    if decision == 'reclaim_failed':
        return f"the reclaim on {node} failed: {action.get('detail') or 'the route still does not answer'}"
    if decision == 'unreachable':
        return f'{node} did not answer, so nothing was attempted'
    if decision == 'idle':
        return f'nothing to do: {(record or {}).get("reason") or "the route answers"}'
    if decision == 'paused':
        return f'paused: {(record or {}).get("reason") or "no reason given"}'
    if decision == 'reset':
        return 'the failure counter was reset by a person'
    return f'stands down ({(record or {}).get("gate") or "no gate named"}): {(record or {}).get("reason") or ""}'.rstrip()


def next_step(record):
    """What to run next, by the decision the record carries."""
    decision = (record or {}).get('decision')
    node = (record or {}).get('node') or 'HOST'
    if breaker_open(record):
        return f'a person: {cli_name()} status {node}, {cli_name()} reclaim, then {cli_name()} ops recover reset'
    if decision == 'reclaim_failed':
        return f'{cli_name()} ops recover status, {cli_name()} ops preflight {node}, then {cli_name()} reclaim'
    if decision == 'unreachable':
        return f'{cli_name()} ops preflight {node} once the host answers'
    if decision == 'stand_down':
        return f'{cli_name()} ops recover status'
    return None
