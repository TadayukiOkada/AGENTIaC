#!/usr/bin/env python3
"""Restore control's evaluation records from the control backup (IP-15, T6.7).

The mechanical half is automated: pick the archive, download it, check its sha256 against the backup record, and
extract it into a repo checkout that has no records of its own.  The half that needs a human is printed and not
attempted: `make install`, Tailscale enrolment and host-key approval, `agentiac chatgpt login`, secret rotation
and the claims.  Those read keys and touch a tailnet, and a script that guesses them fails on a machine that is
already broken (docs/operations.md, "Restoring control").

Nothing here writes into the tree the backup was taken from.  `--into` is refused when it is that tree, when it is
under it, or when it already holds records.

  scripts/restore-control.py --into PATH                       # PATH: a fresh checkout of the site
  scripts/restore-control.py --dry-run --into PATH            # what would happen, changing nothing
  scripts/restore-control.py --archive NAME --sha256 HEX --into PATH   # an archive the record does not name
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()

from agentiac import backup  # noqa: E402
from agentiac.errors import command as cli_name  # noqa: E402

RCLONE_TIMEOUT_S = 3600

# The steps this script does not run.  Each one needs a key, a tailnet, or a decision about a host.
HUMAN_STEPS = (
    ('make install', 'the unit files, the wrapper and the venv on the new control host'),
    ('tailscale up', 'enrolment, and the host-key approval at https://login.tailscale.com/admin/machines'),
    ('scripts/bootstrap-secrets.py --rotate', 'secrets are not in the archive, so a rebuilt control has none until '
                                              'this runs'),
    (f'{cli_name()} ops deploy', 're-render the units and the gateway; it reaches the workers over the tailnet and '
                               'reads the secrets, so it comes after the two steps above'),
    (f'{cli_name()} chatgpt login', 'the ChatGPT sign-in for cloud-gpt-sol, then ops deploy --only chatgpt (a browser '
                                  'and a human)'),
    (f'{cli_name()} claim HOST', f'the hosts the route needs, then {cli_name()} route TARGET'),
    (f'{cli_name()} check', 'last, and the proof the restored host serves'),
)


def refuse(reason):
    raise SystemExit(f'restore refused: {reason}')


def target(ROOT, into, holds_the_records=False):
    """The checkout to restore into, or a refusal.

    The tree the backup was taken from is not identified by a path - on a rebuilt host the restore runs from
    the very checkout it restores into, and that is the point.  It is identified by holding the records and the
    record of its own backup: that is the tree this command would overwrite."""
    target = Path(into).expanduser().resolve()
    live = (ROOT / 'artifacts').resolve()
    if holds_the_records and (target == (ROOT / 'artifacts') or target == ROOT):
        refuse(f'{target} is the tree the backup was taken from')
    if holds_the_records and (live in target.parents or target in live.parents):
        refuse(f'{target} sits inside the live artifacts tree it would be restoring from')
    if not (target / 'src' / 'agentiac').is_dir():
        refuse(f'{target} is not an agentiac checkout (no src/agentiac); clone the repository there first')
    records = target / 'artifacts'
    if records.exists() and any(records.iterdir()):
        refuse(f'{target}/artifacts already holds records; restore into an empty checkout')
    return target


def remote_record(destination, runner=subprocess.run):
    """The record the backup run published next to the archives.  A rebuilt host has no local one."""
    done = runner(['rclone', 'cat', f'{destination}/backup.json'], capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        return None
    try:
        record = json.loads(done.stdout)
    except ValueError:
        return None
    return record if isinstance(record, dict) and record.get('archive') and record.get('sha256') else None


def chosen(record, archive, sha256_expected):
    """(name, sha256) to restore, or a refusal.  An unrecorded archive needs its digest from the caller."""
    if archive is None:
        if not record or not record.get('archive'):
            refuse('there is no backup record; pass --archive NAME with --sha256 HEX')
        archive, sha256_expected = record['archive'], record.get('sha256')
    elif record and record.get('archive') == archive:
        sha256_expected = record.get('sha256')
    elif not sha256_expected:
        refuse(f'{archive} is not the recorded archive, so its sha256 must be given with --sha256')
    if not isinstance(archive, str) or not archive or archive in ('.', '..') or '/' in archive or '\\' in archive:
        refuse('the archive name must be a single filename, without a directory')
    if not isinstance(sha256_expected, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', sha256_expected):
        refuse('a valid 64-digit SHA256 is required before downloading or extracting an archive')
    return archive, sha256_expected.lower()


def fetch(destination, name, into, runner=subprocess.run):
    where = into / '.restore'
    where.mkdir(parents=True, exist_ok=True)
    done = runner(['rclone', 'copyto', f'{destination}/{name}', str(where / name), '--retries', '3',
                   '--timeout', '30m'], capture_output=True, text=True, timeout=RCLONE_TIMEOUT_S)
    if done.returncode != 0:
        raise SystemExit(f'download failed: {(done.stderr or done.stdout).strip()[:300]}')
    return where / name


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def counts(into):
    records = into / 'artifacts'
    files = sum(1 for path in records.rglob('*') if path.is_file()) if records.exists() else 0
    return files, sum(path.stat().st_size for path in records.rglob('*') if path.is_file())


def evidence(into, importer=None):
    """Rebuild the evidence view the archive deliberately left out, inside the restored checkout only.

    The importer is the repository's own code, imported lazily: a host that has not run `make install` yet has
    no pyyaml and no venv, and that is said as the reason rather than reported as a broken archive."""
    if importer is None:
        try:
            from agentiac.evidence import import_evidence as importer
        except Exception as error:      # no venv yet, or an interpreter older than the code needs
            return False, (f'the repository code did not import here ({type(error).__name__}: {str(error)[:120]}); '
                            f'run make install, then {cli_name()} evidence import')
    try:
        importer(into)
    except Exception as error:
        return False, f'{type(error).__name__}: {str(error)[:200]}'
    return True, 'rebuilt inside the restored checkout'


def main(argv=None, destination=None, fetcher=None, runner=subprocess.run, record=None, reader=None,
         loader=backup.load, importer=None):
    parser = argparse.ArgumentParser(prog='restore-control.py', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--into', required=True, help='an agentiac checkout with no records of its own')
    parser.add_argument('--archive', help='a remote archive the backup record does not name')
    parser.add_argument('--sha256', help='its digest, required with --archive')
    parser.add_argument('--destination', default=None, help='rclone remote and path (default: the record, then the declared backup_destination)')
    parser.add_argument('--dry-run', action='store_true', help='print what would happen and change nothing')
    args = parser.parse_args(argv)
    record = record if record is not None else loader(ROOT)
    where = target(ROOT, args.into, holds_the_records=record is not None)
    destination = args.destination or (record or {}).get('destination') or backup.destination(ROOT)
    if record is None and args.archive is None:
        record = (reader or remote_record)(destination)
        if record:
            print('record    read from the remote copy (this host has none)')
    name, expected = chosen(record, args.archive, args.sha256)
    print(f'archive    {name} from {destination}')
    print(f'sha256     {"the recorded one" if expected and expected == (record or {}).get("sha256") else "given by the caller"}')
    print(f'into       {where}')
    if record and record.get('bytes'):
        print(f'size       {record["bytes"] / 1073741824:.2f} GiB compressed (the run reported {record.get("files")} files)')
    if args.dry_run:
        print('dry run: nothing was downloaded and nothing was written')
        return 0

    path = (fetcher or fetch)(destination, name, where)
    got = sha256(path)
    if expected and got != expected:
        path.unlink(missing_ok=True)
        raise SystemExit(f'restore refused: {name} sha256 is {got[:16]}, the record says {expected[:16]}')
    print(f'sha256     matches ({got[:16]})')

    try:
        with backup.reading(path) as archive:
            backup.extract(archive, where)
    except ValueError as error:
        raise SystemExit(f'restore refused: {error}')
    files, size = counts(where)
    print(f'extracted  {files} files under {where}/artifacts ({size / 1073741824:.2f} GiB)')
    path.unlink(missing_ok=True)

    ok, detail = evidence(where, importer)
    print(f'evidence   {"rebuilt" if ok else "not rebuilt"}: {detail}')

    print('\nwhat is left to a human, in order (docs/operations.md "Restoring control"):')
    for command, why in HUMAN_STEPS:
        print(f'  {command:<38} {why}')
    print('\nNothing above was run. The restored checkout holds the records; it is not serving anything yet.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
