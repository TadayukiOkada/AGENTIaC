"""T8.3: `agentiac doctor`, the whole of a session start in one read (PA §10).

T11.1 found that sessions open by rebuilding the same picture by hand: `git status`, `git log`, the handoff and
`docs/progress.md`. This is that picture as one command, so an Agent reads a short structured result and
keeps its judgement for what the result means.

**It reads and nothing else.** No check may load a model: the front is asked only `GET /ondemand/status`, which
is passive, the header that asks for a load is never sent, and `tests/test_doctor.py` holds it to that.

**One failed check costs one line.** Each source is read on its own, and one that fails is `unknown` with the
reason, because a screen that disappears when a host misbehaves is no use on the day it is needed.

**`unknown` is not `problem`.** A borrowed machine being off is not a fault, and a check nobody could run is
not a pass. Exit 0 means no problem and nothing undetermined, 1 a problem, 3 undetermined and no problem.
"""
from __future__ import annotations

import concurrent.futures
import datetime
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

from agentiac import audit, backup, declared, hygiene, paths, recovery, services
from agentiac.errors import command as cli_name

ROOT = paths.root()

STATES = ('problem', 'unknown', 'note', 'ok')      # the order a reader wants them in
FROM_DRIFT = {'ok': 'ok', 'note': 'note', 'drift': 'problem'}

# What to run next, by what the drift line is about. `drift` says what is wrong; this says where to look.
NEXT = (('gateway', f'{cli_name()} ops deploy --check'), ('chatgpt', f'{cli_name()} ops deploy --check'),
        ('deploy', f'{cli_name()} ops deploy --check'), ('route', f'{cli_name()} reclaim'),
        ('front identity', f'{cli_name()} claim the node again'), ('tunnel', f'{cli_name()} release HOST'),
        ('failed units', 'systemctl status the unit named'), ('storage', f'{cli_name()} ops storage'),
        ('eval image', f'{cli_name()} ops eval-image'), ('suite image', f'{cli_name()} ops eval-image'),
        ('checkout', 'git status'), ('agentiac-', f'{cli_name()} ops deploy --check'))


def check(identifier, status, evidence, following=None):
    return {'id': identifier, 'status': status, 'evidence': evidence, 'next': following}


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def _next(subject):
    return next((step for prefix, step in NEXT if subject.startswith(prefix)), None)


def from_drift(line):
    """One `ops drift` line as a check. A host nobody could reach was not looked at, so it is not `note`."""
    state, subject, detail = line
    status = 'unknown' if 'not reachable' in detail else FROM_DRIFT[state]
    return check(f'drift.{_slug(subject)}', status, detail, None if status == 'ok' else _next(subject))


def drift_checks(drift, hosts):
    """Every section on its own: the first one to raise must not take the others with it."""
    found = []
    for index, section in enumerate(drift.sections(hosts)):
        try:
            found += [from_drift(line) for line in section]
        except Exception as error:      # noqa: BLE001 - the screen must outlive any one source
            found.append(check(f'drift.section-{index}', 'unknown', f'{type(error).__name__}: {error}'))
    return found


def preflight_checks(preflight, hosts):
    workers = {name: host for name, host in hosts.items() if host.get('ansible_connection') != 'local'}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(workers) or 1) as pool:
        records = list(pool.map(lambda item: preflight.check_host(*item), workers.items()))
    found = []
    for record in records:
        name = record['host']
        if 'unreachable' in record:
            found.append(check(f'preflight.{name}', 'unknown', f'not reachable: {record["unreachable"]}'))
        elif record['ok']:
            found.append(check(f'preflight.{name}', 'ok', f'{record["free_gib"]} GiB free, every check passed'))
        else:
            failed = ', '.join(record['failed']) or f'{record["free_gib"]} GiB free is below the minimum'
            found.append(check(f'preflight.{name}', 'problem', failed, f'{cli_name()} ops preflight {name}'))
    return found


def _git(root, *argv):
    return subprocess.run(['git', '-C', str(root), *argv], capture_output=True, text=True, timeout=20).stdout.strip()


def git_check(root):
    """The checkout against what this machine last heard from origin. It never fetches: it only reads."""
    branch = _git(root, 'rev-parse', '--abbrev-ref', 'HEAD')
    dirty = len(_git(root, 'status', '--porcelain').splitlines())
    counts = _git(root, 'rev-list', '--left-right', '--count', 'origin/main...HEAD').split()
    behind, ahead = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (None, None)
    evidence = f'{branch}, {dirty} uncommitted paths, {ahead} ahead and {behind} behind origin/main'
    clean = branch == 'main' and not dirty and not ahead and not behind
    return check('git', 'ok' if clean else 'note', evidence, None if clean else 'git status')


def hygiene_check(root, heads=None):
    """Merged agent/ branches and worktrees still on this machine. A note, so it is seen at every session start."""
    report = hygiene.survey(root, hygiene.merged_heads(root) if heads is None else heads, here=root)
    if not report['remove']:
        return check('hygiene', 'ok', f"no merged agent/ branch or worktree left ({len(report['keep'])} in progress)")
    trees = sum(1 for item in report['remove'] if item['worktree'])
    return check('hygiene', 'note', f"{len(report['remove'])} merged agent/ branches ({trees} with a worktree) can go",
                 f'{cli_name()} worktrees prune')


def dashboard_probes_check(root, hosts=None, unit_state=None):
    """The dashboard probes user unit and its --target list against the declared inventory nodes.

    A node rename (former_names) does not resolve in probe.endpoint_for, so a probes unit left on the old
    name fails at the next start. Reads private/dashboard-deployment/current.json and the installed unit file
    when present; starts no unit and runs no probe.
    """
    root = Path(root)
    record_path = root / 'private/dashboard-deployment/current.json'
    if not record_path.exists():
        return check('dashboard.probes', 'ok', 'no dashboard deployment recorded')
    record = json.loads(record_path.read_text())
    # The plan names both dashboard units; the record is read first so a root without site settings still works.
    unit = (next((name for name in (record.get('desired') or record.get('after') or {})
                  if name.endswith('-dashboard-probes.service')), None)
            or declared.resource('dashboard-probes.service', root))
    unit_dir = record.get('unit_dir')
    installed = (Path(unit_dir) / unit) if unit_dir else None
    desired = (record.get('desired') or {}).get(unit) or {}
    after = (record.get('after') or {}).get(unit) or {}
    unit_text = (installed.read_text() if installed and installed.is_file()
                 else after.get('text') or desired.get('text') or '')
    targets = list(dict.fromkeys([*re.findall(r'--target\s+(\S+)', unit_text),
                                  *((record.get('config') or {}).get('targets') or ())]))
    # A stop, or a plan without probes (a rollback can restore one), leaves nothing that should be running.
    wanted = desired.get('active', bool(targets or unit_text))
    if record.get('action') == 'stop' or not wanted:
        return check('dashboard.probes', 'ok', 'dashboard probes are not enabled')
    # The same reader probe.endpoint_for resolves a target with, so a target ok here is one the worker can start on.
    nodes = set(hosts) if hosts is not None else set(services.inventory.nodes(root))
    retired = [target for target in targets if target not in nodes]
    fix = f'{cli_name()} dashboard plan with the current targets, then {cli_name()} dashboard apply --approve-plan HASH'
    if retired:
        return check('dashboard.probes', 'problem',
                     f"{unit} names undeclared probe targets: {', '.join(retired)}", fix)
    state, source = unit_state, ''
    if state is None:
        # Asked even when the unit file is gone: a removed file the record still wants running is inactive.
        try:
            result = subprocess.run(['systemctl', '--user', 'is-active', unit],
                                    capture_output=True, text=True, timeout=10)
            candidate = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            candidate = ''
        if candidate in ('active', 'inactive', 'failed', 'deactivating', 'activating'):
            state = candidate
    if state is None:  # no user manager here (another account, a container): only the record can answer
        state = 'active' if after.get('active', desired.get('active', True)) else 'inactive'
        source = ' (as recorded; the user manager did not answer)'
    if state != 'active':
        return check('dashboard.probes', 'problem', f'{unit} is {state}{source}', f'systemctl --user status {unit}')
    return check('dashboard.probes', 'ok', f"{unit}: {', '.join(targets)}{source}")


def lease_check(root, now=None):
    """Suite cases whose runner is gone: `running`, with a lease that has passed."""
    path = Path(root) / 'artifacts/suite/state.sqlite'
    if not path.exists():
        return check('suite.leases', 'ok', 'no suite state here')
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        count = db.execute("SELECT COUNT(*) FROM cases WHERE status = 'running' AND lease_until < ?",
                           (now or time.time(),)).fetchone()[0]
    if count:
        return check('suite.leases', 'note', f'{count} cases hold an expired lease',
                     f'resume with the options the run started with: {cli_name()} eval suite run')
    return check('suite.leases', 'ok', 'no expired lease')


def backup_check(root, now=None):
    """Backup status (set B): age of the last backup and restore verification."""
    record = backup.load(Path(root))
    if record is None:
        return check('backup', 'note', 'no backup has run yet', f'{cli_name()} ops backup run')
    days = backup.age_days(record, now)
    if days is None:
        return check('backup', 'note', backup.line(record, now), 'the backup record carries no time')
    if record.get('set') != backup.SET:
        return check('backup', 'note', f"{backup.line(record, now)}, but this checkout says set {backup.SET}",
                     f'{cli_name()} ops backup plan')
    if days > backup.OLD_AFTER_DAYS:
        return check('backup', 'problem', f'nothing backed up for {days:.0f} days: {backup.line(record, now)}',
                     f'systemctl status {declared.resource("backup.timer")}')
    if not record.get('sha256'):
        return check('backup', 'note', backup.line(record, now), f'{cli_name()} ops backup run')
    verified = record.get('verify') or {}
    if not verified.get('at'):
        return check('backup', 'note', backup.line(record, now),
                     f'{cli_name()} ops backup verify downloads it back and re-runs the aggregation')
    try:
        since = backup.age_days(verified, now)
    except (TypeError, ValueError):
        since = None
    if since is None:
        return check('backup', 'note', backup.line(record, now), 'the verify record carries no time')
    if since > backup.VERIFY_AFTER_DAYS:
        return check('backup', 'note', f'{backup.line(record, now)}, last restore test {since:.0f} days ago',
                     f'{cli_name()} ops backup verify, or systemctl status {declared.resource("backup-verify.timer")}')
    return check('backup', 'ok', backup.line(record, now))


def audit_check(root, now=None):
    """The saved audit (T9.7). It opens no connection: `ops update check` fetched this, and doctor only reads it."""
    report = audit.load(Path(root) / 'artifacts/components/audit.json')
    if report is None:
        return check('audit', 'note', 'no audit saved yet', f'{cli_name()} ops update check')
    line, days = audit.line(report), audit.age_days(report, now)
    if days > audit.OLD_AFTER_DAYS:
        return check('audit', 'note', f'{line}: {days:.0f} days old, so the daily check has stopped',
                     f'systemctl status {declared.resource("update-check.timer")}')
    numbers = audit.counts(report)
    if numbers['advisories'] or numbers['stale'] or numbers['unread']:
        return check('audit', 'note', line, 'artifacts/components/audit.json names them')
    return check('audit', 'ok', line)


def recovery_check(root, now=None):
    """The recovery-only automation: what it last did, and whether a person is needed.

    It reads `artifacts/recovery/state.json`, which `scripts/recover.py` writes on its own timer. Like every
    other check here it asks nothing of a host and loads nothing: the automation's own record is the evidence.
    """
    record = recovery.load(Path(root))
    if record is None:
        return check('recovery', 'note', 'no automatic reclaim has run here yet',
                     f'systemctl status {declared.resource("recovery.timer")}, or {cli_name()} ops recover status')
    decision = record.get('decision')
    status = ('problem' if recovery.breaker_open(record) or decision == 'reclaim_failed'
              else 'ok' if decision in ('idle', 'reset') else 'note')
    return check('recovery', status, recovery.line(record, now), recovery.next_step(record))


def gateway_check(gateway):
    healthy, line = gateway()
    return check('gateway', 'ok' if healthy else 'problem', line,
                 None if healthy else f'systemctl status {declared.resource("gateway")}')


def summary(root, hosts, gateway, drift=None, preflight=None, hygiene_read=None):
    """The record. The scripts are imported here because they live outside the package."""
    if drift is None or preflight is None:
        sys.path.insert(0, str(paths.CODE / 'scripts'))
        import drift as drift_module
        import preflight as preflight_module
        drift, preflight = drift or drift_module, preflight or preflight_module
    found = []
    for name, read in (('gateway', lambda: [gateway_check(gateway)]), ('git', lambda: [git_check(root)]),
                       ('hygiene', hygiene_read or (lambda: [hygiene_check(root)])),
                       ('dashboard.probes', lambda: [dashboard_probes_check(root)]),
                       ('drift', lambda: drift_checks(drift, hosts)),
                       ('preflight', lambda: preflight_checks(preflight, hosts)),
                       ('suite.leases', lambda: [lease_check(root)]), ('audit', lambda: [audit_check(root)]),
                       ('backup', lambda: [backup_check(root)]), ('recovery', lambda: [recovery_check(root)])):
        try:
            found += read()
        except Exception as error:      # noqa: BLE001 - see drift_checks
            found.append(check(name, 'unknown', f'{type(error).__name__}: {error}'))
    found.sort(key=lambda item: STATES.index(item['status']))
    return {'schema_version': 1,
            'generated': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
            'summary': {state: sum(item['status'] == state for item in found) for state in STATES},
            'checks': found}


def exit_code(report):
    return 1 if report['summary']['problem'] else 3 if report['summary']['unknown'] else 0


def screen(report):
    """Everything that is not `ok`, and a count of what is: the short answer is the point."""
    lines = [f"{item['status']:<8} {item['id']:<34} {item['evidence']}"
             + (f"\n{'':<8} {'next':<34} {item['next']}" if item['next'] else '')
             for item in report['checks'] if item['status'] != 'ok']
    counts = ', '.join(f"{report['summary'][state]} {state}" for state in STATES)
    return '\n'.join([*lines, counts])
