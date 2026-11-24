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
- no API key handling beyond presenting `<root>/api.key` to the readiness route. How a given engine is *given* that key
  is an engine flag question, recorded as Stage 1c's (§2.3 of the evaluation plan).
"""
import datetime
import json
import pathlib
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


def bearer_headers():
    key_file = R / 'api.key'
    if not key_file.is_file():
        return {}
    lines = [line.strip() for line in key_file.read_text().splitlines() if line.strip() and not line.startswith('#')]
    value = lines[0].split('=', 1)[-1].strip() if lines else ''
    return {'Authorization': 'Bearer ' + value} if value else {}


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
