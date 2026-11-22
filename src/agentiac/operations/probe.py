"""Bounded minimal probes over a fixed local/SSH protocol, with durable launch intent."""
import hashlib
from functools import cache
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import sys
import time

from agentiac.services.inventory import nodes
from . import probe_remote

PROBE_CODE = Path(probe_remote.__file__).read_text()
OUTPUT_LIMIT = 16384
POLL_S = 0.05


@cache
def implementation_id():
    directory = Path(__file__).parent
    return hashlib.sha256(b''.join((directory / name).read_bytes() for name in
                                  ('probe.py', 'probe_remote.py', 'worker.py', 'ledger.py'))).hexdigest()


def endpoint_for(root, target):
    spec = nodes(root).get(target)
    if spec is None:
        raise ValueError('target is no longer declared')
    connection = spec.get('ansible_connection', 'ssh')
    if connection == 'local':
        return {'connection': 'local'}
    if connection != 'ssh':
        raise ValueError('unsupported connection type')
    host, user, port = spec.get('ansible_host'), spec.get('ansible_user'), spec.get('ansible_port', 22)
    if not isinstance(host, str) or len(host) > 253 or host.startswith('-'):
        raise ValueError('invalid declared host')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host):
            raise ValueError('invalid declared host') from None
    if not isinstance(user, str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', user):
        raise ValueError('invalid declared SSH user')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('invalid declared SSH port')
    return {'connection': 'ssh', 'host': host, 'user': user, 'port': port}


def argv_for(endpoint, token, mode='run', settings=None, code=None):
    code = PROBE_CODE if code is None else code
    if not re.fullmatch(r'[a-f0-9]{32}', token) or mode not in {'run', 'inspect'}:
        raise ValueError('invalid probe protocol request')
    python = sys.executable if endpoint['connection'] == 'local' else '/usr/bin/python3'
    command = [python, '-I', '-u', '-c', code, probe_remote.MARKER, mode, token]
    if mode == 'inspect':
        command.append(json.dumps(settings, separators=(',', ':')))
    if endpoint['connection'] == 'local':
        return command
    # Ignore user SSH config (ProxyCommand/forwarding/multiplexing); never accept host keys here.
    return ['ssh', '-F', '/dev/null', '-x', '-T',
            '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UpdateHostKeys=no', '-o', 'ClearAllForwardings=yes',
            '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
            '-o', 'ConnectTimeout=3', '-o', 'ConnectionAttempts=1',
            '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=1',
            '-l', endpoint['user'], '-p', str(endpoint['port']), '--', endpoint['host'],
            shlex.join(command)]


def local_identity(pid):
    return probe_remote.identity(pid)


def group_alive(pgid):
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != 'Z':
                return True
        except FileNotFoundError:
            continue
    return False


def stop_group(handle):
    """Only a recorded session leader with an unchanged Linux process identity."""
    pid = handle['pid']
    try:
        if local_identity(pid) != handle:
            return True  # That process exited; never signal a recycled PID.
        if os.getpgid(pid) != pid or handle['uid'] != os.getuid():
            return False
        for sig in (signal.SIGTERM, signal.SIGKILL):
            os.killpg(pid, sig)
            until = time.monotonic() + 0.5
            while time.monotonic() < until:
                if not group_alive(pid):
                    return True
                time.sleep(0.02)
        return not group_alive(pid)
    except (ProcessLookupError, FileNotFoundError):
        return not group_alive(pid)


def handle_alive(handle):
    """True while the recorded process, or any member of its process group, may still run."""
    try:
        if local_identity(handle['pid']) == handle:
            return True
    except (ProcessLookupError, FileNotFoundError):
        pass
    return group_alive(handle['pid'])


def find_local(argv):
    handles = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            parts = (entry / 'cmdline').read_bytes().split(b'\0')[:-1]
            if parts == [os.fsencode(value) for value in argv]:
                handles.append(local_identity(int(entry.name)))
        except FileNotFoundError:
            continue
    return handles


def valid_handle(value):
    return (isinstance(value, dict) and set(value) == {'pid', 'start_ticks', 'boot_id', 'uid'}
            and type(value['pid']) is int and value['pid'] > 1
            and type(value['uid']) is int and value['uid'] >= 0
            and isinstance(value['start_ticks'], str) and value['start_ticks'].isdigit()
            and isinstance(value['boot_id'], str)
            and bool(re.fullmatch(r'[a-f0-9-]{36}', value['boot_id'])))


def valid_data(value):
    expected = {'system', 'release', 'machine', 'cpu_count', 'memory_total_bytes',
                'memory_available_bytes', 'source', 'collected_at'}
    if not isinstance(value, dict) or set(value) != expected or value['source'] != 'linux-proc-v1':
        return False
    for key in ('system', 'release', 'machine', 'collected_at'):
        if not isinstance(value[key], str) or len(value[key]) > 128 or any(ord(c) < 32 for c in value[key]):
            return False
    for key in ('cpu_count', 'memory_total_bytes', 'memory_available_bytes'):
        if value[key] is not None and (type(value[key]) is not int or value[key] < 0):
            return False
    return True


def bounded_inspect(endpoint, token, settings, argv_builder=argv_for):
    """An independent check proves remote absence; SSH disconnect never does."""
    process = subprocess.Popen(argv_builder(endpoint, token, 'inspect', settings),
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    handle = local_identity(process.pid)
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            until = time.monotonic() + 3
            while selector.get_map() and time.monotonic() < until:
                for key, _ in selector.select(POLL_S):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    output.extend(chunk)
                    if len(output) > OUTPUT_LIMIT:
                        return 'unknown'
            if selector.get_map():
                return 'unknown'
        process.wait(timeout=0.2)
        value = json.loads(output)
        if not isinstance(value, dict):
            return 'unknown'
        return value['state'] if process.returncode == 0 and value.get('state') in {'absent', 'running'} else 'unknown'
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return 'unknown'
    finally:
        stop_group(handle)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        process.stdout.close()


class LiveProbeExecutor:
    def __init__(self, root, ledger=None, tick=None, argv_builder=argv_for):
        self.root, self.ledger = Path(root), ledger
        self.tick = tick or (lambda: None)
        self.argv_builder = argv_builder

    def run(self, job):
        if self.ledger is None:
            raise RuntimeError('a durable probe ledger is required')
        endpoint = endpoint_for(self.root, job['target'])
        token = self.ledger.execution_token(job)
        argv = self.argv_builder(endpoint, token)
        digest = hashlib.sha256(PROBE_CODE.encode()).hexdigest()
        self.ledger.prepare_execution(job, endpoint, argv, token, digest)
        # prepare_execution must commit before Popen; no network effect after failed intent.
        process = None
        local = remote = data = None
        reason = 'protocol_error'
        authorized = False
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
            local = local_identity(process.pid)
            self.ledger.record_execution(job, local_handle=local)
            output = bytearray()
            total = 0
            deadline = time.monotonic() + job['timeout_s']
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
                while selector.get_map():
                    self.tick()
                    row = self.ledger.get(job['id'])
                    if row['status'] == 'cancel_requested':
                        reason = 'cancelled'
                        break
                    if time.monotonic() >= deadline:
                        reason = 'timeout'
                        break
                    for key, _ in selector.select(POLL_S):
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        total += len(chunk)
                        if total > OUTPUT_LIMIT:
                            raise ValueError('output_limit')
                        if key.data != 'stdout':
                            continue
                        output.extend(chunk)
                        while b'\n' in output:
                            line, _, output = output.partition(b'\n')
                            message = json.loads(line)
                            if not isinstance(message, dict):
                                raise ValueError('invalid_protocol')
                            if not authorized and message.get('type') == 'handle' and valid_handle(message.get('handle')):
                                remote = message['handle']
                                # Durable GO intent includes the remote identity, eliminating the launch/handle gap.
                                self.ledger.record_execution(job, remote_handle=remote, authorized=True)
                                authorized = True
                                process.stdin.write(b'go\n')
                                process.stdin.flush()
                                process.stdin.close()
                            elif authorized and data is None and message.get('type') == 'result' and valid_data(message.get('data')):
                                data = message['data']
                            else:
                                raise ValueError('invalid_protocol')
                else:
                    process.wait(timeout=0.2)
                    if (authorized and data is not None and process.returncode == 0 and not output
                            and not group_alive(local['pid'])):
                        return {'status': 'succeeded', 'result': {'node': job['target'], 'probe': data,
                                'scope': 'minimal-os-cpu-memory', 'stopped': True},
                                'logs': ['Minimal OS/CPU/memory probe completed.'], 'events': []}
                    reason = 'probe_failed'
        except (OSError, ValueError, subprocess.TimeoutExpired):
            reason = 'probe_failed'
        finally:
            if process is not None:
                if local is not None:
                    stop_group(local)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream and not stream.closed:
                        stream.close()
        stopped = local is None or not group_alive(local['pid'])
        if process is not None:
            state = bounded_inspect(endpoint, token, {'code_sha256': digest, 'handle': remote, 'stop': True},
                                    self.argv_builder)
            stopped = stopped and state == 'absent'
        return {'status': ('cancelled' if reason == 'cancelled' else 'failed') if stopped else 'needs_attention',
                'result': {'node': job['target'], 'reason': reason, 'stopped': stopped},
                'logs': ['Probe stopped and verified.' if stopped else 'Probe needs ownership reconciliation.'],
                'events': []}

    def reconcile(self, execution, stop=False):
        handles = find_local(execution['argv'])
        if execution['local_handle'] is not None:
            handles.append(execution['local_handle'])
        if execution['endpoint'].get('connection') == 'local' and not stop:
            # For a local target the local process is the probe itself: inspection never signals it.
            if any(handle_alive(handle) for handle in handles):
                return False
        elif any(not stop_group(handle) for handle in handles):
            # For SSH this is only the owned transport; the remote probe is stopped only with `stop`.
            return False
        state = bounded_inspect(execution['endpoint'], execution['token'],
                                {'code_sha256': execution['code_sha256'],
                                 'handle': execution['remote_handle'], 'stop': stop}, self.argv_builder)
        return state == 'absent'
