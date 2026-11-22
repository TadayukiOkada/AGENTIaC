"""Fixed, stdlib-only Linux probe protocol, sent verbatim to a declared host.

No subprocesses, files written, services, credentials, GPU or inventory enumeration.
The controller persists the process handle before authorizing the data read.
"""
import datetime
import hashlib
import json
import os
import re
import signal
import sys
import time

MARKER = '--agent-infra-minimal-probe-v1'


def emit(value):
    print(json.dumps(value, separators=(',', ':')), flush=True)


def identity(pid):
    with open('/proc/sys/kernel/random/boot_id') as stream:
        boot = stream.read(64).strip()
    with open(f'/proc/{pid}/stat') as stream:
        stat = stream.read(4096).rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_ticks': stat[19], 'boot_id': boot,
            'uid': os.stat(f'/proc/{pid}').st_uid}


def inspect(token, settings):
    """Only signal this protocol's exact code/nonce/UID; PID reuse is not ownership."""
    matches = []
    unknown = False
    for entry in os.scandir('/proc'):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            with open(f'/proc/{pid}/cmdline', 'rb') as stream:
                parts = stream.read(32768).split(b'\0')[:-1]
            if (len(parts) != 8 or parts[5:] != [MARKER.encode(), b'run', token.encode()]
                    or hashlib.sha256(parts[4]).hexdigest() != settings['code_sha256']):
                continue
            handle = identity(pid)
            expected = settings.get('handle')
            if expected is not None and handle != expected:
                unknown = True
                continue
            if settings.get('stop') and identity(pid) == handle:
                os.kill(pid, signal.SIGTERM)
                until = time.monotonic() + 0.4
                while time.monotonic() < until:
                    try:
                        if identity(pid) != handle:
                            break
                    except FileNotFoundError:
                        break
                    time.sleep(0.02)
                else:
                    if identity(pid) == handle:
                        os.kill(pid, signal.SIGKILL)
                # A subsequent inspection, not kill() returning, proves absence.
                continue
            matches.append(handle)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError):
            unknown = True
    if settings.get('stop'):
        settings['stop'] = False
        return inspect(token, settings)
    return {'state': 'unknown' if unknown else 'running' if matches else 'absent'}


def run():
    signal.alarm(35)  # Includes the pre-authorization handshake; no orphan can wait forever.
    emit({'type': 'handle', 'handle': identity(os.getpid())})
    if sys.stdin.readline(16) != 'go\n':
        return
    memory = {}
    with open('/proc/meminfo') as stream:
        for line in stream.read(4096).splitlines():
            name, _, value = line.partition(':')
            if name in {'MemTotal', 'MemAvailable'}:
                memory[name] = int(value.split()[0]) * 1024
    uname = os.uname()
    emit({'type': 'result', 'data': {
        'system': uname.sysname, 'release': uname.release, 'machine': uname.machine,
        'cpu_count': os.cpu_count(), 'memory_total_bytes': memory.get('MemTotal'),
        'memory_available_bytes': memory.get('MemAvailable'),
        'source': 'linux-proc-v1',
        'collected_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }})


if __name__ == '__main__':
    if (len(sys.argv) < 4 or sys.argv[1] != MARKER
            or not re.fullmatch(r'[a-f0-9]{32}', sys.argv[3])):
        raise SystemExit(2)
    if sys.argv[2] == 'run' and len(sys.argv) == 4:
        run()
    elif sys.argv[2] == 'inspect' and len(sys.argv) == 5:
        signal.alarm(3)
        emit(inspect(sys.argv[3], json.loads(sys.argv[4])))
    else:
        raise SystemExit(2)
