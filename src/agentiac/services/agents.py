"""Agent service: declared Agents, their pinned version, path and capability matrix.

Every capability row is tied to the version actually pinned. Nothing unpinned is given a
capability claim, and a probe from another version is `unverified`, not evidence for this one.
"""
import datetime
import json
import subprocess
from pathlib import Path

from agentiac import adapters, declared, paths

ROOT = paths.root()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _version(root, entry):
    if entry.get('lock'):
        package = paths.locate(entry['lock'], root)
        data = json.loads((package.parent / 'package.json').read_text())
        deps = data.get('dependencies') or {}
        return str(next(iter(deps.values()))) if deps else None
    if entry.get('pin'):
        return json.loads(paths.locate(entry['pin'], root).read_text()).get('version')
    return None



def _pin(root, entry):
    if entry.get('pin'):
        data = json.loads(paths.locate(entry['pin'], root).read_text())
        return {'path': entry['pin'], 'sha256': data.get('sha256') or data.get('tree_sha256'),
                'bytes': data.get('bytes')}
    if entry.get('lock'):
        return {'path': entry['lock'], 'sha256': None, 'bytes': None}
    return None


def rows(root=ROOT, ids=None, show=False):
    """The CLI rows: a pinned Agent, or the reason it is not pinned. `show` adds the matrix."""
    # Explicit ids need only their adapters; the declaration graph is read only to list all Agents.
    chosen = [entry['id'] for entry in declared.load(root).agents] if ids is None else list(ids)
    rows = []
    for agent_id in chosen:
        entry = adapters.adapter(agent_id)
        version = None
        note = ''
        try:
            if entry.installed():
                version = entry.version()
            else:
                note = 'not pinned'
        except (adapters.AdapterError, OSError, subprocess.SubprocessError) as error:
            note = str(error)
        try:
            paths = list(entry.allowed_paths(root=root))
        except adapters.AdapterError as error:
            paths = [str(error)]
        row = {'id': entry.id, 'version': version, 'note': note, 'install': str(entry.install),
               'gateway_only': entry.gateway_only, 'allowed_paths': paths, 'baseline': entry.baseline}
        if show:
            row['capabilities'] = adapters.capabilities_for(entry.id, version, root=root) if version else \
                {name: {'value': 'unverified', 'reason': 'not pinned'} for name in adapters.CAPABILITIES}
        rows.append(row)
    return rows


def load(root=ROOT, now=None):
    graph = declared.load(root)
    rows_out = []
    for entry in graph.agents:
        # GET readers must never execute an installed Agent binary to discover its version.
        # Installed-version attestation belongs to an explicit future collector/probe.
        row = {'id': entry['id'], 'version': None, 'install': entry.get('install'),
               'gateway_only': entry.get('gateway_only'),
               'allowed_paths': entry.get('allowed_paths') or [],
               'capabilities': {name: {'value': 'unverified',
                                      'reason': 'installed version has not been probed'}
                                for name in adapters.CAPABILITIES}}
        row['pin'] = _pin(root, entry)
        row['declared_version'] = _version(root, entry)
        row['endpoint'] = adapters.adapter(entry['id']).endpoint
        rows_out.append(row)
    return {'schema_version': 1, 'as_of': now or _now(), 'agents': rows_out}
