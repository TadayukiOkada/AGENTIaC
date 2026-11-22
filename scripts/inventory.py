#!/usr/bin/env python3
"""Read-only inventory; output may contain host metadata. Keep artifacts local.

Every probe is a read: nothing here loads a model, starts a service or touches a route, and
the same probes run on borrowed hosts. The raw output goes to a timestamped snapshot under
`artifacts/inventory/` for a human to check; the same readings are also distilled into the
PA §9.1 record (facts with source and confidence, plus `hw_fp`) and written per host to
`artifacts/inventory/<host>.json`, which is what `agentiac eval suite` records as
provenance (T10.3). A fact nothing could read stays `unknown` with a null value — see
`src/agentiac/hardware.py`.
"""
import argparse
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import sys
import subprocess

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
from agentiac import declared, hardware, identities  # noqa: E402

PROBES = {
    'os': 'hostname; uname -a; cat /etc/os-release',
    'resources': 'uptime; free -b; df -B1 / /home; swapon --show',
    'meminfo': 'head -5 /proc/meminfo',
    'gpu': 'nvidia-smi; ls -l /dev/nvidia* /dev/nvhost-gpu /dev/nvmap 2>/dev/null',
    'containers': "docker version --format '{{.Server.Version}}'; docker ps -a --format '{{.Names}} {{.Image}} {{.Status}} {{.Ports}}'",
    'services': 'systemctl --type=service --state=running --no-pager',
    'ports': 'ss -lnt',
    'processes': 'ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -25',
    'cuda': 'ls /usr/local; test ! -x /usr/local/cuda/bin/nvcc || /usr/local/cuda/bin/nvcc --version',
    'jetson': 'test ! -r /etc/nv_tegra_release || cat /etc/nv_tegra_release; command -v nvpmodel >/dev/null && sudo -n nvpmodel -q',
    # the three below exist only so hardware.py can read a fact instead of parsing a table; each
    # labels its own output, because a missing file must not shift a value into the wrong fact, and
    # each ends in `|| true` because a host without the tool is an unknown fact, not a failed probe
    'cpu_facts': 'echo "arch: $(uname -m)"; echo "cpus: $(nproc)"; '
                 'echo "dtmodel: $(tr -d \'\\0\' < /proc/device-tree/model 2>/dev/null)"; '
                 'echo "socid: $(cat /sys/devices/soc0/soc_id 2>/dev/null)"; '
                 'echo "socfamily: $(cat /sys/devices/soc0/family 2>/dev/null)"; '
                 "grep -iE '^(model name|model|hardware|soc|chip)[[:space:]]*:' /proc/cpuinfo | head -4 || true",
    'gpu_facts': "nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader,nounits 2>/dev/null || true",
    'gpu_list': "nvidia-smi -L 2>/dev/null || true",
}


def host_names():
    """Probe target -> inventory name: the hosts arrive as `<account>@<address>`, but a record
    must be filed under the inventory name, which is what the declared graph and the suite call it."""
    try:
        nodes = declared.load(ROOT).nodes
    except OSError:
        return {}
    found = {'local': 'control'}
    for name, facts in nodes.items():
        address = facts.get('ansible_host')
        for key in (address, f'{declared.account(facts, ROOT)}@{address}' if address else None, name):
            if key:
                found[key] = name
    return found


def host_name(raw, known=None):
    """The inventory name of a probe target; an unknown target keeps the bare host it answered as."""
    known = host_names() if known is None else known
    if raw in known:
        return known[raw]
    return 'control' if raw == 'local' else raw.split('@')[-1].split('.')[0]


def collect(host, known=None):
    result = {'host': host, 'probes': {}}
    for name, command in PROBES.items():
        argv = ['bash', '-c', command] if host == 'local' else [
            'ssh', '-x', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host, command]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            result['probes'][name] = {'exit_code': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr}
        except subprocess.TimeoutExpired:
            result['probes'][name] = {'error': 'timeout'}
    result['name'] = host_name(host, known)
    result['record'] = hardware.record(result['name'], result['probes'], nodes().get(result['name']))
    return result


_NODES = None


def nodes():
    """The declared node facts, so a host's `inference_cpus` is recorded as `manual`, not as a reading."""
    global _NODES
    if _NODES is None:
        try:
            _NODES = declared.load(ROOT).nodes
        except OSError:  # an inventory read must not stop a hardware probe
            _NODES = {}
    return _NODES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hosts', nargs='+', default=['local'])
    parser.add_argument('--output', type=Path, default=Path('artifacts/inventory'))
    args = parser.parse_args()
    os.umask(0o077)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    args.output.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        known = host_names()
        hosts = list(pool.map(lambda host: collect(host, known), args.hosts))
    path = args.output / (stamp + '.json')
    with path.open('x') as out:
        json.dump({'time_utc': stamp, 'warning': 'local sandbox can hide GPU devices', 'hosts': hosts}, out, indent=2)
    for host in hosts:
        previous = hardware.latest(args.output, host['record']['host'])
        if host['record']['hw_fp'] == identities.UNKNOWN and previous and previous['hw_fp'] != identities.UNKNOWN:
            # A host that was off is not a host that changed: the last good record stays, and ages out by itself.
            print(f"{host['name']:<8} not read this time; keeping the record from {previous['collected_at']}")
            continue
        hardware.write(args.output, host['record'])  # latest per host, for the suite provenance (T10.3)
        facts = host['record']['facts']
        read = sum(1 for name, entry in facts.items() if entry['confidence'] != 'unknown')
        print(f"{host['name']:<8} hw_fp {host['record']['hw_fp']:<17} "
              f"{read}/{len(facts)} facts read, unknown: {', '.join(host['record']['unknowns']) or 'none'}")
    print(path)
    return int(any('error' in p or p.get('exit_code') == 255 for h in hosts for p in h['probes'].values()))


if __name__ == '__main__':
    raise SystemExit(main())
