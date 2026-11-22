#!/usr/bin/env python3
"""Worker-side resource log and stop rules for a Strata trial backend (strata-128k-plan.md).

Every --interval seconds one JSON line: host MemAvailable, swap used, our inference cgroup
memory.current/memory.peak with the memory.stat anon/file split, GPU memory used, GPU processes that are not ours,
and the engine's own figures from /v1/status (expert cache, KV streaming) when it is loaded. Stop rules stop only our
units, the on-demand front first so nothing loads the model again: MemAvailable below --min-available-gib, or another
process on the GPU. The reason is logged; there is no restart.
"""
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import time
import urllib.request



def meminfo(text):
    values = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in text.splitlines() if line.split()[1:]}
    return values['MemAvailable'], values.get('SwapTotal', 0) - values.get('SwapFree', 0)


def foreign_gpu_pids(apps_output, ours):
    """PIDs on the GPU that are not in our cgroup."""
    pids = []
    for line in apps_output.splitlines():
        try:
            pid = int(line.split(',')[0])
        except ValueError:
            continue
        if pid not in ours:
            pids.append(pid)
    return pids


def gpu_used_mib(output):
    """GPU memory used, or None where nvidia-smi has no figure: a Jetson prints [N/A], and its GPU memory is the host
    RAM that MemAvailable already watches. Its process list is [N/A] too, so another GPU process goes unseen there."""
    try:
        return int(output.split()[0])
    except (IndexError, ValueError):
        return None


def stop_reason(sample, min_available):
    if sample['mem_available'] < min_available:
        return 'host MemAvailable below the floor'
    if sample['foreign_gpu_pids']:
        return 'another process is on the GPU'
    return None


def memory_stat(text):
    """(anon, file) from a cgroup memory.stat.

    The split is the point: the resident mode's page-locked expert copy is anon and cannot be reclaimed, while the
    pack's and the PLE table's cached pages are file and the kernel can take them back. Without it memory.current is
    one number that conflates pinned experts with reclaimable cache, when only the anon half is un-reclaimable.
    """
    values = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            values[parts[0]] = int(parts[1])
    return values.get('anon'), values.get('file')


def cgroup_files(unit):
    group = subprocess.run(['systemctl', 'show', unit, '-p', 'ControlGroup', '--value'],
                           capture_output=True, text=True).stdout.strip()
    return Path('/sys/fs/cgroup') / group.lstrip('/') if group else None


def sample(port, key, unit):
    available, swap = meminfo(Path('/proc/meminfo').read_text())
    record = {'time': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'mem_available': available,
              'swap_used': swap, 'cgroup_current': None, 'cgroup_peak': None, 'cgroup_anon': None,
              'cgroup_file': None, 'foreign_gpu_pids': []}
    group, ours = cgroup_files(unit), set()
    if group and group.exists():
        record['cgroup_current'] = int((group / 'memory.current').read_text())
        peak = group / 'memory.peak'
        record['cgroup_peak'] = int(peak.read_text()) if peak.exists() else None
        stat = group / 'memory.stat'
        record['cgroup_anon'], record['cgroup_file'] = memory_stat(stat.read_text()) if stat.exists() else (None, None)
        ours = {int(p) for p in (group / 'cgroup.procs').read_text().split()}
    gpu = subprocess.run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                         capture_output=True, text=True).stdout
    record['gpu_used_mib'] = gpu_used_mib(gpu)
    apps = subprocess.run(['nvidia-smi', '--query-compute-apps=pid,used_gpu_memory', '--format=csv,noheader,nounits'],
                          capture_output=True, text=True).stdout
    record['foreign_gpu_pids'] = foreign_gpu_pids(apps, ours)
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{port}/v1/status', headers={'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(request, timeout=5) as response:
            record['engine'] = json.load(response).get('engine')
    except (OSError, ValueError):
        record['engine'] = None
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True, help="the engine's internal API port")
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=10)
    parser.add_argument('--min-available-gib', type=float, default=2)
    parser.add_argument('--unit', required=True, help='the engine unit it watches: <resource_prefix>-strata-inference')
    parser.add_argument('--front', required=True, help="the on-demand front's unit: <resource_prefix>-ondemand")
    args = parser.parse_args()
    key = args.key_file.read_text().split('=', 1)[1].strip()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    while True:
        record = sample(args.port, key, args.unit)
        reason = stop_reason(record, args.min_available_gib * 1024**3)
        if reason:
            for unit in (args.front, args.unit):
                subprocess.run(['sudo', '-n', 'systemctl', 'stop', unit], capture_output=True)
            record['stopped'] = reason
        with args.log.open('a') as log:
            log.write(json.dumps(record) + '\n')
        if reason:
            raise SystemExit(reason)
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
