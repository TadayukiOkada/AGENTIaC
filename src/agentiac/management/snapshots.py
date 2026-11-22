"""Private, offline two-DB snapshots. No set-B upload, extraction or in-place restore."""
from contextlib import closing, nullcontext
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

from .lifecycle import StateLock
from .paths import ROOT

DATABASES = ('session.sqlite', 'operations.sqlite')
SCOPE = 'management-databases-v1'
MAX_DATABASE_BYTES = 1024 ** 3


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def private_file(path):
    path = Path(path)
    if (path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o077):
        raise ValueError('snapshot/state files must be regular, owner-only files')
    if path.stat().st_size > MAX_DATABASE_BYTES:
        raise ValueError('management database exceeds the supported snapshot bound')


def connect_readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    conn.execute('PRAGMA trusted_schema=OFF')
    return conn


def signature(path):
    with closing(connect_readonly(path)) as conn:
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('database integrity check failed')
        objects = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        if any(kind not in {'table', 'index'} or (sql and 'CREATE VIRTUAL' in sql.upper()) for kind, _, sql in objects):
            raise ValueError('unsupported database schema objects')
        return [(kind, name, ' '.join((sql or '').split())) for kind, name, sql in objects]


def expected_signatures():
    from .session import SessionStore
    from agentiac.operations.ledger import Ledger
    with tempfile.TemporaryDirectory(prefix='management-schema-') as folder:
        base = Path(folder)
        SessionStore(base / 'session.sqlite')
        ledger = Ledger(ROOT, path=base / 'operations.sqlite', log_dir=base / 'jobs')
        ledger.close()
        return {name: signature(base / name) for name in DATABASES}


def validate_database(path, expected):
    private_file(path)
    if signature(path) != expected:
        raise ValueError('unsupported management schema; no automatic downgrade or migration')


def fsync(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_manifest(folder, manifest):
    path = folder / 'manifest.json'
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def verify(folder, allow_incomplete=False):
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir() or folder.stat().st_uid != os.getuid() or folder.stat().st_mode & 0o077:
        raise ValueError('snapshot directory must be private and owned')
    names = {entry.name for entry in folder.iterdir()}
    allowed = {*DATABASES, 'manifest.json'} | ({'.incomplete'} if allow_incomplete else set())
    if names != allowed:
        raise ValueError('incomplete snapshot or unexpected files')
    private_file(folder / 'manifest.json')
    manifest = json.loads((folder / 'manifest.json').read_text())
    if (manifest.get('scope') != SCOPE or manifest.get('version') != 1
            or set(manifest.get('databases', {})) != set(DATABASES)):
        raise ValueError('unsupported snapshot manifest')
    expected = expected_signatures()
    for name in DATABASES:
        path = folder / name
        validate_database(path, expected[name])
        entry = manifest['databases'][name]
        if entry.get('bytes') != path.stat().st_size or entry.get('sha256') != digest(path):
            raise ValueError('snapshot checksum/size mismatch')
    # Never import login cookies, authentication throttles or stale worker readiness.
    with closing(connect_readonly(folder / 'session.sqlite')) as conn:
        if conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] or conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]:
            raise ValueError('snapshot contains live session/throttle state')
    with closing(connect_readonly(folder / 'operations.sqlite')) as conn:
        if conn.execute('SELECT COUNT(*) FROM probe_workers').fetchone()[0]:
            raise ValueError('snapshot contains worker readiness')
    return manifest


def backup(state, destination, locked=False, root=ROOT):
    state, destination = Path(state), Path(destination)
    protected = (Path(root) / 'artifacts').resolve()
    if destination.resolve() == protected or protected in destination.resolve().parents:
        raise ValueError('private snapshots cannot be stored under set-B artifacts')
    from .paths import require_complete
    with nullcontext() if locked else StateLock(state, exclusive=True):
        require_complete(state)
        allowed = {*DATABASES, 'lifecycle.lock', 'probe-worker.lock', 'jobs', '.credential-review-required'}
        allowed |= {name + suffix for name in DATABASES for suffix in ('-wal', '-shm', '-journal')}
        unsupported = [p.name for p in state.iterdir() if p.name not in allowed]
        jobs = state / 'jobs'
        if unsupported or (jobs.exists() and (jobs.is_symlink() or not jobs.is_dir() or any(jobs.iterdir()))):
            raise ValueError('unsupported state files; snapshot scope cannot omit them')
        if state.resolve() == destination.resolve() or state.resolve() in destination.resolve().parents:
            raise ValueError('snapshot destination must be outside management state')
        expected = expected_signatures()
        for name in DATABASES:
            validate_database(state / name, expected[name])
        destination.mkdir(mode=0o700)  # Existing destinations are never overwritten.
        (destination / '.incomplete').touch(mode=0o600)
        try:
            for name in DATABASES:
                target = destination / name
                target.touch(mode=0o600)
                with closing(connect_readonly(state / name)) as source, closing(sqlite3.connect(target)) as target_conn:
                    source.backup(target_conn)
                    target_conn.execute('PRAGMA secure_delete=ON')
                    if name == 'session.sqlite':
                        target_conn.execute('DELETE FROM sessions')
                        target_conn.execute('DELETE FROM attempts')
                    else:
                        target_conn.execute('DELETE FROM probe_workers')
                    target_conn.commit()
                    target_conn.execute('PRAGMA journal_mode=DELETE')
                    target_conn.execute('VACUUM')
                fsync(target)
            manifest = {'version': 1, 'scope': SCOPE, 'id': uuid.uuid4().hex,
                        'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        'databases': {name: {'bytes': (destination / name).stat().st_size,
                                             'sha256': digest(destination / name)} for name in DATABASES}}
            write_manifest(destination, manifest)
            verify(destination, allow_incomplete=True)
            (destination / '.incomplete').unlink()
            fsync(destination)
            fsync(destination.parent)
            return manifest
        except Exception:
            # Preserve a private, visibly incomplete artifact for inspection. Never publish it.
            raise


def restore(snapshot, destination, root=ROOT):
    snapshot, destination = Path(snapshot), Path(destination)
    protected = (Path(root) / 'artifacts').resolve()
    if destination.resolve() == protected or protected in destination.resolve().parents:
        raise ValueError('private state cannot be restored under set-B artifacts')
    verify(snapshot)
    destination.mkdir(mode=0o700)  # Restore only to a new directory; never replace live state.
    (destination / '.incomplete').touch(mode=0o600)
    with StateLock(destination, exclusive=True):
        expected = expected_signatures()
        for name in DATABASES:
            target = destination / name
            target.touch(mode=0o600)
            with closing(connect_readonly(snapshot / name)) as source, closing(sqlite3.connect(target)) as target_conn:
                source.backup(target_conn)
                target_conn.execute('PRAGMA journal_mode=DELETE')
            validate_database(target, expected[name])
            if digest(target) != digest(snapshot / name):
                # SQLite backup may rewrite header counters; compare complete logical contents instead.
                with closing(connect_readonly(target)) as restored, closing(connect_readonly(snapshot / name)) as original:
                    def logical_hash(connection):
                        result = hashlib.sha256()
                        for line in connection.iterdump():
                            result.update(line.encode())
                        return result.digest()
                    if logical_hash(restored) != logical_hash(original):
                        raise ValueError('restored database does not match its snapshot')
            fsync(target)
        (destination / '.credential-review-required').touch(mode=0o600)
        fsync(destination / '.credential-review-required')
        (destination / '.incomplete').unlink()
        fsync(destination)
        fsync(destination.parent)
    return {'scope': SCOPE, 'restored': True, 'sessions_revoked': True, 'worker_readiness_restored': False, 'owner_review_required': True}


def validate_startup(state):
    """Serving an initialized instance must never recreate a lost database implicitly."""
    from .paths import require_ready
    state = Path(state)
    require_ready(state)
    for name, expected in expected_signatures().items():
        validate_database(state / name, expected)
    with closing(connect_readonly(state / 'session.sqlite')) as conn:
        row = conn.execute("SELECT password_hash FROM credentials WHERE actor='owner'").fetchone()
    if not row or not re.fullmatch(r'scrypt\$[0-9a-f]{32}\$[0-9a-f]{128}', row[0]):
        raise ValueError('initialize/recover the owner before starting the dashboard')
