"""Control's record of its own claims (finding F05): `artifacts/claims/<host>.json`.

A claim used to leave nothing on control, so after it ended nobody could say when or with what it had been made
or whether a tunnel had outlived it. `borrow.claim` writes this record
once the front and the tunnel are up, and `borrow.release` removes it after the remote release succeeded.

It is control's memory of its own action, not proof that the front runs; `drift` compares the two.
"""
import datetime
import json
import os
import re

from . import mutation

DIRECTORY = 'artifacts/claims'


def _path(root, host):
    # a host name becomes a file name
    if not isinstance(host, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', host):
        raise ValueError(f'not a host name: {host!r}')
    return mutation.state_root(root) / DIRECTORY / f'{host}.json'


def write(root, host, backend, context, idle_unload_minutes, by):
    """Record a claim of HOST atomically; BY is 'claim' or 'reclaim'."""
    path = _path(root, host)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {'host': host, 'backend': backend, 'context': context, 'idle_unload_minutes': idle_unload_minutes,
              'claimed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
              'by': by, 'actor': mutation.actor()}
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(record) + '\n')
    os.replace(tmp, path)


def _names(root, host):
    """HOST, then every former name it declares: a record written before a rename is still this host's (T8.10)."""
    from agentiac import declared
    return [host] + [old for old, current in declared.former_names(declared.hosts(root)).items() if current == host]


def _read(root, name):
    try:
        record = json.loads(_path(root, name).read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def read(root, host):
    """The record of HOST (or of a former name of it), or None when it is missing or unreadable."""
    for name in _names(root, host):
        record = _read(root, name)
        if record is not None:
            return record
    return None


def clear(root, host):
    """Remove the record of HOST, and any written under a former name of it."""
    for name in _names(root, host):
        _path(root, name).unlink(missing_ok=True)


def all(root):  # noqa: A001 - the name D2.2 gives it
    """{host: record} for every readable record; one written under a former name is keyed by the current host."""
    from agentiac import declared
    nodes = declared.hosts(root)
    directory = mutation.state_root(root) / DIRECTORY
    records = {}
    for path in sorted(directory.glob('*.json')) if directory.is_dir() else ():
        record = _read(root, path.stem)
        if record is not None:
            records.setdefault(declared.current_host(path.stem, nodes), record)
    return records
