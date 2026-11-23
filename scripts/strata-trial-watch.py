#!/usr/bin/env python3
"""Worker-side resource log and stop rules for a trial backend on a borrowed worker.

Written for a Strata trial (strata-128k-plan.md) and shaped to watch any engine the project starts on a worker: a
Strata process in a systemd unit, or an engine inside a container under a wrapper unit (--container).

Every --interval seconds one JSON line: host MemAvailable, swap used, the measured cgroup's memory.current and
memory.peak with the memory.stat anon/file split, GPU memory used, GPU processes that are not ours, and the engine's
own figures from --status-path when it answers. Stop rules stop only our units, the on-demand front first so nothing
loads the model again: MemAvailable below --min-available-gib, or another process on the GPU. The reason is logged;
there is no restart.

Two things this watch must get right, because both were wrong once:

* **Whose GPU PIDs are ours.** A containerized engine's process lives in Docker's container cgroup, not in the wrapper
  unit's cgroup (`--unit`), so comparing `nvidia-smi` PIDs against the unit's `cgroup.procs` calls our own container
  foreign and stops the service at the first sample. With `--container` the PIDs come from
  `docker top <container> -eo pid`, the pattern `spark-tensorfold-worker.py` already uses.
* **Which cgroup holds the model's memory.** `docker run` puts the container in its own scope
  (`system.slice/docker-<id>.scope` by default), so the wrapper unit's `memory.current` reports the launcher, not the
  model. With `--container` the watch measures the container's cgroup when it can find it, and every record names the
  cgroup and the PID source it used (`cgroup_path`, `pid_source`) so a log never reads as something it was not.
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


def api_key(text):
    """The bearer value from a key file, which comes in one of two shapes.

    A claim writes an env file (`LITELLM_MASTER_KEY=...`) into the prepared root, while a host's own front key file
    (`<root>/api.key`) holds the bare value. Reading the second with `split('=', 1)[1]` raises IndexError, so a trial
    watch pointed at such a host died at startup rather than sampling once.
    """
    value = text.strip()
    return value.split('=', 1)[1].strip() if '=' in value else value


def docker_pids(top_output):
    """The PIDs `docker top <container> -eo pid` printed, skipping its header line."""
    lines = [line.strip() for line in top_output.splitlines() if line.strip()]
    pids = set()
    for line in lines[1:]:
        try:
            pids.add(int(line.split()[0]))
        except ValueError:
            continue
    return pids


def container_cgroup_paths(inspect_output, root='/sys/fs/cgroup'):
    """Candidate cgroup directories for a container, from the `docker inspect` id and CgroupParent.

    Docker's own cgroup is where the model's pages actually sit. Under systemd the default is
    `system.slice/docker-<id>.scope`; a `--cgroup-parent` moves it, and a parent given as an absolute path is the
    directory itself. The candidates come back in the order worth trying and the caller uses the first that exists, so
    the record is not a guess: it names the path it read.
    """
    lines = inspect_output.split('\t')
    container_id = (lines[0] or '').strip()
    parent = (lines[1] if len(lines) > 1 else '').strip()
    if not container_id:
        return []
    scope = f'docker-{container_id}.scope'
    if parent.startswith('/'):
        return [Path(parent), Path(root) / parent.lstrip('/')]
    base = Path(root) / (parent or 'system.slice')
    return [base / scope, Path(root) / 'system.slice' / scope]


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


def status_figure(body, key):
    """What the engine's own status JSON contributes to the record, whatever engine it is.

    Strata answers `/v1/status` with an `engine` object; a containerized engine answers `/health` with its own shape.
    So the field is a choice (`--status-key`), and an answer that does not carry it is still worth logging as
    `status_ok`: the engine answered with JSON, which is the fact a resource log needs.
    """
    if not isinstance(body, dict):
        return None, False
    return body.get(key), True


def engine_status(port, key, status_path):
    """The engine's status JSON, or None where it does not answer (not loaded yet, no such route, wrong port)."""
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{port}{status_path}',
                                         headers={'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def container_report(container):
    """(cgroup path or None, our PIDs, pid source) for an engine running inside a container."""
    inspected = subprocess.run(['docker', 'inspect', '--format', '{{.Id}}\t{{.HostConfig.CgroupParent}}', container],
                               capture_output=True, text=True)
    group = next((path for path in container_cgroup_paths(inspected.stdout) if path.exists()), None)
    top = subprocess.run(['docker', 'top', container, '-eo', 'pid'], capture_output=True, text=True)
    return group, docker_pids(top.stdout), 'docker-top'


def sample(port, key, unit, container=None, status_path='/v1/status', status_key='engine'):
    available, swap = meminfo(Path('/proc/meminfo').read_text())
    record = {'time': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'mem_available': available,
              'swap_used': swap, 'cgroup_current': None, 'cgroup_peak': None, 'cgroup_anon': None,
              'cgroup_file': None, 'cgroup_path': None, 'pid_source': 'unit-cgroup', 'foreign_gpu_pids': []}
    if container:
        group, ours, record['pid_source'] = container_report(container)
    else:
        group, ours = cgroup_files(unit), set()
    if group and group.exists():
        record['cgroup_path'] = str(group)
        record['cgroup_current'] = int((group / 'memory.current').read_text())
        peak = group / 'memory.peak'
        record['cgroup_peak'] = int(peak.read_text()) if peak.exists() else None
        stat = group / 'memory.stat'
        record['cgroup_anon'], record['cgroup_file'] = memory_stat(stat.read_text()) if stat.exists() else (None, None)
        if not container:  # a process engine: the unit's own PIDs are the engine's
            ours = ours | {int(p) for p in (group / 'cgroup.procs').read_text().split()}
    gpu = subprocess.run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                         capture_output=True, text=True).stdout
    record['gpu_used_mib'] = gpu_used_mib(gpu)
    apps = subprocess.run(['nvidia-smi', '--query-compute-apps=pid,used_gpu_memory', '--format=csv,noheader,nounits'],
                          capture_output=True, text=True).stdout
    record['foreign_gpu_pids'] = foreign_gpu_pids(apps, ours)
    record['engine'], record['status_ok'] = status_figure(engine_status(port, key, status_path), status_key)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True, help="the engine's internal API port")
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=10)
    parser.add_argument('--min-available-gib', type=float, default=2)
    parser.add_argument('--unit', required=True,
                       help="the systemd unit the project started for the engine; with --container it is the wrapper "
                            'unit, and stopping it is still what releases the GPU')
    parser.add_argument('--container', help='the engine runs in this container: take the GPU PIDs from `docker top` '
                                            'and measure the container cgroup instead of the unit cgroup')
    parser.add_argument('--status-path', default='/v1/status',
                        help="the engine's own status route (Strata: /v1/status; a containerized engine: /health)")
    parser.add_argument('--status-key', default='engine', help='the field to keep from that JSON')
    args = parser.parse_args()
    key = api_key(args.key_file.read_text())
    args.log.parent.mkdir(parents=True, exist_ok=True)
    while True:
        record = sample(args.port, key, args.unit, args.container, args.status_path, args.status_key)
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
