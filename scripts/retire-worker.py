#!/usr/bin/env python3
"""The host-side half of `agentiac ops retire`, `ops thin` and the N-1 restore (T5.2).

Run on the node, by `scripts/retire.py`, over the same ssh path the read-only checks use. It is copied to the
host and run there — like `strata-prepare-worker.py` — because the questions it answers are about inodes,
hashes and open file descriptors, and those do not travel well through a shell quoting layer.

It answers three things:

  measure   what is at a path: size, entry count, how much of it shares an inode with something outside (F8),
            whether it is a symlink or a mount point, whether a process of this user has it open (and, when
            `sudo -n` is accepted there, whether any other user's process does), and whether the on-demand front
            THIS HOST RUNS starts from it — read off the unit's own `ExecStart`, with the residue an older claim
            left in the root reported apart from that answer.
  thin      move the weight files of a rollback root into the staging directory, keeping the engine, the packs,
            the MTP layer and the rest of the root in place (S §5.7). Refuses anything whose hash does not match
            the manifest pin here and at a copy apart from this root (§3.2), and anything not on its own inode
            (ADR 0008).
  restore   put those weights back with a verified copy on an independent inode — from the isolated copy if it
            is still there, otherwise from another root this host declares. Never a hard link: 0.2.8's
            `repair_download` reopens a finished file `r+b` and would damage both generations.

It is not self-governing: the *decision* is `scripts/retire.py`'s, which reads `infra/retention.yml` and the
refusals in `src/agentiac/retire.py`. What this script adds is that a refusal here is not the last line of
defence — an interrupted thin leaves the weights in the staging directory, which is a visible state, and a
restore that does not hash the same refuses to call the root full.

Coverage, stated: an unprivileged opener scan sees this user's processes only. `--privileged` adds
`sudo -n lsof +D`, which is the read §5.6 used; where `sudo -n` is refused the answer is `unknown`, not 0.

Nothing here deletes. `rm -rf` is `scripts/retire.py` naming one path on one host (F11).
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

FICLONE = 0x9409
CHUNK = 8 * 1024**2
FLOOR_GIB = 40                       # the owner's floor, the number prepare holds (S §2.2)
GIB = 1024**3
FRONT_DIR = 'ondemand'               # the directory a root's own front lives in: the name borrow.py uses
ASK = object()                       # "ask this host which front it runs", as opposed to being told


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def du_bytes(path):
    """What `du -sx` says about this path, in bytes (one file system, no double counting of a mount)."""
    done = subprocess.run(['du', '-sx', '--block-size=1', str(path)], capture_output=True, text=True, check=False)
    if done.returncode or not done.stdout.strip():
        return 0
    return int(done.stdout.split('\t')[0])


def filesystem(path):
    done = subprocess.run(['df', '--output=target', str(path)], capture_output=True, text=True, check=False)
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    return lines[-1] if len(lines) > 1 else '?'


def mount_point(path):
    return subprocess.run(['mountpoint', '-q', str(path)], capture_output=True, check=False).returncode == 0


def walk(path):
    """(entries, shared_files, shared_bytes) — `shared` is a file with another name somewhere else, which is
    the part a deletion does not give back (F8: the RTX 4080 host's Q2_0 shard 2 was 26.8 GiB of what `du` counted)."""
    entries = shared = 0
    shared_bytes = 0
    for directory, names, files in os.walk(path):
        entries += len(names) + len(files)
        for name in files:
            try:
                info = os.stat(os.path.join(directory, name), follow_symlinks=False)
            except OSError:
                continue
            entries += 0
            if info.st_nlink > 1:
                shared += 1
                shared_bytes += info.st_size
    return entries, shared, shared_bytes


def open_processes(path, privileged):
    """How many processes have a file under PATH open.

    Unprivileged: this user's /proc/*/fd, which is what `lsof` run as this user can see. `privileged` adds
    `sudo -n lsof +D PATH` (the read S §5.6 used before its rm), which is the only way to see another user's
    process on a shared machine. Returns {'processes': n, 'method': ...} or {'unknown': ...} — never a 0 that
    stands for "nothing was read".
    """
    seen = set()
    target = str(path)
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        fds = Path('/proc') / entry / 'fd'
        try:
            for fd in os.listdir(fds):
                try:
                    where = os.readlink(fds / fd)
                except OSError:
                    continue
                if where == target or where.startswith(target.rstrip('/') + '/'):
                    seen.add(entry)
                    break
        except OSError:
            continue  # another user's process: invisible without privileges
    found = {'processes': len(seen), 'method': 'proc'}
    if privileged:
        done = subprocess.run(['sudo', '-n', 'lsof', '+D', str(path)], capture_output=True, text=True,
                              timeout=600, check=False)
        if done.returncode == 255 or 'sudo' in (done.stderr or '').lower() and done.returncode not in (0, 1):
            return {'unknown': f'sudo -n lsof +D {path} exited {done.returncode}: '
                               'another user\'s process could hold this open', 'method': 'lsof'}
        rows = [line for line in (done.stdout or '').splitlines() if line.strip()][1:]
        found = {'processes': max(len({row.split()[1] for row in rows if len(row.split()) > 1}), len(seen)),
                 'method': 'sudo lsof +D'}
    return found


def unit_files(path):
    """Unit files that name PATH: the system and user directories, including /run/systemd/system where
    `systemd-run` puts a transient unit (which is what a claim installs)."""
    home = Path.home()
    places = ['/etc/systemd/system', '/run/systemd/system', str(home / '.config/systemd/user'),
              str(home / '.config/systemd/system')]
    found = []
    for place in places:
        if not Path(place).is_dir():
            continue
        done = subprocess.run(['grep', '-rl', '--', str(path), place], capture_output=True, text=True,
                              timeout=120, check=False)
        found += [line.strip() for line in (done.stdout or '').splitlines() if line.strip()]
    return sorted(set(found))


def front_directory(path):
    """Where a root's own front lives: `<root>/ondemand`, the directory `scripts/borrow.py` names."""
    return str(Path(path) / FRONT_DIR)


def running_front(front_unit):
    """(the directory of the front this host RUNS, or None; an error string when the host could not be asked).

    The same read `borrow.running_front_directory` makes, for the same reason: a
    host holds several prepared roots and each carries its own `ondemand/config.json`, so a file inside a root
    says what a claim WOULD run there, not what the host is running. A retirement asks the second question, and
    only the unit's own `ExecStart` answers it. FRONT_UNIT is the front's unit name, which control passes (D6.8).
    """
    done = subprocess.run(['systemctl', 'cat', front_unit, '--no-pager'], capture_output=True, text=True,
                          timeout=60, check=False)
    for line in (done.stdout or '').splitlines():
        if line.startswith('ExecStart=') and f'/{FRONT_DIR}/proxy.py' in line:
            return line.split(f'/{FRONT_DIR}/proxy.py')[0].split()[-1] + f'/{FRONT_DIR}', None
    text = (done.stdout + done.stderr).lower()
    if done.returncode == 0 or 'no files found' in text or 'could not find' in text:
        return None, None                      # no front installed: no root here is being served through one
    return None, f'systemctl cat {front_unit} exited {done.returncode}: {(done.stderr or done.stdout).strip()[:160]}'


def front_configs(path):
    """The `*.json` in this root's own `ondemand` that name it: what a claim that served it left behind.

    Left behind is the point. A root that was claimed and then handed over to another root keeps its
    `config.json`, `proxy.py` and `api-key-claim.env` until the root itself moves, and none of them says what
    the host is running now.
    """
    directory = Path(path) / FRONT_DIR
    if not directory.is_dir():
        return []
    found = []
    for config in sorted(directory.glob('*.json')):
        try:
            text = config.read_text()
        except OSError:
            continue
        if str(path) in text:
            found.append(str(config))
    return found


def front_names(path, front=ASK, front_unit=None):
    """Whether the on-demand front this host RUNS starts from, keys or manifests inside PATH.

    True when the front unit's own `ExecStart` names this root's proxy — which includes the case where the model
    is unloaded and the front would load it on the next request, because that is a root a front still serves.
    The residue of a claim that has moved elsewhere is not a service: it answers False, and `front_configs`
    reports the residue so the refusal or the passing is read against what the host actually runs.
    """
    if front is ASK:
        front, _ = running_front(front_unit)
    if front is None:
        return False
    wanted = front_directory(path)
    if front == wanted:
        return True
    try:
        return Path(front).resolve() == Path(wanted).resolve()   # a root named through a symlink
    except OSError:
        return False


def manifest_weights(path):
    """Manifest -> {weight file name: (path under data/models, size, sha256)}."""
    spec = json.loads(Path(path).read_text())
    return {Path(entry['path']).name: (entry['path'], entry['size'], entry['sha256']) for entry in spec['files']}


def locate(root, relative, name, size):
    """The weight file of this root: the manifest's own path first, else the one file of that name."""
    exact = Path(root) / 'data/models' / relative
    if exact.is_file():
        return exact
    if not (Path(root) / 'data/models').is_dir():
        return None
    matches = [p for p in Path(root).joinpath('data/models').rglob(name) if p.is_file() and p.stat().st_size == size]
    return matches[0] if len(matches) == 1 else None


def weight_states(root, apart, manifest):
    """Per pinned weight: 'identical' when this root, a copy apart on this host and the manifest pin all agree,
    and a reason when they do not. `shared-inode` is its own state: ADR 0008 forbids a weight shared across
    generations, so a thin would move what another root is running.

    APART is every other root this host declares, not only the one that serves: a rollback root's weight can be
    held identically by another declared candidate root while the serving root holds a different quantization of
    the same model. The policy asks for "a copy on that host apart from the root it thins", and a restore has to
    be able to find one.

    Both hashes are read because §3.2 asks for both: the root's copy has to be the pinned one, and the copy apart
    has to be the pinned one too.
    """
    states = {}
    for name, (relative, size, expected) in manifest_weights(manifest).items():
        here = locate(root, relative, name, size)
        others = [str(place) for place in (apart or []) if str(place) != str(root)]
        there = None
        for place in others:
            candidate = locate(place, relative, name, size)
            if candidate is None:
                continue
            if there is None or (candidate.stat().st_size == size and there.stat().st_size != size):
                there = candidate
        entry = {'path': str(here) if here else None, 'apart_path': str(there) if there else None,
                 'apart_roots': others, 'size_bytes': size}
        if here is None:
            entry['state'] = 'missing-or-ambiguous' if (Path(root) / 'data/models').is_dir() else 'no-data-dir'
            states[name] = entry
            continue
        info = here.stat()
        entry['nlink'] = info.st_nlink
        if info.st_nlink != 1:
            entry['state'] = 'shared-inode'
            states[name] = entry
            continue
        if info.st_size != size:
            entry['state'] = 'size-differs'
            states[name] = entry
            continue
        if there is None:
            entry['state'] = 'no-copy-apart'
            states[name] = entry
            continue
        if digest(here) != expected:
            entry['state'] = 'hash-differs-from-pin'
            states[name] = entry
            continue
        if digest(there) != expected:
            entry['state'] = 'copy-apart-differs'
            states[name] = entry
            continue
        entry['state'] = 'identical'
        states[name] = entry
    return states


def clone_or_copy(source, target):
    """Give TARGET its own inode: a reflink where the filesystem clones, a byte copy otherwise.

    The same rule as scripts/strata-prepare-worker.py: never a hard link, because an in-place repair of a
    finished file would then damage both generations.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.restoring')
    with source.open('rb') as read, temporary.open('wb') as write:
        try:
            fcntl.ioctl(write.fileno(), FICLONE, read.fileno())
        except OSError:
            pass  # ext4 has no reflink
    if temporary.stat().st_size != source.stat().st_size:
        temporary.unlink()
        shutil.copyfile(source, temporary)
    temporary.replace(target)


def stage_dir(staging, root):
    """Where a thinned root's weights go: `<staging>/<root name>-weights/`, one directory per thinned root."""
    return Path(staging) / (Path(root).name + '-weights')


def move_weights(root, staging, manifest, apart):
    """Thin: move the weight files, keep everything else. Each move is verified at the destination.

    §3.2 is what this checks before it moves anything: the root's copy hashes to the manifest's pin, and a copy
    apart from this root — another root this host declares — hashes to the same pin. `restore` is the reason that
    has to be true, and a move that cannot be verified at the far end is not a thin either.
    """
    states = weight_states(root, apart, manifest)
    moved = []
    for name, entry in sorted(states.items()):
        if entry['state'] != 'identical':
            raise SystemExit(f'thin refused for {name}: {entry["state"]}')
        source, size = Path(entry['path']), entry['size_bytes']
        relative = source.relative_to(Path(root) / 'data/models')
        target = stage_dir(staging, root) / 'data/models' / relative
        if target.exists():
            raise SystemExit(f'thin refused for {name}: {target} already exists (a thin already ran?)')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        if target.stat().st_nlink != 1 or target.stat().st_size != size or digest(target) != manifest_hash(manifest, name):
            raise SystemExit(f'thin damaged {name}: it is at {target} but is not itself')
        moved.append({'name': name, 'from': str(source), 'to': str(target), 'size_bytes': size})
    return moved


def manifest_hash(manifest, name):
    return manifest_weights(manifest)[name][2]


def free_bytes(path):
    """Bytes free on the file system `path` lives on.

    One place asks the question, so a restore's floor check can be measured on a declared number instead of on
    whatever the machine running the check happens to have free. The answer it guards is the owner's floor (S §2.2).
    """
    return shutil.disk_usage(path).free


def put_weights_back(root, apart, staging, manifest):
    """Restore: a verified copy on an independent inode, from the isolated copy or from another root here.

    The staging copy is the first source because a thin put it there; the other declared roots are the fallback,
    which is what `ops restore` is for after a thinned root's staging copy was itself deleted.
    """
    available = free_bytes(root)
    if available < FLOOR_GIB * GIB:
        raise SystemExit(f'restore refused: {available / GIB:.0f} GiB free on {root}, '
                         f'the owner keeps {FLOOR_GIB} GiB free at all times')
    restored = []
    for name, (relative, size, expected) in sorted(manifest_weights(manifest).items()):
        target = Path(root) / 'data/models' / relative
        if target.exists():
            raise SystemExit(f'restore refused for {name}: {target} is already there (this root is full)')
        candidates = [stage_dir(staging, root) / 'data/models' / relative]
        candidates += [Path(place) / 'data/models' / relative for place in apart if str(place) != str(root)]
        source = next((p for p in candidates if p.is_file()), None)
        if source is None:
            raise SystemExit(f'restore refused for {name}: it is in none of {", ".join(str(p) for p in candidates)}. '
                             'Re-prepare this root (agentiac ops prepare) — a download is not a thin restore')
        if source.stat().st_size != size or digest(source) != expected:
            raise SystemExit(f'restore refused for {name}: {source} is not the pinned weight')
        clone_or_copy(source, target)
        if target.stat().st_ino == source.stat().st_ino or target.stat().st_nlink != 1:
            target.unlink()
            raise SystemExit(f'restore damaged {name}: the copy shares an inode with {source}')
        if digest(target) != expected:
            target.unlink()
            raise SystemExit(f'restore damaged {name}: {target} does not hash to the pin')
        restored.append({'name': name, 'from': str(source), 'to': str(target), 'size_bytes': size})
    return restored


def measure(args, front=ASK, front_error=None):
    out = {}
    staging = args.staging[0] if args.staging else None
    if front is ASK:
        # Only a root that carries its own front directory makes the question worth asking the host.
        front, front_error = (running_front(args.front_unit) if any((Path(raw) / FRONT_DIR).is_dir() for raw in args.path)
                              else (None, None))
    for raw in args.path:
        path = Path(raw)
        facts = {'path': raw, 'exists': path.is_dir() or path.is_file(), 'symlink': path.is_symlink(),
                 'resolved': str(path.resolve()) if path.exists() else None,
                 'mount_point': path.is_dir() and mount_point(path), 'filesystem': None, 'device': None,
                 'size_bytes': 0, 'entries': 0, 'shared_files': 0, 'shared_bytes': 0,
                 'openers': {'processes': 0}, 'units': [], 'front_names_path': False, 'front_residue': [],
                 'front_running': front, 'free_bytes': 0}
        if front_error:
            facts['front_error'] = front_error
        if not facts['exists']:
            out[raw] = facts
            continue
        info = path.stat()
        facts['device'] = info.st_dev
        facts['filesystem'] = filesystem(path)
        facts['free_bytes'] = shutil.disk_usage(path).free
        if path.is_dir():
            facts['size_bytes'] = du_bytes(path)
            facts['entries'], facts['shared_files'], facts['shared_bytes'] = walk(path)
            facts['front_residue'] = front_configs(path)
            facts['front_names_path'] = front_names(path, front)
        else:
            facts['size_bytes'] = info.st_size
            facts['entries'] = 1
        facts['openers'] = open_processes(path, args.privileged)
        facts['units'] = unit_files(path)
        if staging:
            if Path(staging).exists():
                facts['staging_path'] = staging
                facts['staging_device'] = Path(staging).stat().st_dev
            else:
                facts['staging_path'], facts['staging_device'] = staging, None
        out[raw] = facts
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='action', required=True)
    m = sub.add_parser('measure', help='read-only facts about each path (JSON on stdout)')
    m.add_argument('--path', action='append', required=True)
    m.add_argument('--staging', action='append', help='the staging directory, to compare filesystems')
    m.add_argument('--privileged', action='store_true', help='ask sudo -n lsof what this user cannot see')
    m.add_argument('--front-unit', required=True, help="the on-demand front's unit: <resource_prefix>-ondemand (D6.8)")
    t = sub.add_parser('thin', help='move the pinned weights of a rollback root into the staging directory')
    t.add_argument('--root', required=True)
    t.add_argument('--roots', action='append', required=True,
                   help='every root this host declares, this one included: the copy apart §3.2 asks for is found '
                        'among the others')
    t.add_argument('--staging', required=True)
    t.add_argument('--manifest', required=True)
    t.add_argument('--check', action='store_true', help='print the plan and change nothing')
    r = sub.add_parser('restore', help='put those weights back, verified and on independent inodes')
    r.add_argument('--root', required=True)
    r.add_argument('--roots', action='append', required=True,
                   help='every root this host declares, this one included: where a copy is looked for')
    r.add_argument('--staging', required=True)
    r.add_argument('--manifest', required=True)
    r.add_argument('--check', action='store_true', help='print the plan and change nothing')
    args = parser.parse_args(argv)

    if args.action == 'measure':
        print(json.dumps(measure(args)))
        return 0
    apart = [place for place in args.roots if str(place) != str(args.root)]
    if str(args.root) not in [str(place) for place in args.roots]:
        raise SystemExit(f'{args.root} is not among the roots this host declares '
                         f'({", ".join(str(place) for place in args.roots)}): the command would act on a path the '
                         'inventory does not name')
    if not Path(args.root).is_dir():
        raise SystemExit(f'{args.root} is not a directory on this host')
    if args.action == 'thin':
        states = weight_states(args.root, apart, args.manifest)
        if args.check:
            print(json.dumps({'root': args.root, 'staging': str(stage_dir(args.staging, args.root)), 'apart': apart,
                             'weights': states,
                             'bytes': sum(e['size_bytes'] for e in states.values() if e['state'] == 'identical')}))
            return 0
        print(json.dumps({'root': args.root, 'staging': str(stage_dir(args.staging, args.root)),
                          'moved': move_weights(args.root, args.staging, args.manifest, apart)}))
        return 0
    if args.check:
        weights = manifest_weights(args.manifest)
        places = [stage_dir(args.staging, args.root)] + [Path(place) for place in apart]
        missing = {name: {'path': str(Path(args.root) / 'data/models' / relative), 'size_bytes': size,
                          'state': 'restorable' if any((place / 'data/models' / relative).is_file() for place in places)
                          else 'nowhere-to-copy-from'}
                   for name, (relative, size, _) in sorted(weights.items())}
        print(json.dumps({'root': args.root, 'apart': apart, 'staging': str(stage_dir(args.staging, args.root)),
                          'weights': missing,
                          'bytes': sum(entry['size_bytes'] for entry in missing.values())}))
        return 0
    print(json.dumps({'root': args.root, 'restored': put_weights_back(args.root, apart, args.staging,
                                                                      args.manifest)}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
