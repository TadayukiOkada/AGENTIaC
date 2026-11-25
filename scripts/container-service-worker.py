#!/usr/bin/env python3
"""The worker a declared service runs: launch the pinned container, admit it, wait for its readiness, hold it.

`<root>/service.json` (what the manifest declares), `<root>/launch-args.json` (the argv this site launches the engine
with, image reference included) and `<root>/backend-manifest.json` (the pin the launched container must match) are
written by `borrow.prepare_backend`. Nothing here names an engine: the container name, the port, the readiness route and
the launch command all come from those three files, which is what makes a second container-served engine a declaration
rather than a second script.

Deliberately not here yet, each because it is a measurement or a decision this checkout does not have:

- no memory admission threshold. Spark TensorFold's worker requires 100 GiB `MemAvailable`, a number that batch measured
  for its own engine; this engine's envelope is Stage 1c's measurement (§2.5 of the evaluation plan), and a guessed
  threshold on a 128 GB unified-memory host would refuse or admit by luck. Memory *is* sampled, so the measurement
  exists when the window runs.
- no inline smoke completion. TensorFold's worker asks its own model for "OK"; the generic readiness answer here is the
  declared route answering, and the text check is `scripts/smoke.py`'s, which the harness runs against the served front.

The API key reaches the engine without ever being a command-line argument (`ps` and `docker inspect` show those to anyone
on a shared host): the manifest's `service.secret_files` names small files this worker writes into `<root>`, mode 0600,
just before the launch, with `{api_key}` replaced by the key `borrow.prepare_backend` wrote to `<root>/api.key`. The
site's launch arguments mount such a file read-only and point the engine at it (for example `--config` with
`api-key = "{api_key}"`). The same key is what the readiness route is asked with.
"""
import datetime
import json
import os
import pathlib
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request

R = pathlib.Path(__file__).resolve().parent
SERVICE = json.loads((R / 'service.json').read_text())
MANIFEST = json.loads((R / 'backend-manifest.json').read_text())
N = SERVICE.get('container')
PORT = SERVICE['port']
STATUS_PATH = '/' + (SERVICE.get('status_path') or '/v1/models').lstrip('/')
PIN = (MANIFEST.get('image') or {}).get('id')
LABEL = 'io.agent-infra.owner'
RESERVED = {'api.key', 'service.json', 'launch-args.json', 'backend-manifest.json', 'worker.py', 'status.json',
            'status.json.tmp', 'memory.jsonl', 'trial-watch.py', 'watch', 'manifest.json', 'ondemand'}
STOP = 'service termination requested'
S = {'phase': 'starting'}
started = False


def save():
    S['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    tmp = R / 'status.json.tmp'
    tmp.write_text(json.dumps(S, indent=2) + '\n')
    tmp.replace(R / 'status.json')


def cmd(argv):
    return subprocess.run(argv, text=True, capture_output=True, timeout=30)


def terminate(sig, frame):
    raise SystemExit(0)  # an idle unload or a release is a clean exit, not a failed unit


signal.signal(signal.SIGTERM, terminate)


def sample():
    values = {line.split(':')[0]: int(line.split()[1]) * 1024
              for line in pathlib.Path('/proc/meminfo').read_text().splitlines()
              if len(line.split()) > 1 and line.split()[1].isdigit()}
    row = {'at': time.time(), **{key: values[key] for key in ('MemAvailable', 'SwapTotal', 'SwapFree')}}
    with (R / 'memory.jsonl').open('a') as log:
        log.write(json.dumps(row) + '\n')
    return values['MemAvailable']


def gpu_pids(output):
    """PIDs nvidia-smi names: digits only, so a `No running processes found` line is not a PID."""
    return {line.strip() for line in output.splitlines() if line.strip().isdigit()}


def top_pids(output):
    """PIDs `docker top <name> -eo pid` printed, without its header line."""
    return {line.strip() for line in output.splitlines()[1:] if line.strip().isdigit()}


KEY = re.compile(r'[A-Za-z0-9._~+/=-]{16,}')       # nothing a TOML/JSON/shell string would have to escape
NAME_VALUE = re.compile(r'([A-Z_][A-Z0-9_]*)=([^=].*)')    # an environment name, and a value that is not padding
SECRET_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}')


def api_key():
    """The key in `<root>/api.key`, or '' when there is none.

    The first line that is not blank or a comment is the key: either the bare key, or `NAME=value` when the left side
    is an environment-style name (`LITELLM_MASTER_KEY=...`) - a bare key that itself contains `=` (base64 padding)
    stays whole.
    The file is not read through a symlink.
    """
    try:
        descriptor = os.open(R / 'api.key', os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ''
    with os.fdopen(descriptor) as stream:
        text = stream.read()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = NAME_VALUE.fullmatch(line)
        return match.group(2).strip() if match else line
    return ''


def bearer_headers():
    value = api_key()
    return {'Authorization': 'Bearer ' + value} if value else {}


def secret_entries():
    """The declared `service.secret_files`, all checked before any is written (as `borrow.service_spec` checks them)."""
    entries = SERVICE.get('secret_files') or []
    if not isinstance(entries, list) or not all(
            isinstance(entry, dict) and set(entry) == {'path', 'text'} and isinstance(entry['path'], str)
            and SECRET_NAME.fullmatch(entry['path']) and entry['path'] not in RESERVED
            and isinstance(entry['text'], str) and '{api_key}' in entry['text'] and len(entry['text']) <= 4096
            for entry in entries) or len({entry['path'] for entry in entries}) != len(entries):
        raise RuntimeError('secret_files must be distinct file names of their own in the service root, each with an '
                           '{api_key} text')
    return entries


def write_secret_files():
    """Write each declared `service.secret_files` entry into <root>, mode 0600, with `{api_key}` filled in.

    Everything is checked first, so a refused declaration writes nothing; a write that fails or is interrupted leaves
    no temporary copy of the key behind.
    """
    entries = secret_entries()
    if not entries:
        return
    key = api_key()
    if not KEY.fullmatch(key):
        raise RuntimeError('secret_files are declared but <root>/api.key holds no usable key')
    for entry in entries:
        path, temporary = R / entry['path'], R / f'.{entry["path"]}.tmp'
        temporary.unlink(missing_ok=True)
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(entry['text'].replace('{api_key}', key))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def guard():
    if cmd(['docker', 'inspect', '--format', '{{.State.Running}}', N]).stdout.strip() != 'true':
        raise RuntimeError('backend exited')
    top = cmd(['docker', 'top', N, '-eo', 'pid'])
    gpu = cmd(['nvidia-smi', '--query-compute-apps=pid', '--format=csv,noheader'])
    if top.returncode or gpu.returncode:
        raise RuntimeError('GPU/ownership check failed')
    foreign = gpu_pids(gpu.stdout) - top_pids(top.stdout)
    if foreign:
        # our engine may have spawned its GPU process between the two reads: look at the container once more
        after = cmd(['docker', 'top', N, '-eo', 'pid'])
        if after.returncode:
            raise RuntimeError('GPU/ownership check failed')
        foreign = gpu_pids(gpu.stdout) - (top_pids(top.stdout) | top_pids(after.stdout))
    if foreign:
        raise RuntimeError('foreign GPU workload appeared')


def ready():
    request = urllib.request.Request(f'http://127.0.0.1:{PORT}{STATUS_PATH}', headers=bearer_headers())
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            if not 200 <= response.status < 300:
                return False
            raw = response.read()
    except Exception:
        return False
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        return True
    if isinstance(body, dict):
        if body.get('loaded') is False:
            return False
        if STATUS_PATH == '/v1/models' and 'data' in body and not body['data']:
            return False
    return True


try:
    save()
    sample()
    if not N:
        raise RuntimeError('service.json names no container')
    gpu = cmd(['nvidia-smi', '--query-compute-apps=pid', '--format=csv,noheader'])
    if gpu.returncode or gpu_pids(gpu.stdout):
        raise RuntimeError('foreign GPU workload or GPU check failure')
    write_secret_files()   # a refused declaration or key stops here, before anything of Docker's is touched
    existing = cmd(['docker', 'inspect', N])
    if existing.returncode == 0:
        item = json.loads(existing.stdout)[0]
        # The name is ours only if we labelled it and it runs the image the manifest pins: an unrecognized container
        # holding the name is not something this worker may delete, and another image is a different engine.
        if item['State']['Running'] or item['Config'].get('Labels', {}).get(LABEL) != 'agent-infra':
            raise RuntimeError('container name is in use by a running or unowned container')
        if PIN and item['Image'] != PIN:
            raise RuntimeError(f'container holds {item["Image"]}, the manifest pins {PIN}')
    with socket.socket() as sock:  # whether or not a stopped container of ours is about to be replaced
        if sock.connect_ex(('127.0.0.1', PORT)) == 0:
            raise RuntimeError('port occupied')
    if existing.returncode == 0 and cmd(['docker', 'rm', N]).returncode:
        raise RuntimeError('could not remove stopped owned container')
    launched = cmd(json.loads((R / 'launch-args.json').read_text()))
    if launched.returncode:
        raise RuntimeError('docker launch failed: ' + launched.stderr)
    started = True
    if PIN:
        now = cmd(['docker', 'inspect', N])
        actual = json.loads(now.stdout)[0].get('Image') if now.returncode == 0 else None
        if actual != PIN:
            raise RuntimeError(f'launched container holds {actual}, the manifest pins {PIN}')
    S['phase'] = 'loading'
    save()
    deadline = time.monotonic() + int(SERVICE.get('start_timeout_s') or 1300)
    while time.monotonic() < deadline:
        sample()
        guard()
        if ready():
            break
        time.sleep(min(5, max(0.1, deadline - time.monotonic())))
    else:
        raise TimeoutError('readiness timeout on ' + STATUS_PATH)
    S['phase'] = 'ready'
    save()
    while True:
        sample()
        guard()
        time.sleep(10)
except BaseException as error:
    stopping = isinstance(error, SystemExit)
    if stopping:
        print(STOP, file=sys.stderr, flush=True)
    S['phase'] = 'stopped' if stopping else 'failed'
    S['reason'] = STOP if stopping else str(error)
    save()
    raise
finally:
    if started:
        stop = cmd(['docker', 'stop', '-t', '10', N])
        S['stop_returncode'] = stop.returncode
        S['container_stopped'] = stop.returncode == 0
        save()
        if stop.returncode:
            raise RuntimeError('container did not stop')
