#!/usr/bin/env python3
"""Check what a host needs before anything of ours runs there. Reads only; exit 1 on a failure.

The prerequisites nothing here installs: passwordless sudo, Docker and its NVIDIA runtime, the
GPU, the tools the build and tunnels use, and room on the disk that holds our files. Exit 0 when every
host is ready, 1 when one is not, 3 when the only thing wrong is a host that could not be asked.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import borrow  # noqa: E402

CHECKS = r'''
check() { name=$1; shift; if "$@" >/dev/null 2>&1; then echo "$name=ok"; else echo "$name=fail"; fi; }
check sudo sudo -n true
check docker docker info
check nvidia_runtime sh -c 'docker info 2>/dev/null | grep -qi nvidia'
check gpu nvidia-smi -L
check git git --version
check python3 python3 --version
echo "free_gib=$(df -BG --output=avail %(path)s 2>/dev/null | tail -1 | tr -dc 0-9)"
'''
MINIMUM_FREE_GIB = 40


def parse(text):
    """Turn the script's name=value lines into (failed checks, free GiB)."""
    values = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
    failed = sorted(name for name, value in values.items() if value == 'fail')
    free = values.get('free_gib', '')
    return failed, int(free) if free.isdigit() else None


def storage_path(host):
    """The disk a root would be prepared on: the directory the versioned Strata roots sit in.

    The RTX 3090 host's are on /mnt/data, not on the home filesystem its retired `strata_root` used to name; the versioned map
    is what a preparation writes to, so it is what the free-space check has to measure. A host with no versioned
    root has nothing prepared, and the check runs where the worker's home is.
    """
    roots = list(host.get('strata_trial_roots', {}).values())
    return str(Path(roots[0]).parent) if roots and roots[0] else '$HOME'


def check_host(name, host):
    """One host's record: the failed checks and free space, or `unreachable` with why."""
    try:
        done = borrow.on_host(host, ['bash', '-s'], input=CHECKS % {'path': storage_path(host)})
    except (OSError, subprocess.SubprocessError) as error:
        return {'host': name, 'unreachable': str(error)}
    if done is None:
        return {'host': name, 'unreachable': 'no answer'}
    if done.returncode:
        return {'host': name, 'unreachable': done.stderr.strip()[:200]}
    failed, free = parse(done.stdout)
    return {'host': name, 'failed': failed, 'free_gib': free,
            'ok': not failed and free is not None and free >= MINIMUM_FREE_GIB}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('hosts', nargs='+')
    parser.add_argument('--json', action='store_true', help='one JSON document instead of one line per host')
    args = parser.parse_args(argv)
    hosts = borrow.hosts()
    records = [check_host(name, hosts[name]) for name in args.hosts]
    if args.json:
        print(json.dumps({'schema_version': 1, 'hosts': records}))
    else:
        for record in records:
            print(json.dumps(record))
    if any('unreachable' not in record and not record['ok'] for record in records):
        return 1
    return 3 if any('unreachable' in record for record in records) else 0


if __name__ == '__main__':
    sys.exit(main())
