"""Prepare immutable code releases and exact, private user-service plans."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .snapshots import digest, fsync
from .tls import validate_tls
from agentiac.errors import command as cli_name

MARKER = f'# Managed by {cli_name()} dashboard-v1'
ROLES = ('dashboard.service', 'dashboard-probes.service')   # the API, then the probe worker


def units(root=None):
    """The dashboard's systemd user units, named from the management root's declarations (D6.8)."""
    from agentiac import declared
    from . import paths as management_paths
    return tuple(declared.resource(role, root or management_paths.ROOT) for role in ROLES)


def __getattr__(name):
    """`releases.UNITS`, resolved on first use rather than at import (PEP 562)."""
    if name == 'UNITS':
        return units()
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


# Non-Python files the package reads next to itself (module-relative), so they belong to the code
# version. Listed only when present, so verifying an older release that predates one still works.
CODE_DATA = ('infra/components.schema.json', 'infra/retention.schema.json',
             'infra/backend-lifecycle.schema.json')


def code_files(root):
    return sorted([p for p in (root / 'src/agentiac').rglob('*.py')]
                  + [p for p in (root / 'dashboard/dist').rglob('*') if p.is_file()]
                  + [root / rel for rel in CODE_DATA if (root / rel).is_file()]
                  + [root / 'requirements-dashboard.lock'])


def release_id(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def prepare(source, destination):
    source, destination = Path(source).resolve(), Path(destination).absolute()
    paths = code_files(source)
    if not (source / 'dashboard/dist/index.html').is_file() or not paths:
        raise ValueError('build the dashboard before preparing a release')
    if source == destination or source in destination.parents:
        raise ValueError('release destination must be outside the source checkout')
    files, total = {}, 0
    for path in paths:
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != source):
            raise ValueError('release inputs may not contain symlinks')
        rel = path.relative_to(source).as_posix()
        if '..' in Path(rel).parts or any(part.startswith('.') for part in Path(rel).parts):
            raise ValueError('unsupported release input')
        total += path.stat().st_size
        if total > 64 * 1024 * 1024:
            raise ValueError('release exceeds the supported size bound')
        files[rel] = digest(path)
    revision = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=5)
    dirty = subprocess.run(['git', '-C', str(source), 'status', '--porcelain'], capture_output=True, text=True, timeout=5)
    receipt = {'version': 1, 'id': release_id(files), 'files': files,
               'revision': revision.stdout.strip() if revision.returncode == 0 else None,
               'uncommitted': dirty.returncode != 0 or bool(dirty.stdout)}
    destination.mkdir(mode=0o700)
    (destination / '.incomplete').touch(mode=0o600)
    for rel in files:
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source / rel, target)
        target.chmod(0o444)
    (destination / 'release.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    (destination / 'release.json').chmod(0o600)
    verify(destination, allow_incomplete=True)
    (destination / '.incomplete').unlink()
    fsync(destination)
    return receipt


def verify(release, environment=False, allow_incomplete=False):
    release = Path(release).resolve()
    if (release / '.incomplete').exists() and not allow_incomplete:
        raise ValueError('release preparation is incomplete')
    receipt = json.loads((release / 'release.json').read_text())
    files = receipt.get('files', {})
    if receipt.get('version') != 1 or receipt.get('id') != release_id(files):
        raise ValueError('invalid release receipt')
    actual = {p.relative_to(release).as_posix() for p in code_files(release)}
    if actual != set(files):
        raise ValueError('release file set changed')
    for rel, expected in files.items():
        path = release / rel
        if path.is_symlink() or '..' in Path(rel).parts or not path.is_file() or digest(path) != expected:
            raise ValueError('release content changed')
    if environment:
        venv = release / '.venv'
        if venv.is_symlink() or not (venv / 'bin/python').is_file():
            raise ValueError('provision a dedicated release environment first')
        script = ('import sys,json,importlib.metadata as m; '
                  'assert sys.version_info[:2]==(3,12); '
                  'print(json.dumps({d.metadata["Name"].lower().replace("_","-"):d.version for d in m.distributions()}))')
        result = subprocess.run([str(venv / 'bin/python'), '-I', '-c', script], capture_output=True,
                                text=True, timeout=15, check=True)
        installed = json.loads(result.stdout)
        for line in (release / 'requirements-dashboard.lock').read_text().splitlines():
            if not line or line.startswith('#'):
                continue
            name, version = line.split('==')
            if installed.get(name.lower().replace('_', '-')) != version:
                raise ValueError('release environment does not match its lock')
    return receipt


def provision(release, uv='uv'):
    release = Path(release).resolve()
    verify(release)
    if (release / '.venv').exists():
        raise ValueError('a release environment is never upgraded in place')
    subprocess.run([uv, 'venv', '--python', '3.12', str(release / '.venv')], cwd=release, check=True)
    subprocess.run([uv, 'pip', 'sync', '--python', str(release / '.venv/bin/python'),
                    str(release / 'requirements-dashboard.lock')], cwd=release, check=True)
    return verify(release, environment=True)


def safe_path(path):
    path = str(Path(path).absolute())
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', path):
        raise ValueError('service paths must be absolute and contain no whitespace or shell/systemd expansions')
    return path


def render_units(release, root, state, port, targets=(), cert=None, key=None):
    release, root, state = map(safe_path, (release, root, state))
    if not 1024 <= port <= 65535:
        raise ValueError('invalid unprivileged dashboard port')
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', target) for target in targets):
        raise ValueError('invalid probe target ID')
    tls = validate_tls(cert, key)
    tls_args = (' --tls-cert ' + safe_path(cert) + ' --tls-key ' + safe_path(key)) if tls else ''
    prefix = (f'{MARKER}\n[Unit]\nDescription=agentiac private management\n'
              'After=network-online.target\nStartLimitIntervalSec=60\nStartLimitBurst=3\n\n[Service]\n'
              f'Type=simple\nWorkingDirectory={release}\nEnvironment=PYTHONPATH={release}/src\n'
              f'Environment=AGENTIAC_MANAGEMENT_ROOT={root}\nEnvironment=AGENTIAC_MANAGEMENT_STATE={state}\n'
              'Environment=PYTHONDONTWRITEBYTECODE=1\nUMask=0077\nNoNewPrivileges=yes\n'
              f'ProtectSystem=strict\nProtectHome=read-only\nReadWritePaths={state}\n'
              'MemoryMax=512M\nCPUQuota=100%\nKillMode=control-group\nTimeoutStopSec=10\n')
    api = (prefix + f'ExecStart={release}/.venv/bin/python -m agentiac.management.main --host 127.0.0.1 --port {port}'
           + tls_args + '\nRestart=on-failure\nRestartSec=5\nPrivateTmp=yes\n\n[Install]\nWantedBy=default.target\n')
    worker = None
    if targets:
        worker = (prefix + f'ExecStart={release}/.venv/bin/python -m agentiac.operations.worker --serve '
                  + ' '.join('--target ' + target for target in sorted(set(targets)))
                  + '\nRestart=no\nKillSignal=SIGINT\n\n[Install]\nWantedBy=default.target\n')
    return dict(zip(units(), (api, worker)))


def check_state(release, state):
    """Ask the target release itself whether it supports the current DB schema, read-only."""
    release, state = Path(release).resolve(), Path(state).resolve()
    script = ('import sys;sys.path.insert(0,sys.argv[1]);'
              'from pathlib import Path;'
              'from agentiac.management import snapshots;'
              'from agentiac.management.paths import require_ready;'
              'state=Path(sys.argv[2]);require_ready(state);'
              '[snapshots.validate_database(state/name,signature) '
              'for name,signature in snapshots.expected_signatures().items()]')
    result = subprocess.run([str(release / '.venv/bin/python'), '-I', '-c', script,
                             str(release / 'src'), str(state)], cwd=release,
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise ValueError('target release cannot read the current management schema; no downgrade will be attempted')
