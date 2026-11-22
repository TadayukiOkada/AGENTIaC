"""Deployment records, read from local files only: control deploy, route and dashboard releases.

- Control: `artifacts/deployed.json` (written by `ops deploy`) against the checkout's HEAD, read from the
  Git files directly instead of running git.
- Route: the runtime route file, the declared default and the newest route changes from the route log.
- Dashboard: the plan/apply history (receipts, `current.json`, failed and emergency-stop records).

Only an allowlist leaves this module: no unit text, plan contents, TLS paths or fingerprints, and a release
is named by its directory name. A missing or unreadable record is `None` or an empty list, never a guess.
"""
import datetime
import json
import os
import re
from pathlib import Path

ROUTE_CHANGES = 20
HISTORY_LIMIT = 200
_RECEIPT = re.compile(r'^(?P<failed>failed-)?(?P<id>[0-9a-f]{32})(?P<stop>-stop)?\.json$')


def _json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _git_dir(root):
    dot = Path(root) / '.git'
    if dot.is_file():  # a worktree: "gitdir: <path>"
        text = dot.read_text().strip()
        return Path(text.partition('gitdir:')[2].strip()) if text.startswith('gitdir:') else None
    return dot if dot.is_dir() else None


def git_head(root):
    """The checkout's HEAD commit from the Git files, or None when it cannot be read exactly."""
    try:
        git = _git_dir(root)
        if git is None:
            return None
        head = (git / 'HEAD').read_text().strip()
        if re.fullmatch(r'[0-9a-f]{40}', head):
            return head
        if not head.startswith('ref: '):
            return None
        ref = head[5:].strip()
        common = git
        if (git / 'commondir').exists():
            common = (git / (git / 'commondir').read_text().strip()).resolve()
        for base in (git, common):
            loose = base / ref
            if loose.is_file():
                value = loose.read_text().strip()
                return value if re.fullmatch(r'[0-9a-f]{40}', value) else None
        packed = common / 'packed-refs'
        if packed.is_file():
            for line in packed.read_text().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref and re.fullmatch(r'[0-9a-f]{40}', parts[0]):
                    return parts[0]
    except OSError:
        return None
    return None


def control(root):
    deployed = _json(Path(root) / 'artifacts/deployed.json') or {}
    revision = deployed.get('revision') if isinstance(deployed.get('revision'), str) else None
    head = git_head(root)
    return {'revision': revision, 'deployed_at': deployed.get('time') if isinstance(deployed.get('time'), str) else None,
            'dirty': deployed.get('dirty') if isinstance(deployed.get('dirty'), bool) else None,
            'head': head, 'up_to_date': (revision == head) if revision and head else None}


def route(root, current):
    declared = _json(Path(root) / 'gateway/local-coder.json') or {}
    changes = []
    try:
        lines = (Path(root) / 'artifacts/gateway/route-log.jsonl').read_text().splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get('to'), str):
            changes.append({'time': entry.get('time') if isinstance(entry.get('time'), str) else None,
                            'from_target': entry.get('from') if isinstance(entry.get('from'), str) else None,
                            'to_target': entry['to']})
        if len(changes) >= ROUTE_CHANGES:
            break
    return {'current': current, 'declared_default': declared.get('default'), 'changes': changes}


def _release(config):
    if not isinstance(config, dict) or not isinstance(config.get('release'), str):
        return None, None, None
    path = Path(config['release'])
    manifest = _json(path / 'release.json') or {}
    revision = manifest.get('revision') if isinstance(manifest.get('revision'), str) else None
    release_id = config.get('release_id') if isinstance(config.get('release_id'), str) else None
    return path.name, release_id, revision


def _applied_at(history, plan_id, path):
    manifest = _json(Path(history) / 'backups' / plan_id / 'manifest.json') or {}
    if isinstance(manifest.get('created_at'), str):
        return manifest['created_at']
    try:  # a stop takes no snapshot; the record's own write time is the best local evidence
        stamp = os.stat(path).st_mtime
    except OSError:
        return None
    return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).isoformat()


def _entry(history, path, current_id):
    match = _RECEIPT.match(path.name)
    record = _json(path)
    if not match or not isinstance(record, dict):
        return None
    plan = record.get('plan') if (match['failed'] or match['stop']) else record
    if not isinstance(plan, dict):
        return None
    plan_id = match['id']
    config = plan.get('config') if isinstance(plan.get('config'), dict) else None
    release, release_id, revision = _release(config)
    previous, _, _ = _release(plan.get('previous_config'))
    desired = plan.get('desired') if isinstance(plan.get('desired'), dict) else {}
    targets = config.get('targets') if config and isinstance(config.get('targets'), list) else []
    if match['failed']:
        status = 'failed'
    elif match['stop']:
        status = 'emergency stop'
    elif plan_id == current_id:
        status = 'current'
    else:
        status = 'superseded'
    return {'id': plan_id, 'action': plan.get('action') if isinstance(plan.get('action'), str) else None,
            'status': status, 'applied_at': _applied_at(history, plan_id, path),
            'plan_hash': record.get('plan_hash') if isinstance(record.get('plan_hash'), str) else None,
            'release': release, 'release_id': release_id, 'revision': revision, 'previous_release': previous,
            'port': config.get('port') if config and isinstance(config.get('port'), int) else None,
            'transport': None if not config else ('ssh-tunnel' if config.get('ssh_tunnel') else
                                                  'tls' if config.get('cert') else None),
            'probe_targets': [target for target in targets if isinstance(target, str)],
            'enabled_at_login': any(isinstance(row, dict) and row.get('enabled') for row in desired.values())}


def dashboard(history):
    """Newest first. `current` is the receipt `current.json` names."""
    history = Path(history)
    current = _json(history / 'current.json') or {}
    current_id = current.get('id') if isinstance(current.get('id'), str) else None
    try:
        paths = [path for path in history.iterdir() if _RECEIPT.match(path.name)]
    except OSError:
        return {'current': None, 'entries': []}
    entries = [entry for entry in (_entry(history, path, current_id) for path in paths) if entry]
    entries.sort(key=lambda entry: (entry['applied_at'] or '', entry['id']), reverse=True)
    return {'current': current_id if any(entry['id'] == current_id for entry in entries) else None,
            'entries': entries[:HISTORY_LIMIT]}
