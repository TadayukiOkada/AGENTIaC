"""Exact local user-service plans, offline snapshots and compensating code rollback.

Never operates an inference/gateway unit; no database reset or remote host mutation.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import uuid

from .lifecycle import StateLock
from . import releases, snapshots


def plan_hash(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


HEALTH_TIMEOUT_S = 30


class Systemctl:
    def loaded(self, name):
        """False only for a unit the user manager does not know; stop/disable would fail on it."""
        result = subprocess.run(['systemctl', '--user', 'show', name, '--property=LoadState', '--value'],
                                capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise ValueError('the operator user-systemd manager is unavailable')
        return result.stdout.strip() != 'not-found'

    def call(self, *args):
        return subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True,
                              timeout=20, check=True)

    def state(self, name):
        result = subprocess.run(['systemctl', '--user', 'show', name, '--property=LoadState', '--value'],
                                capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise ValueError('the operator user-systemd manager is unavailable')
        if result.stdout.strip() == 'not-found':
            return {'active': False, 'enabled': False}
        if result.stdout.strip() != 'loaded':
            raise ValueError('dashboard unit is not in a stable loaded state')
        values = []
        for verb in ('is-active', 'is-enabled'):
            result = subprocess.run(['systemctl', '--user', verb, name], capture_output=True,
                                    text=True, timeout=10)
            value = result.stdout.strip()
            if value in {'activating', 'deactivating', 'reloading', 'masked', 'masked-runtime'}:
                raise ValueError('dashboard service is transitioning or masked')
            allowed = ({'active', 'inactive', 'failed', 'unknown'} if verb == 'is-active'
                       else {'enabled', 'disabled', 'static', 'indirect', 'linked', 'not-found'})
            if value not in allowed or result.returncode not in (0, 1, 3, 4):
                raise ValueError('the operator user-systemd manager is unavailable')
            values.append(value == ('active' if verb == 'is-active' else 'enabled'))
        return {'active': values[0], 'enabled': values[1]}


def unit_snapshot(unit_dir, manager):
    result = {}
    for name in releases.UNITS:
        path = Path(unit_dir) / name
        if path.is_symlink():
            raise ValueError('refusing a symlinked unit')
        text = path.read_text() if path.exists() else None
        if text is not None and not text.startswith(releases.MARKER + '\n'):
            raise ValueError('unit is not managed by this dashboard installer')
        state = manager.state(name)
        if text is None and (state['active'] or state['enabled']):
            raise ValueError('refusing an unowned/transient service with a dashboard unit name')
        result[name] = {'text': text, **state}
    return result


def private_location(path, root):
    path, artifacts = Path(path).resolve(), (Path(root) / 'artifacts').resolve()
    if path == artifacts or artifacts in path.parents:
        raise ValueError('dashboard state/releases/backups must not be placed in set-B artifacts')
    return str(path)


def create_plan(release, root, state, unit_dir, history, port=8000, targets=(), cert=None,
                key=None, ca=None, ssh_tunnel=False, enable=False, allow_uncommitted=False,
                action='activate', manager=None):
    manager = manager or Systemctl()
    root, state, history, unit_dir = map(lambda p: str(Path(p).resolve()), (root, state, history, unit_dir))
    private_location(state, root)
    private_location(history, root)
    if action == 'activate':
        from agentiac import declared
        graph = declared.load(root)
        if declared.references(graph):
            raise ValueError('resolve dangling declaration references before service deployment')
    before = unit_snapshot(unit_dir, manager)
    previous = Path(history) / 'current.json'
    prior = json.loads(previous.read_text()) if previous.exists() else None
    if action == 'rollback':
        if prior is None:
            raise ValueError('no deployment receipt is available for rollback')
        if before != prior['after']:
            raise ValueError('installed dashboard state drifted since its deployment receipt')
        desired, config = prior['before'], prior['previous_config']
        if config and prior['config'] and config['state'] != prior['config']['state']:
            raise ValueError('a database restore/state handoff cannot be undone by code rollback')
        if config is None and any(row['text'] for row in desired.values()):
            raise ValueError('previous configuration is unknown; do not guess a rollback')
    elif action == 'stop':
        desired = {name: {**row, 'active': False, 'enabled': False} for name, row in before.items()}
        config = prior['config'] if prior else None
    elif action == 'activate':
        release = str(Path(release).resolve())
        receipt = releases.verify(release, environment=True)
        if receipt['uncommitted'] and not allow_uncommitted:
            raise ValueError('commit/review the source first, or explicitly plan an uncommitted local prototype')
        if not ssh_tunnel and not (cert and key and ca):
            raise ValueError('provide TLS cert/key/CA, or explicitly select SSH-tunnel HTTP')
        from agentiac.operations.probe import endpoint_for
        for target in targets:
            endpoint_for(root, target)
        units = releases.render_units(release, root, state, port, targets, cert, key)
        config = {'release': release, 'release_id': receipt['id'], 'release_receipt_sha256': snapshots.digest(Path(release) / 'release.json')
                  if (Path(release) / 'release.json').exists() else None, 'root': root, 'state': state,
                  'port': port, 'targets': sorted(set(targets)), 'cert': cert, 'key': key, 'ca': ca,
                  'ssh_tunnel': ssh_tunnel}
        desired = {name: {'text': text, 'active': text is not None,
                          'enabled': bool(enable and text)} for name, text in units.items()}
    else:
        raise ValueError('unknown dashboard deployment action')
    if config and prior and action == 'activate':
        if prior['config'] and (config['state'] != prior['config']['state'] or config['root'] != prior['config']['root']):
            raise ValueError('use a separately reviewed offline state handoff, not a code update')
    if config and prior is None and action == 'activate':
        for row in before.values():
            if row['active'] and row['text'] and f'Environment=AGENTIAC_MANAGEMENT_STATE={state}\n' not in row['text']:
                raise ValueError('stop the old state instance before reviewing a restored-state handoff')
    if config and action != 'stop':
        config = dict(config)
        # Detect code and TLS material changed after review; never put key contents in the plan.
        # A stop never starts TLS, so it must stay possible when that material is missing.
        config['tls_files'] = {str(Path(file).resolve()): snapshots.digest(file)
                               for file in (config.get('cert'), config.get('key'), config.get('ca')) if file}
    return {'version': 1, 'id': uuid.uuid4().hex, 'action': action, 'unit_dir': unit_dir,
            'history': history, 'root': root, 'state': state, 'before': before, 'desired': desired,
            'config': config, 'previous_config': prior['config'] if prior else None}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    snapshots.fsync(path.parent)


def install_units(unit_dir, rows):
    unit_dir = Path(unit_dir)
    unit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in releases.UNITS:
        path, text = unit_dir / name, rows[name]['text']
        if path.is_symlink():
            raise ValueError('unit became a symlink during deployment')
        if text is None:
            path.unlink(missing_ok=True)
        else:
            temporary = path.with_name('.' + name + '.' + uuid.uuid4().hex)
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
    snapshots.fsync(unit_dir)


def stop_units(manager, rows):
    for name in reversed(releases.UNITS):
        # A unit that was never installed (failure before install) has nothing to stop; asking the
        # user manager to stop it fails and would turn a clean compensation into a retained intent.
        if (rows[name]['text'] is not None or rows[name]['active']) and manager.loaded(name):
            manager.call('stop', name)
            manager.call('disable', name)


def start_units(manager, rows):
    manager.call('daemon-reload')
    for name in releases.UNITS:
        row = rows[name]
        if row['text'] is None:
            continue
        manager.call('enable' if row['enabled'] else 'disable', name)
        if row['active']:
            manager.call('start', name)


def health(config, timeout_s=HEALTH_TIMEOUT_S):
    """Wait for the started API by elapsed time, not by attempts: a refused connection returns at
    once, so a fixed attempt count gave a cold-starting service only about two seconds."""
    if config is None:
        return
    context = ssl.create_default_context(cafile=config['ca']) if config['cert'] else None
    url = ('https' if config['cert'] else 'http') + '://127.0.0.1:' + str(config['port'])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context))
    last_error = None
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            try:
                opener.open(url + '/api/v1/session', timeout=1)
                raise ValueError('management API allowed an unauthenticated session read')
            except urllib.error.HTTPError as error:
                if error.code != 401 or json.load(error).get('code') != 'unauthorized':
                    raise ValueError('management API authentication contract failed')
            with opener.open(url + '/dashboard/', timeout=1) as response:
                expected = Path(config['release']) / 'dashboard/dist/index.html'
                if hashlib.sha256(response.read(1024 * 1024)).hexdigest() != snapshots.digest(expected):
                    raise ValueError('dashboard assets do not match the approved release')
            return
        except OSError as error:
            # Not answering yet (or an HTTP error other than the expected 401): keep waiting.
            last_error = error
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        except ValueError as error:
            # The service answered and broke its contract; waiting longer cannot fix that.
            raise ValueError('dashboard post-check failed') from error
    raise ValueError('dashboard post-check failed') from last_error


def apply(plan, approval, manager=None, check_health=health):
    manager = manager or Systemctl()
    if approval != plan_hash(plan) or plan.get('version') != 1:
        raise ValueError('approval does not match this exact dashboard plan')
    if set(plan['desired']) != set(releases.UNITS) or set(plan['before']) != set(releases.UNITS):
        raise ValueError('dashboard plan must name exactly its two owned units')
    history = Path(plan['history'])
    private_location(history, plan['root'])
    private_location(plan['state'], plan['root'])
    history.mkdir(parents=True, exist_ok=True, mode=0o700)
    with StateLock(history, exclusive=True):
        unresolved_intent = (history / '.incomplete').exists()
        if unresolved_intent and plan['action'] != 'stop':
            raise ValueError('a prior deployment has unresolved compensation; inspect it before applying again')
        if unit_snapshot(plan['unit_dir'], manager) != plan['before']:
            raise ValueError('dashboard service/unit state changed after plan review')
        if unresolved_intent:
            # Emergency stop never clears or replaces the failed transaction's evidence.
            stop_units(manager, plan['before'])
            after = unit_snapshot(plan['unit_dir'], manager)
            if any(row['active'] or row['enabled'] for row in after.values()):
                raise ValueError('owned dashboard units did not stop')
            write_json(history / (plan['id'] + '-stop.json'),
                       {'plan': plan, 'after': after, 'unresolved_intent_retained': True})
            return {'applied': True, 'stopped': True, 'unresolved_intent_retained': True}
        attempted = [history / 'backups' / plan['id'], history / (plan['id'] + '.json'),
                     history / (plan['id'] + '-stop.json'), history / f"failed-{plan['id']}.json"]
        if any(path.exists() for path in attempted):
            # Checked before any downtime: a plan's snapshot/record paths are single-use.
            raise ValueError('this exact plan was already attempted; create a new plan')
        config = plan['config']
        if config and plan['action'] != 'stop':
            receipt = releases.verify(config['release'], environment=True)
            if (receipt['id'] != config['release_id']
                    or (config.get('release_receipt_sha256') is not None and
                        snapshots.digest(Path(config['release']) / 'release.json') != config['release_receipt_sha256'])):
                raise ValueError('release changed after plan review')
            releases.check_state(config['release'], plan['state'])
            for file, expected in config.get('tls_files', {}).items():
                if snapshots.digest(file) != expected:
                    raise ValueError('TLS material changed after plan review')
        # Validate DBs before causing downtime, without modifying them.
        from .paths import require_ready
        if plan['action'] != 'stop':
            require_ready(plan['state'])
            for name, signature in snapshots.expected_signatures().items():
                snapshots.validate_database(Path(plan['state']) / name, signature)
            from .session import SessionStore
            with closing(snapshots.connect_readonly(Path(plan['state']) / 'session.sqlite')) as connection:
                owner = connection.execute("SELECT password_hash FROM credentials WHERE actor='owner'").fetchone()
            import re
            if not owner or not re.fullmatch(r'scrypt\$[0-9a-f]{32}\$[0-9a-f]{128}', owner[0]):
                raise ValueError('initialize/recover the owner before approving service startup')
        write_json(history / '.incomplete', {'plan_hash': approval, 'plan': plan})
        schema_before = {name: snapshots.signature(Path(plan['state']) / name)
                         for name in snapshots.DATABASES} if plan['action'] != 'stop' else {}
        try:
            stop_units(manager, plan['before'])
            if plan['action'] != 'stop':
                folder = history / 'backups'
                folder.mkdir(mode=0o700, exist_ok=True)
                snapshots.backup(plan['state'], folder / plan['id'], root=plan['root'])
            install_units(plan['unit_dir'], plan['desired'])
            start_units(manager, plan['desired'])
            if config and plan['desired'][releases.UNITS[0]]['active']:
                check_health(config)
            after = unit_snapshot(plan['unit_dir'], manager)
            if after != plan['desired']:
                raise ValueError('dashboard units did not reach the approved state')
            record = {**plan, 'plan_hash': approval, 'after': after}
            write_json(history / (plan['id'] + '.json'), record)
            write_json(history / 'current.json', record)
            (history / '.incomplete').unlink()
            return {'applied': True, 'plan_hash': approval}
        except Exception as error:
            # Compensate code/units only. Never reset SQLite, Git or unrelated services.
            try:
                stop_units(manager, plan['desired'])
                if any(snapshots.signature(Path(plan['state']) / name) != old for name, old in schema_before.items()):
                    raise ValueError('database schema changed; automatic code rollback is unsafe')
                install_units(plan['unit_dir'], plan['before'])
                start_units(manager, plan['before'])
                if unit_snapshot(plan['unit_dir'], manager) != plan['before']:
                    raise ValueError('previous unit state was not restored')
                (history / '.incomplete').unlink()
            except Exception as rollback_error:
                raise ValueError('deployment failed and compensation needs operator attention; intent retained') from rollback_error
            raise ValueError('deployment failed; previous owned unit state restored, databases preserved') from error
