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
import re
import subprocess
import time
import urllib.request


def _run(argv, timeout=15):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def meminfo(text):
    values = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in text.splitlines() if line.split()[1:]}
    return values['MemAvailable'], values.get('SwapTotal', 0) - values.get('SwapFree', 0)


def api_key(text):
    """The bearer value from a key file, which comes in one of two shapes.

    A claim writes an env file (`LITELLM_MASTER_KEY=...`) into the prepared root, while a host's own front key file
    (`<root>/api.key`) holds the bare value. Reading the second with `split('=', 1)[1]` raises IndexError, so a trial
    watch pointed at such a host died at startup rather than sampling once.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            name, value = line.split('=', 1)
            name, value = name.strip(), value.strip()
            if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) and value.strip('='):
                return value
        return line
    return ''


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
    `system.slice/docker-<id>.scope`; a `--cgroup-parent` moves it, and `cgroupfs` places it under
    `docker/<id>`. The candidates come back in the order worth trying and the caller uses the first that exists,
    never matching the parent slice/cgroup directory itself.
    """
    lines = inspect_output.split('\t')
    container_id = (lines[0] or '').strip()
    parent = (lines[1] if len(lines) > 1 else '').strip()
    if not container_id:
        return []
    root_path = Path(root)
    scope = f'docker-{container_id}.scope'
    candidates = []
    if parent:
        parent_path = Path(parent)
        rel_base = root_path / parent.lstrip('/')
        if parent.startswith('/') and str(parent_path).startswith(str(root_path) + '/'):
            bases = [parent_path, rel_base]
        else:
            bases = [rel_base]
        for base in bases:
            candidates.append(base / scope)
            candidates.append(base / container_id)
    candidates.append(root_path / 'system.slice' / scope)
    candidates.append(root_path / 'docker' / container_id)
    return list(dict.fromkeys(candidates))


def proc_cgroup_path(cgroup_text, root='/sys/fs/cgroup'):
    """Cgroup v2 directory from `/proc/<pid>/cgroup`, or None when at root or unreadable."""
    for line in cgroup_text.splitlines():
        parts = line.split(':', 2)
        if len(parts) == 3 and parts[0] == '0' and parts[1] == '':
            rel = parts[2].strip().lstrip('/')
            return Path(root) / rel if rel else None
    return None


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
    shown = _run(['systemctl', 'show', unit, '-p', 'ControlGroup', '--value'])
    group = shown.stdout.strip() if shown and shown.returncode == 0 else ''
    return Path('/sys/fs/cgroup') / group.lstrip('/') if group else None


def cgroup_pids(group):
    """PIDs listed in a cgroup's `cgroup.procs`, or empty when the cgroup is missing/vanished."""
    if group is None:
        return set()
    try:
        return {int(p) for p in (group / 'cgroup.procs').read_text().split()}
    except (OSError, ValueError):
        return set()


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
    path = status_path if status_path.startswith('/') else '/' + status_path
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{port}{path}',
                                         headers={'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def container_report(container):
    """(cgroup path or None, our PIDs, pid source) for an engine running inside a container."""
    inspected = _run(['docker', 'inspect', '--format', '{{.Id}}\t{{.HostConfig.CgroupParent}}', container])
    group = None
    if inspected and inspected.returncode == 0:
        group = next((path for path in container_cgroup_paths(inspected.stdout) if path.exists()), None)
    top = _run(['docker', 'top', container, '-eo', 'pid'])
    pids = docker_pids(top.stdout) if top and top.returncode == 0 else set()
    if group is None:
        for pid in sorted(pids):
            try:
                candidate = proc_cgroup_path(Path(f'/proc/{pid}/cgroup').read_text())
            except OSError:
                continue
            if candidate is not None and candidate.exists():
                group = candidate
                break
    return group, pids, 'docker-top'


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
        try:
            current = int((group / 'memory.current').read_text())
            peak = group / 'memory.peak'
            peak_val = int(peak.read_text()) if peak.exists() else None
            stat = group / 'memory.stat'
            anon, file_bytes = memory_stat(stat.read_text()) if stat.exists() else (None, None)
            record['cgroup_path'] = str(group)
            record['cgroup_current'] = current
            record['cgroup_peak'] = peak_val
            record['cgroup_anon'], record['cgroup_file'] = anon, file_bytes
        except (OSError, ValueError):
            pass
        ours = ours | cgroup_pids(group)
    gpu = _run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'])
    record['gpu_used_mib'] = gpu_used_mib(gpu.stdout if gpu and gpu.returncode == 0 else '')
    apps = _run(['nvidia-smi', '--query-compute-apps=pid,used_gpu_memory', '--format=csv,noheader,nounits'])
    apps_out = apps.stdout if apps and apps.returncode == 0 else ''
    foreign = foreign_gpu_pids(apps_out, ours)
    if foreign:
        # Re-check our PIDs once after nvidia-smi so a newly spawned container/CUDA child is not misclassified.
        if container:
            group_after, ours_after, _ = container_report(container)
        else:
            group_after, ours_after = cgroup_files(unit), set()
        ours_after = ours_after | cgroup_pids(group_after)
        foreign = foreign_gpu_pids(apps_out, ours | ours_after)
    record['foreign_gpu_pids'] = foreign
    record['engine'], record['status_ok'] = status_figure(engine_status(port, key, status_path), status_key)
    return record


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True, help="the engine's internal API port")
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=10)
    parser.add_argument('--min-available-gib', type=float, default=2)
    parser.add_argument('--unit', required=True,
                        help='the systemd unit the project started for the engine; with --container it is the wrapper '
                             'unit, and stopping it is still what releases the GPU')
    parser.add_argument('--front', required=True, help="the on-demand front's unit: <resource_prefix>-ondemand")
    parser.add_argument('--container', help='the engine runs in this container: take the GPU PIDs from `docker top` '
                                            'and measure the container cgroup instead of the unit cgroup')
    parser.add_argument('--status-path', default='/v1/status',
                        help="the engine's own status route (Strata: /v1/status; a containerized engine: /health)")
    parser.add_argument('--status-key', default='engine', help='the field to keep from that JSON')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
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
