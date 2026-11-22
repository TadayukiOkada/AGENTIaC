"""Read-only evaluation watchdog; manifests opt in via their watch metadata."""
import argparse
import datetime
import hashlib
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

from agentiac import declared, paths
from agentiac.errors import command as cli_name

ROOT = paths.root()
TERMINAL = {'completed', 'failed', 'unexpected_stop'}
NORMAL = {'running', 'starting'}
REMOTE = r'''
import hashlib,json,pathlib,subprocess,sys
spec=json.loads(sys.argv[1]); result={'units':{},'status':{},'progress':[]}
for name in spec['worker_units']:
 p=subprocess.run(['systemctl','show',name,'-p','ActiveState','-p','Result','-p','LoadState'],text=True,capture_output=True,timeout=10)
 result['units'][name]=dict(l.split('=',1) for l in p.stdout.splitlines() if '=' in l)
root=pathlib.Path(spec['worker_directory'])
for name in ['smoke-status.json','prepare-status.json','download-method.json']:
 p=root/name
 if p.exists():
  try: result['status'][name]=json.loads(p.read_text())
  except ValueError: pass
container=spec.get('container')
if container:
 p=subprocess.run(['docker','logs','--tail','20',container],text=True,capture_output=True,timeout=10)
 result['log_hash']=hashlib.sha256((p.stdout+p.stderr).encode()).hexdigest()
for directory in spec.get('progress_directories',[]):
 root=pathlib.Path(directory)
 if root.exists():
  files=list(root.rglob('*.bin'))+list(root.rglob('*.incomplete'))
  result['progress'] += [(str(p),p.stat().st_blocks,p.stat().st_mtime_ns) for p in files if p.is_file()]
print(json.dumps(result))
'''


def strip_heartbeat(value):
    if isinstance(value, dict):
        return {k: strip_heartbeat(v) for k, v in value.items()
                if k not in ('updated_at', 'checked_at', 'log_hash')}
    if isinstance(value, list):
        return [strip_heartbeat(v) for v in value]
    return value


def classify(snapshot, unchanged_s, now, deadline, stall_s=1800):
    if snapshot.get('error'):
        return 'unreachable'
    units = list(snapshot['units'].values())
    phases = [v.get('phase') for v in snapshot.get('status', {}).values()]
    if any(u.get('ActiveState') == 'failed' for u in units) or any(
            p in ('failed', 'interrupted') for p in phases):
        return 'failed'
    active = any(u.get('ActiveState') in ('active', 'activating', 'deactivating') for u in units)
    if not active:
        if (units and all(u.get('LoadState', 'not-found') == 'not-found' for u in units)
                and not any(phases) and now < snapshot.get('launch_grace_until', 0)):
            return 'starting'
        if snapshot.get('expects_control'):
            control = snapshot.get('status', {}).get('control', {}).get('phase')
            if control != 'benchmark_complete':
                return 'unexpected_stop'
            if snapshot.get('expected_requests') is not None and snapshot.get('measured_requests') != snapshot['expected_requests']:
                return 'incomplete_results'
            return 'completed' if 'passed' in phases else 'unexpected_stop'
        return 'completed' if 'passed' in phases or 'verified' in phases else 'unexpected_stop'
    if now > deadline:
        return 'overdue'
    return 'stalled' if unchanged_s >= stall_s else 'running'


def transition(previous, snapshot, now, deadline):
    signature = hashlib.sha256(json.dumps(strip_heartbeat(snapshot), sort_keys=True).encode()).hexdigest()
    changed = now if previous.get('signature') != signature else previous['last_progress_at']
    status = classify(snapshot, now - changed, now, deadline)
    record = {'status': status, 'signature': signature, 'last_progress_at': changed,
              'checked_at': now, 'snapshot': snapshot, 'proof_version': 2,
              'acknowledged': previous.get('acknowledged', False) if previous.get('status') == status else False,
              'last_alert_at': previous.get('last_alert_at', 0) if previous.get('status') == status else 0}
    alert = status not in NORMAL and previous.get('status') != status
    return record, alert


def notify(batch, status):
    return desktop('Evaluation ' + status, batch + f'\n{cli_name()} eval watch status',
                   'normal' if status in ('completed', 'recovered') else 'critical')


def desktop(title, body, urgency):
    env = dict(os.environ)
    env['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path=/run/user/{os.getuid()}/bus'
    try:
        p = subprocess.run(['notify-send', '--app-name=agentiac', '--expire-time=0', '--urgency=' + urgency, title, body],
                           env=env, text=True, capture_output=True, timeout=8)
        return 'desktop_accepted_unconfirmed' if p.returncode == 0 else 'desktop unavailable'
    except (OSError, subprocess.TimeoutExpired):
        return 'desktop unavailable'


def collect(manifest, directory):
    spec = manifest['watch']
    command = 'python3 -c ' + shlex.quote(REMOTE) + ' ' + shlex.quote(json.dumps(spec))
    p = subprocess.run(['ssh', '-x', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                        '-o', 'ServerAliveInterval=10', spec['ssh_host'], command],
                       text=True, capture_output=True, timeout=60)
    if p.returncode:
        raise RuntimeError('worker status unavailable')
    snapshot = json.loads(p.stdout)
    snapshot['expects_control'] = bool(spec.get('control_units'))
    try:
        snapshot['launch_grace_until'] = datetime.datetime.fromisoformat(manifest['started_at']).timestamp() + 300
    except (KeyError, ValueError):
        snapshot['launch_grace_until'] = 0
    for name in spec.get('control_units', []):
        p = subprocess.run(['systemctl', 'show', name, '-p', 'ActiveState', '-p', 'Result'],
                           text=True, capture_output=True, timeout=10)
        snapshot['units'][name] = dict(l.split('=', 1) for l in p.stdout.splitlines() if '=' in l)
    p = directory / 'latest-status.json'
    if p.exists():
        try:
            data = json.loads(p.read_text())
            snapshot['status']['control'] = {'phase': data.get('phase')}
        except ValueError:
            pass
    p = directory / 'results.jsonl'
    snapshot['result_bytes'] = p.stat().st_size if p.exists() else 0
    benchmark = manifest.get('benchmark', {})
    if all(k in benchmark for k in ['depths', 'cases', 'repeats']):
        snapshot['expected_requests'] = len(benchmark['depths']) * len(benchmark['cases']) * benchmark['repeats']
        concurrency = benchmark.get('concurrency')
        if isinstance(concurrency, list):
            snapshot['expected_requests'] *= sum(concurrency)
        rows = []
        if p.exists():
            for line in p.read_text().splitlines():
                try:
                    row = json.loads(line)
                    if row.get('kind') in benchmark['cases']:
                        rows.append(row)
                except ValueError:
                    pass
        snapshot['measured_requests'] = len({r.get('id') for r in rows})
    return snapshot


def atomic(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def summary(state, directory):
    lines = ['# Evaluation watchdog', '', 'Last check: ' + state.get('checked_at', 'unknown'), '',
             '15min checks; 30min real-progress stall. Desktop acceptance is NOT user acknowledgement.', '',
             '| Batch | Status | Acknowledgement |', '|---|---|---|']
    pending = []
    for key, record in state['batches'].items():
        ack = 'acknowledged' if record.get('acknowledged') else 'pending'
        if record['status'] in NORMAL:
            ack = '-'
        elif not record.get('acknowledged'):
            pending.append((key, record['status']))
        lines.append(f'| {key} | {record["status"]} | {ack} |')
    (directory / 'latest.md').write_text('\n'.join(lines) + '\n')
    text = '# Unacknowledged evaluation alerts\n\n'
    text += '\n'.join(f'- {key}: **{status}**; `{cli_name()} eval watch ack {key}`' for key, status in pending) or 'None.'
    (directory / 'pending.md').write_text(text + '\n')


def health_problem(timer_active, checked_at, now):
    if not timer_active:
        return 'watch timer is not active'
    if checked_at is None or now - checked_at > 1200:
        return 'watch heartbeat older than20min'
    return None


def check_health(directory, state):
    now = time.time()
    p = subprocess.run(['systemctl', 'is-active', declared.resource('evaluation-watch.timer')],
                       text=True, capture_output=True, timeout=10)
    checked = state.get('checked_at')
    try:
        checked = datetime.datetime.fromisoformat(checked).timestamp() if checked else None
    except (ValueError, TypeError):
        checked = None
    problem = health_problem(p.stdout.strip() == 'active', checked, now)
    path = directory / 'health.json'
    try:
        old = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        old = {}
    status = 'unhealthy' if problem else 'healthy'
    record = {'status': status, 'problem': problem, 'checked_at': now,
              'last_alert_at': old.get('last_alert_at', 0)}
    if problem and (old.get('status') != status or now - record['last_alert_at'] >= 900):
        event = {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 'batch': 'evaluation-watch', 'status': status, 'reason': problem,
                 'notification': notify('evaluation-watch: ' + problem, status)}
        with (directory / 'alerts.jsonl').open('a') as f:
            f.write(json.dumps(event) + '\n')
        record['last_alert_at'] = now
        print(json.dumps(event), flush=True)
    atomic(path, record)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'status', 'test-notification', 'ack', 'health'])
    parser.add_argument('batch', nargs='?')
    parser.add_argument('--root', type=Path, default=ROOT / 'artifacts/spark-flash-next')
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'artifacts/evaluation-watch')
    args = parser.parse_args(argv)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    # Health must not wait for the main check's lock: it detects a stuck check.
    if args.action == 'health':
        try:
            state = json.loads((args.state_dir / 'state.json').read_text())
        except (OSError, ValueError):
            state = {}
        check_health(args.state_dir, state)
        return
    # Serialize manual checks/acknowledgements with the timer.
    lock = (args.state_dir / 'state.lock').open('a')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state_file = args.state_dir / 'state.json'
        state = json.loads(state_file.read_text()) if state_file.exists() else {'batches': {}}
        if args.action == 'test-notification':
            print(notify('Watch test; reminders every15min until acknowledged.', 'test'))
            return
        if args.action == 'status':
            print(json.dumps(state, indent=2))
            return
        if args.action == 'ack':
            if not args.batch:
                parser.error('ack requires a batch ID or all')
            if args.batch != 'all' and args.batch not in state['batches']:
                parser.error('unknown batch ID')
            for key, record in state['batches'].items():
                if args.batch in ('all', key) and record['status'] not in NORMAL:
                    record['acknowledged'] = True
                    record['acknowledged_at'] = time.time()
            atomic(state_file, state)
            summary(state, args.state_dir)
            return
        now = time.time()
        for path in sorted(args.root.glob('*/manifest.json')):
            try:
                manifest = json.loads(path.read_text())
                if 'watch' not in manifest:
                    continue
                key = manifest['id']
                previous = state['batches'].get(key, {})
                prior_units = previous.get('snapshot', {}).get('units', {}).values()
                if previous.get('status') in TERMINAL and previous.get('acknowledged') and previous.get('proof_version') == 2 and not any(
                        u.get('ActiveState') in ('active', 'activating', 'deactivating') for u in prior_units):
                    continue
                try:
                    snapshot = collect(manifest, path.parent)
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                    snapshot = {'error': 'status collection failed'}
                record, changed_alert = transition(previous, snapshot, now, manifest['deadline_epoch'])
                reminder = record['status'] not in NORMAL | {'completed'} and not record['acknowledged'] and now - record['last_alert_at'] >= 900
                recovered = previous.get('status') not in (None, 'running', 'starting', 'completed') and record['status'] in NORMAL
                if changed_alert or reminder or recovered:
                    event = {'at': datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
                             'batch': key, 'status': 'recovered' if recovered else record['status'], 'reminder': not changed_alert and not recovered,
                             'notification': notify(key, 'recovered' if recovered else record['status'])}
                    with (args.state_dir / 'alerts.jsonl').open('a') as f:
                        f.write(json.dumps(event) + '\n')
                    record['last_alert_at'] = now
                    print(json.dumps(event), flush=True)
                state['batches'][key] = record
            except (KeyError, ValueError, OSError) as exc:
                print(json.dumps({'manifest': str(path), 'error': type(exc).__name__}), flush=True)
        state['checked_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        atomic(state_file, state)
        summary(state, args.state_dir)
    finally:
        lock.close()


if __name__ == '__main__':
    main()
