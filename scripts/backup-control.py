#!/usr/bin/env python3
"""`agentiac ops backup ACTION` (IP-15, T6.7): copy control's evaluation records off the host.

plan    what the set covers today. Reads nothing off-host, writes nothing.
run     build the archive, upload it, check the remote size, prune to the retention rule, record it.
verify  download the last archive back, compare its sha256, and re-run the aggregation it must reproduce.
prune  apply the retention rule to the remote only.
status  print what was last backed up.

The set is `agentiac.backup`: the records, and not the installs, caches or built backends that a lock or
a build command reproduces. Upload goes to the declared `backup_destination` (D8.2), whose rclone remote must
already be configured for the service account.
"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
from agentiac import backup, declared  # noqa: E402

RCLONE_TIMEOUT_S = 3600
# The run the Phase 10 judgement rests on. A restore that cannot reproduce these numbers did not restore
# what the decision used.
SUITE_CONFIG = '3c8298b3794891dd'
FRONTS = ('pi 1.5.0', 'pi 1.3.0')


def rclone(*args, timeout=RCLONE_TIMEOUT_S):
    return subprocess.run(['rclone', *args], capture_output=True, text=True, timeout=timeout)


def staging(root):
    path = Path(root) / backup.STAGING
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


@contextlib.contextmanager
def held():
    """One job at a time. The weekly archive and the monthly restore test share `artifacts/backup`."""
    handle = open(staging(ROOT) / '.lock', 'a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SystemExit('another backup job holds the staging directory; nothing was started')
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def remote_size(destination, name):
    done = rclone('lsjson', f'{destination}/{name}', timeout=120)
    if done.returncode != 0:
        return None, (done.stderr or done.stdout).strip()
    entries = json.loads(done.stdout or '[]')
    found = [item for item in entries if item.get('Name') == name]      # lsjson names its keys capitalized
    return (found[0].get('Size') if found else None), None


def remote_names(destination):
    done = rclone('lsjson', destination, '--files-only', timeout=120)
    if done.returncode != 0:
        raise SystemExit(f'rclone could not list {destination}: {(done.stderr or done.stdout).strip()}')
    return [item['Name'] for item in json.loads(done.stdout or '[]') if item.get('Name')]


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 22), b''):
            digest.update(block)
    return digest.hexdigest()


def do_plan(destination):
    found = backup.plan(ROOT)
    print(f"set {found['set']}: {found['files']} files, {found['bytes'] / 1073741824:.2f} GiB raw, "
          f"to {destination}")
    print(f"  excluded because a lock or a build reproduces it: {', '.join(found['reproducible_excluded'])}")
    for name, item in found['top'].items():
        print(f"  {name:<22} {item['files']:>7} files {item['bytes'] / 1048576:>9.1f} MiB")
    return 0


def prune_remote(destination, names):
    """Return actual deletions and failed names; an attempted deletion is not a success."""
    failed = []
    for name in names:
        if rclone('deletefile', f'{destination}/{name}', timeout=120).returncode:
            failed.append(name)
    if failed:
        print(f'pruning failed for {len(failed)} archives: {", ".join(failed)}')
    return len(names) - len(failed), failed


def do_run(destination, keep, dry_run):
    where = staging(ROOT)
    built = backup.build(ROOT)
    print(f"built {built['archive']}: {built['files']} files, {built['bytes'] / 1073741824:.2f} GiB, "
          f"sha256 {built['sha256'][:16]}")
    if dry_run:
        print('dry run: nothing left this host')
        return 0
    done = rclone('copyto', str(where / built['archive']), f'{destination}/{built["archive"]}',
                  '--checksum', '--retries', '3', '--timeout', '10m')
    if done.returncode != 0:
        print(f"upload failed: {(done.stderr or done.stdout).strip()[:400]}")
        return 1
    size, problem = remote_size(destination, built['archive'])
    if problem or size != built['bytes']:
        print(f"the remote copy does not match: remote {size}, local {built['bytes']} {problem or ''}")
        return 1
    names, gone = backup.retention(remote_names(destination), keep=keep)
    pruned, failed = prune_remote(destination, gone)
    (where / built['archive']).unlink(missing_ok=True)
    record = {'set': backup.SET, 'destination': destination, 'archive': built['archive'],
              'files': built['files'], 'bytes': built['bytes'], 'sha256': built['sha256'],
              'interval_days': backup.INTERVAL_DAYS, 'kept': len(names) + len(failed),
              'pruned': pruned, 'prune_failed': failed}
    path = backup.save(ROOT, record)
    # The record has to travel with the archive.  A rebuilt control host has no local record to read, so the
    # name and sha256 it verifies against come from this copy next to the archives.
    published = rclone('copyto', str(path), f'{destination}/backup.json', '--checksum', '--retries', '3',
                       '--timeout', '120s')
    if published.returncode != 0:
        print(f"the record is on this host but not on the remote: {(published.stderr or published.stdout).strip()[:200]}")
        return 1
    print(f"stored and recorded. remote keeps {len(names) + len(failed)} archives, {pruned} pruned. record: {path} and "
          f"{destination}/backup.json")
    return 1 if failed else 0


def patch_hashes(root, config):
    """The patch each attempt finally left, hashed from its own workspace tree.

    `state.sqlite` keeps only a `git diff --stat` summary, so the trees are the record. Hashing them the same
    way in the restored copy and in the live tree is what proves the copy restored what the judgement read.
    """
    db = sqlite3.connect(f'file:{Path(root) / "artifacts/suite/state.sqlite"}?mode=ro', uri=True)
    found = {}
    for case, spec, result, number in db.execute("""SELECT c.id, c.spec, a.result, a.number FROM cases c
                                           JOIN attempts a ON a.case_id = c.id AND a.number = c.attempts"""):
        spec, result = json.loads(spec), json.loads(result or '{}')
        if (result.get('provenance') or {}).get('config_id') != config:
            continue
        tree = Path(root) / 'artifacts/suite/runs' / case / f'attempt-{number}' / 'workspace'
        if not (tree / '.git').exists():
            found[case] = None
            continue
        git = ['git', '-c', 'safe.directory=*', '-c', 'core.quotepath=false', '-C', str(tree)]
        diff = subprocess.run([*git, 'diff', 'HEAD', '--no-color'], capture_output=True, text=True,
                              timeout=180).stdout
        untracked = ''
        for line in subprocess.run([*git, 'status', '--porcelain'], capture_output=True, text=True,
                                   timeout=180).stdout.splitlines():
            if line.startswith('?? '):
                path = tree / line[3:]
                if path.is_file():
                    untracked += line[3:] + ':' + sha256(path) + '\n'
        found[case] = hashlib.sha256((diff + '\n#\n' + untracked).encode()).hexdigest()
    return found


def content_test(root):
    """Re-run the aggregation the Phase 10 judgement used, from the restored tree only."""
    script = root / 'artifacts' / 't9-6' / 'compare.py'
    if not script.exists():
        return False, 'artifacts/t9-6/compare.py is not in the restored tree'
    done = subprocess.run([sys.executable, str(script), SUITE_CONFIG, *FRONTS], cwd=root,
                          capture_output=True, text=True, timeout=600,
                          env={**os.environ, 'PYTHONPATH': str(CODE / 'src')})
    if done.returncode != 0:
        return False, (done.stderr or done.stdout).strip()[:300]
    out = done.stdout
    return all(f'{front} ' in out for front in FRONTS), out.strip()[:500]


def do_verify(destination, keep):
    record = backup.load(ROOT)
    if not record or not record.get('archive'):
        print('no backup record to verify')
        return 1
    where = staging(ROOT) / 'verify'
    where.mkdir(parents=True, exist_ok=True)
    name = record['archive']
    done = rclone('copyto', f'{destination}/{name}', str(where / name), '--retries', '3', '--timeout', '10m')
    if done.returncode != 0:
        print(f"download failed: {(done.stderr or done.stdout).strip()[:300]}")
        return 1
    digest = sha256(where / name)
    matched = digest == record.get('sha256')
    print(f'{name}: downloaded {record["bytes"] / 1073741824:.2f} GiB, sha256 '
          f'{"matches" if matched else f"does not match ({digest[:16]})"}')
    if not matched:
        # Never extract or execute compare.py from bytes that failed the recorded digest.
        (where / name).unlink(missing_ok=True)
        return 1
    extract = where / 'extract'
    if extract.exists():
        subprocess.run(['rm', '-rf', str(extract)], check=False)
    extract.mkdir(parents=True)
    try:
        with backup.reading(where / name) as archive:
            backup.extract(archive, extract)
    except ValueError as error:
        print(f'verify refused: {error}')
        return 1
    ok, detail = content_test(extract)
    print(f'content test ({"pass" if ok else "fail"}): {detail}')
    restored, live = patch_hashes(extract, SUITE_CONFIG), patch_hashes(ROOT, SUITE_CONFIG)
    same = sorted(restored.values()) == sorted(live.values())
    print(f'patch test ({"pass" if same else "fail"}): {len(restored)} attempts restored from the archive, '
          f'{len({value for value in restored.values() if value})} distinct patches, '
          f'{"every one identical to the live tree" if same else "the restored trees do not match the live ones"}')
    ok = ok and same
    (where / name).unlink(missing_ok=True)
    record['verify'] = {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                        'sha256_match': matched, 'content': 'pass' if ok else 'fail',
                        'detail': detail if not ok else 'compare.py reproduced both front rows'}
    backup.save(ROOT, record)
    if not matched or not ok:
        return 1
    subprocess.run(['rm', '-rf', str(extract)], check=False)
    return 0


def do_prune(destination, keep):
    names, gone = backup.retention(remote_names(destination), keep=keep)
    pruned, failed = prune_remote(destination, gone)
    print(f'{destination}: keeping {len(names)}, removed {pruned}')
    return 1 if failed else 0


def do_status(destination):
    record = backup.load(ROOT)
    if not record:
        print(f'no backup has run yet. The set is {backup.SET}; the timer is {declared.resource("backup.timer", ROOT)}; '
              f'the destination is {destination}')
        return 3
    print(backup.line(record))
    if record.get('verify'):
        print(f"  last verified {record['verify'].get('at')}: sha256 "
              f"{'matched' if record['verify'].get('sha256_match') else 'did not match'}, content "
              f"{record['verify'].get('content')}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('plan', 'run', 'verify', 'prune', 'status'))
    parser.add_argument('--destination', default=backup.DESTINATION)
    parser.add_argument('--keep', type=int, default=4, help='weekly archives always kept, newest first')
    parser.add_argument('--dry-run', action='store_true', help='build the archive and stop before uploading')
    args = parser.parse_args(argv)

    if args.action == 'plan':
        sys.exit(do_plan(args.destination))
    if args.action == 'status':
        sys.exit(do_status(args.destination))
    with held():
        if args.action == 'run':
            sys.exit(do_run(args.destination, args.keep, args.dry_run))
        if args.action == 'verify':
            sys.exit(do_verify(args.destination, args.keep))
        sys.exit(do_prune(args.destination, args.keep))


if __name__ == '__main__':
    main()
