"""The newest stored memory reading of a host, and whether it can be trusted.

T12.5 lost its memory check twice. A snippet that read the newest file without looking at its age printed
31.0 GiB from hours before the run, and a hand-written check read the control host's /proc/meminfo and called
it Spark. So this reads only what `agentiac ops metrics HOST` stored for that host, and a reading that is
too old is `unknown`, not a pass. Exit codes follow PA §13: 0 fine, 1 below the floor, 3 undetermined.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from agentiac.errors import command as cli_name

FINE, BELOW_FLOOR, UNDETERMINED = 0, 1, 3


def latest(folder, node, former=()):
    """The (time, MemAvailable in KiB) of `node`'s newest sample, or None. A sample with no reading is skipped.

    FORMER are names the host was known by before (inventory `former_names`): their files are its readings too.
    """
    files = [(path.name[len(name) + 1:], path) for name in (node, *former) for path in Path(folder).glob(f'{name}-*.jsonl')]
    for _stamp, path in sorted(files, reverse=True):     # newest timestamp first, whichever name filed it
        for line in reversed(path.read_text().splitlines()):
            try:
                row = json.loads(line)
                return datetime.datetime.fromisoformat(row['time']), row['mem_kib']['MemAvailable']
            except (ValueError, KeyError, TypeError):
                continue    # a probe error, or a line still being written
    return None


def verdict(sample, node, floor_gib, max_age_s, now):
    """The exit code and the one line to print for `sample`."""
    if sample is None:
        return UNDETERMINED, f'{node}: no memory sample stored; start {cli_name()} ops metrics {node}'
    taken, available = sample
    age = (now - taken).total_seconds()
    if age > max_age_s:
        return UNDETERMINED, f'{node}: newest memory sample is {age:.0f} s old (limit {max_age_s} s), so it says nothing'
    gib = available / 2**20
    line = f'{node}: MemAvailable {gib:.1f} GiB, {age:.0f} s old, floor {floor_gib:g} GiB'
    return (BELOW_FLOOR, line + ': BELOW FLOOR') if gib < floor_gib else (FINE, line + ': ok')


def report(folder, node, floor_gib, max_age_s, now=None, former=()):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return verdict(latest(folder, node, former), node, floor_gib, max_age_s, now)
