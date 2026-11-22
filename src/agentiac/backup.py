"""What a control backup covers, and what was last backed up (IP-15, T6.7).

Set **B** covers the evaluation records, kept whole, and excludes anything that a lock or a
build command reproduces. The destination is the rclone remote and path the site declares as
`backup_destination` in `all.vars` of its inventory (site-separation.md D8.2). `secrets/` is not in the
set: worker and gateway keys are reissued with `bootstrap-secrets.py --rotate`, and `secrets/chatgpt/auth.json`
needs a person either way (ADR 0005), so a copy of it on a third-party service would only widen the blast
radius. Nothing here is encrypted; `docs/operations.md` says what changes if that stops being acceptable.

**The set is written as exclusions, not as a list of folders.** A new directory under `artifacts/` is
therefore backed up by default and only leaves the set when someone names it as reproducible. That is the
safe direction: forgetting a directory here silently drops evidence.

`suite/runs` holds `trace.jsonl` and the attempt workspace trees, which are the only copy
of what an attempt changed - `state.sqlite` keeps a `git diff --stat` summary and nothing more. Those trees
keep their `.git` on purpose: the restore test reproduces patch hashes with `git diff HEAD` inside them.
"""
import datetime
import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from agentiac import declared

SCHEMA_VERSION = 1
SET = 'B'
PREFIX = 'control-backup-'
INTERVAL_DAYS = 7                 # the weekly timer
OLD_AFTER_DAYS = 21               # three missed runs is a failure, not a note


def destination(root=None):
    """Where the archive goes: the site's declared `backup_destination` (D8.2)."""
    return declared.setting('backup_destination', root)


def __getattr__(name):
    """`backup.DESTINATION`, the instance root's destination, resolved on first use rather than at import (PEP 562)."""
    if name == 'DESTINATION':
        return destination()
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
VERIFY_AFTER_DAYS = 45          # the monthly restore test may be a month late, not two
STAGING = 'artifacts/backup'
RECORD = 'artifacts/backup.json'

# Everything here comes back from a lock or a build command, so a copy of it is only spent space.
REPRODUCIBLE = ('frontends/', 'uv-cache/', 'corpora/', 'backends/', 'tmp/', 'backup/')
# A view, not a record: the documented restore rebuilds it with `agentiac evidence import`.
VIEWS = ('evidence/evidence.sqlite',)
# Legacy dashboard prototypes stored credentials/cookies here; never export them in set B.
PRIVATE = ('management/',)
# The live database and its side files are replaced by a snapshot taken with sqlite3's own backup API.
DATABASE = 'suite/state.sqlite'
SIDES = ('suite/state.sqlite-wal', 'suite/state.sqlite-shm')
ZSTD = ('zstd', '-1', '-T4', '-q')   # -q: a timer's journal gets no progress bar
FAILURES = (OSError, ValueError, LookupError, TypeError, sqlite3.Error, subprocess.SubprocessError)


def _rel(base, path):
    return os.path.relpath(path, base).replace(os.sep, '/')


def excluded(rel):
    """`rel` is a path inside `artifacts/`, or a directory prefix ending in `/`."""
    return (any(rel.startswith(kind) for kind in (*REPRODUCIBLE, *PRIVATE)) or rel in VIEWS or rel in SIDES
            or rel.rstrip('/') == 'backup' or rel.rstrip('/') == 'tmp')


def entries(root):
    """Every path under `artifacts/` the record covers, relative to `artifacts/`, sorted."""
    base = Path(root) / 'artifacts'
    if not base.is_dir():
        raise ValueError(f'{base} is not a directory')
    found = []
    for dirpath, dirs, files in os.walk(base):
        rel_dir = _rel(base, dirpath)
        keep = []
        for name in dirs:
            rel = f'{rel_dir}/{name}/' if rel_dir != '.' else f'{name}/'
            if not excluded(rel):
                keep.append(name)
        dirs[:] = keep
        for name in files:
            rel = f'{rel_dir}/{name}' if rel_dir != '.' else name
            if not excluded(rel):
                found.append(rel)
    if not found:
        raise ValueError(f'{base} holds nothing to back up')
    return sorted(found)


def plan(root):
    """What the set covers today: counts and bytes, per top-level directory."""
    found = entries(root)
    base = Path(root) / 'artifacts'
    by_top, total = {}, 0
    for rel in found:
        try:
            size = (base / rel).stat().st_size
        except OSError:
            size = 0
        total += size
        top = rel.split('/', 1)[0]
        by_top[top] = by_top.get(top, [0, 0])
        by_top[top][0] += 1
        by_top[top][1] += size
    return {'schema_version': SCHEMA_VERSION, 'set': SET, 'files': len(found), 'bytes': total,
            'reproducible_excluded': list(REPRODUCIBLE), 'private_excluded': list(PRIVATE),
            'top': {name: {'files': counts[0], 'bytes': counts[1]} for name, counts in sorted(by_top.items())}}


def archive_name(when=None):
    when = when or datetime.datetime.now(datetime.timezone.utc)
    return f'{PREFIX}{when.strftime("%Y%m%dT%H%M%SZ")}.tar.zst'


def snapshot_database(root, dest):
    """A consistent copy of the attempt ledger. Copying the live file can capture a half-written page."""
    src = Path(root) / 'artifacts' / DATABASE
    if not src.exists():
        return False
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as source, sqlite3.connect(dest) as target:
        source.backup(target)
    return True


def build(root, name=None, when=None, progress=None):
    """Write `artifacts/backup/<name>.tar.zst` from the set. Returns the record fields for it."""
    name = name or archive_name(when)
    staging = Path(root) / STAGING
    staging.mkdir(parents=True, exist_ok=True)
    os.chmod(staging, 0o700)
    out = staging / name
    snapshot = staging / f'{name}.state.sqlite'
    taken = snapshot_database(root, snapshot)
    found = entries(root)
    base = Path(root) / 'artifacts'
    zstd = subprocess.Popen([*ZSTD, '-o', str(out)], stdin=subprocess.PIPE)
    added = 0
    try:
        with tarfile.open(fileobj=zstd.stdin, mode='w|') as archive:
            for rel in found:
                source = base / rel
                if rel == DATABASE:
                    if not taken:
                        continue
                    archive.add(snapshot, arcname=f'artifacts/{DATABASE}', recursive=False)
                    added += 1
                    continue
                try:
                    archive.add(source, arcname=f'artifacts/{rel}', recursive=False)
                except FileNotFoundError:  # a file that vanished mid-run: the ledger keeps its own copy
                    continue
                added += 1
            if taken and DATABASE not in found:
                archive.add(snapshot, arcname=f'artifacts/{DATABASE}', recursive=False)
                added += 1
    finally:
        if zstd.stdin:
            with contextlib.suppress(BrokenPipeError):
                zstd.stdin.close()
        returncode = zstd.wait()
        snapshot.unlink(missing_ok=True)
    if returncode:
        out.unlink(missing_ok=True)
        raise subprocess.CalledProcessError(returncode, zstd.args)
    with out.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    if progress:
        progress(f'{name}: {added} files in archive, {out.stat().st_size / 1073741824:.2f} GiB compressed')
    return {'archive': name, 'files': added, 'bytes': out.stat().st_size, 'sha256': digest}


def stamp(name):
    """The UTC time an archive name carries, or None when the name is not ours."""
    if not name.startswith(PREFIX) or not name.endswith('.tar.zst'):
        return None
    try:
        return datetime.datetime.strptime(name[len(PREFIX):-len('.tar.zst')], '%Y%m%dT%H%M%SZ')
    except ValueError:
        return None


def retention(names, keep=4, months=6, now=None):
    """Which remote archives to keep: the newest `keep`, plus the earliest one of each of the last `months`
    months. Everything else is deleted. It never keeps fewer than `keep` and never deletes a name it cannot
    read a time from, because that name is not one this script wrote."""
    mine, foreign = [], []
    for name in names:
        (mine if stamp(name) else foreign).append(name)
    mine.sort(key=stamp, reverse=True)
    keep_names = mine[:max(0, keep)]
    now = now or datetime.datetime.now(datetime.timezone.utc)
    seen_months = set()
    for name in reversed(mine):
        when = stamp(name)
        if (now.year - when.year) * 12 + (now.month - when.month) > months:
            continue
        key = (when.year, when.month)
        if key not in seen_months:
            seen_months.add(key)
            keep_names.append(name)
    keep_set = set(keep_names) | set(foreign)
    return sorted(keep_set), sorted(set(names) - keep_set)


def open_archive(path):
    """Open an archive for reading. Python's tarfile has no zstd, so the decompression is a pipe."""
    if str(path).endswith('.zst'):
        child = subprocess.Popen([ZSTD[0], '-d', '-c', '-q', str(path)], stdout=subprocess.PIPE)
        try:
            return tarfile.open(fileobj=child.stdout, mode='r|'), child
        except BaseException:
            child.stdout.close()
            child.terminate()
            child.wait()
            raise
    return tarfile.open(path, mode='r'), None


@contextlib.contextmanager
def reading(path):
    """Read and validate the entire compressed stream, closing the pipe on every path."""
    archive, child = open_archive(path)
    try:
        yield archive
        if child:
            # tar can stop at its end marker before zstd checks the trailer.
            while child.stdout.read(1 << 20):
                pass
            if child.wait():
                raise ValueError('backup decompressor exited unsuccessfully')
    finally:
        archive.close()
        if child:
            child.stdout.close()
            if child.poll() is None:
                child.terminate()
            child.wait()


def unsafe(member):
    """Why this member must not be extracted, or None when it may.  What a backup holds is files and
    directories under `artifacts/`, so anything else is not a backup - it is a way to write elsewhere."""
    name = PurePosixPath(member.name)
    if name.is_absolute() or len(name.parts) < 2 or name.parts[0] != 'artifacts':
        return f'{member.name!r} is not a path under artifacts/'
    if '.' in name.parts or '..' in name.parts:
        return f'{member.name!r} reaches outside artifacts/'
    if member.issym() or member.islnk():
        link = PurePosixPath(member.linkname)
        if link.is_absolute() or '..' in link.parts:
            return f'{member.name!r} links to {member.linkname!r}, which is outside the tree'
    if member.islnk() or member.issym() or member.isfile() or member.isdir():
        return None
    return f'{member.name!r} is neither a file, a directory, nor a link inside the tree'


def extract(archive, into):
    """Extract member by member, refusing the first one that does not belong.

    Require tarfile's data filter (also backported to maintained Python 3.10 releases), plus the
    narrower artifacts boundary. Restores must use a private directory with no concurrent writers."""
    if not hasattr(tarfile, 'data_filter'):
        raise ValueError('safe extraction needs tarfile.data_filter; use a security-patched Python or Python 3.12+')
    base = Path(into).resolve()
    boundary = base / 'artifacts'
    written = 0
    while True:
        member = archive.next()
        if member is None:
            break
        reason = unsafe(member)
        if reason:
            raise ValueError(f'the archive holds something a backup does not: {reason}')
        destinations = [base / member.name]
        if member.islnk():
            destinations.append(base / member.linkname)
        elif member.issym():
            destinations.append((base / member.name).parent / member.linkname)
        if any(not path.resolve().is_relative_to(boundary) for path in destinations):
            raise ValueError(f'{member.name!r} reaches outside artifacts/ through a path or link')
        try:
            archive.extract(member, path=into, filter='data')
        except tarfile.FilterError as error:
            raise ValueError(f'unsafe backup member: {error}') from error
        written += 1
    return written


def record_path(root):
    return Path(root) / RECORD


def save(root, record):
    path = record_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record.setdefault('schema_version', SCHEMA_VERSION)
    record.setdefault('set', SET)
    record['at'] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
    os.chmod(path, 0o600)
    return path


def load(root):
    path = record_path(root)
    if not path.exists():
        return None
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return saved if isinstance(saved, dict) else None


def age_days(record, now=None):
    at = record.get('at')
    if not at:
        return None
    then = datetime.datetime.fromisoformat(at)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (now - then).total_seconds() / 86400)


def line(record, now=None):
    """One line for `doctor`. It says what is stored, where, and how old the record is."""
    days = age_days(record, now)
    size = record.get('bytes') or 0
    age = 'no time recorded' if days is None else f'{days:.1f} days ago'
    verified = record.get('verify') or {}
    check = verified.get('at') and 'verified' if verified.get('sha256_match') else 'not verified'
    return (f"set {record.get('set', '?')}: {record.get('files', '?')} files, {size / 1073741824:.2f} GiB "
            f"compressed to {record.get('archive', '?')} at {record.get('destination', '?')}, {age}, {check}")
