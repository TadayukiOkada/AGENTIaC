#!/usr/bin/env python3
"""Worker-local pinned Strata preparation; no model start or privileged package changes.

Every preparation names its manifest (`--backend <manifest.json>`), and prepares that backend in the root the
inventory declares for it (`prepare-<id>-state.json`), reusing a byte-identical file from `--reuse-root` as a
verified independent copy (a reflink where the filesystem clones, a full copy otherwise; never a hard link, because
0.2.8 repairs a mismatched file in place). There is no legacy no-manifest path (`prepare-state.json` is retired).
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

FLOOR_GIB = 40      # disk kept free for the owner at all times (strata-128k-plan.md)
SCRATCH_GIB = 15    # packs, MTP draft layer, build; a manifest raises it per host where that under-counts
RESERVE_DEFAULT = 700  # upstream's --vram-reserve-mib default, for a manifest that names none
FICLONE = 0x40049409  # the same-superblock clone ioctl (independent inode, shared extents)
ISOLATED_INPUTS = ('prepare-worker.py', 'start-worker.py', 'backends', 'prepare.lock', 'api-key-claim.env')


def isolated_leftovers(root):
    """Names in an isolated root other than the inputs a preparation is handed; anything else means a
    previous attempt (source, data, .venv, home) and must be inspected before resuming."""
    return sorted(p.name for p in root.iterdir()
                  if p.name not in ISOLATED_INPUTS and not p.name.startswith('prepare-'))


def saved_stage(root, backend_path):
    """The stage the last run of *this* manifest left in this root, or None when it left none.

    A gate that refuses has to say what it found, so this reads the state file without loading the manifest's
    rules: a manifest that fails its own checks must not turn a refusal into a traceback.
    """
    try:
        manifest_id = json.loads(Path(backend_path).read_text())['id']
        return json.loads((root / state_name({'id': manifest_id})).read_text())['stage']
    except (OSError, ValueError, KeyError, TypeError):
        return None


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def load_backend(path):
    """Manifest -> backend with {name: (size, sha256)} weights. A preparation always names one.

    The legacy no-manifest fallback is retired, so a fallback here could only invent a root nothing declares:
    refusing names what is missing instead.
    """
    if path is None:
        raise SystemExit('a preparation names its manifest: --backend <backends/ID.json>. The legacy Q2_0 root '
                         'this script prepared without one was retired; its recorded attempts stay under '
                         'artifacts/pipelines/')
    backend = json.loads(Path(path).read_text())
    backend['weights'] = {Path(f['path']).name: (f['size'], f['sha256']) for f in backend['files']}
    return backend


def state_name(backend):
    return f"prepare-{backend['id']}-state.json"


def resolved_reserve(args, backend):
    """The GPU reserve preparation should hand setup: what the operator named, else the manifest's per-host value.

    Preparation used to require --reserve-mib on the command line, which let stale manual reserves be copied
    into later roots. The manifest decides now, so a re-preparation cannot silently reinstate a reserve the start
    would only lower again."""
    asked = (args.reserve_mib if args.reserve_mib is not None
             else backend['workers'][args.node].get('vram_reserve_mib', RESERVE_DEFAULT))
    if isinstance(asked, bool) or not isinstance(asked, int) or asked < 0:
        raise SystemExit(f'invalid vram_reserve_mib for {args.node}: {asked!r} (want 0 or more)')
    return asked


def resolved_scratch(node, backend):
    """GiB of non-weight work the preparation still has to make on this host: the manifest's value or the default.

    15 GiB is the estimate for packs, the MTP draft layer and the build, and it fits the RTX 4080 host (its 0.2.8 root holds
    9.4 GiB beside the weights). The RTX 3090 host's Gyro-M is 39.4 GiB beside them, packs alone being 31.3 GiB, so a
    preparation there passed a 15 GiB scratch and finished under the 40 GiB floor the owner keeps.
    """
    asked = backend['workers'][node].get('scratch_gib', SCRATCH_GIB)
    if isinstance(asked, bool) or not isinstance(asked, int) or asked < 0:
        raise SystemExit(f'invalid scratch_gib for {node}: {asked!r} (want 0 or more)')
    return asked


def cuda_toolkit(backend):
    """The CUDA major setup compiles with: 12 for the x86 roots (the experimental engine-cuda12 build), 13 where the
    manifest says so. An Orin (JetPack 7.2) has only CUDA 13, and setup builds a CUDA 13 engine into engine/."""
    major = backend.get('cuda_toolkit_major', 12)
    if major not in (12, 13):
        raise SystemExit(f'invalid cuda_toolkit_major: {major!r} (want 12 or 13)')
    return major


def setup_argv(node, root, reserve_mib, backend):
    worker = backend['workers'][node]
    # `--build` is what tells setup to compile the engine from the source this preparation cloned, and it is the
    # Qwen roots' path. For a weight set whose engine is a download of its own it is the opposite of what is wanted:
    # setup.py skips fetching the DeepSeek engine whenever --build was given (`if exe is None and not a.build:
    # get_dsv41_engine(...)`, setup.py:9455-9461) and stops at its Step 4 - nothing in setup.py builds a DSV41
    # engine, only a hint at 9483 tells a developer to run cmake themselves. Naming the manifest's pinned package
    # with --dsv41-package is the flag upstream offers for exactly this case.
    engine = (['--dsv41-package', str(root / backend['engine_package']['dir'] / backend['engine_package']['name'])]
              if backend.get('engine_package') else ['--build'])
    argv = ['setup.py', '--yes', '--family', backend['family'], '--model', backend['model'],
            '--context', str(backend['context']), '--vision', 'no', '--gpu', '0', '--cuda', str(cuda_toolkit(backend)), *engine,
            '--no-start', '--no-browser', '--data-dir', str(root / 'data'), '--host', '127.0.0.1',
            '--port', '18085', '--source', 'huggingface', '--vram-reserve-mib', str(reserve_mib)]
    if worker.get('low_ram', 'auto') != 'auto':
        argv += ['--low-ram', worker['low_ram']]
    if worker.get('drafter'):
        # One drafter per run is Strata's rule (strata_control/drafting.py): naming dflash2 here is what keeps the
        # MTP draft layer's 5.21 GB off the disk, so a root built for one drafter does not quietly hold the other.
        argv += ['--drafter', worker['drafter']]
    return argv


def isolated_environment(environ, root, venv):
    """Remove inherited setup overrides and confine Strata's settings to this root."""
    env = {key: value for key, value in environ.items()
           if not key.startswith(('STRATA_', 'HF_', 'HUGGINGFACE_', 'MODELSCOPE_', 'XDG_', 'PIP_', 'PYTHON', 'GIT_'))
           and key not in ('CC', 'CXX', 'CUDAHOSTCXX', 'CMAKE_PREFIX_PATH', 'LD_PRELOAD', 'LD_LIBRARY_PATH')}
    env.update(HOME=str(root / 'home'), XDG_CONFIG_HOME=str(root / 'home' / '.config'),
               XDG_DATA_HOME=str(root / 'home' / '.local/share'), XDG_CACHE_HOME=str(root / 'home' / '.cache'),
               STRATA_SETTINGS_FILE=str(root / 'home' / 'strata-settings.json'),
               # /usr/sbin last: on a Jetson nvidia-smi is /usr/sbin/nvidia-smi, and setup stops without it
               PATH=str(venv / 'bin') + ':/usr/local/cuda/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
               PYTHONUNBUFFERED='1', OMP_NUM_THREADS='4')
    return env


def terminal_root(source):
    """Where setup.py, requirements.txt, serve/server.py, and compiled engines live inside SOURCE.

    Releases up to 0.2.15 place them at SOURCE directly; 0.2.16+ places them under SOURCE/terminal/.
    """
    source = Path(source)
    nested = source / 'terminal'
    return nested if (nested / 'setup.py').is_file() else source


def phase_setup(args):
    backend = load_backend(args.backend)
    weights = backend['weights']
    sys.path.insert(0, str(terminal_root(args.root / 'source')))
    import setup
    original_run = setup.run
    original_download = setup.download
    def guarded_run(command, *positional, **keywords):
        command = list(command)
        if Path(str(command[0])).name in ('sudo', 'apt', 'apt-get', 'dpkg', 'systemctl'):
            raise RuntimeError('privileged/system package changes are outside this preparation')
        if '-j' in command:
            command[command.index('-j') + 1] = '4'
        return original_run(command, *positional, **keywords)
    def verified_download(url, path, *positional, **keywords):
        result = original_download(url, path, *positional, **keywords)
        path = Path(path)
        if path.name in weights:
            size, expected = weights[path.name]
            if path.stat().st_size != size or digest(path) != expected:
                raise RuntimeError('pinned model size/SHA256 mismatch: ' + path.name)
        return result
    original_repair = getattr(setup, 'repair_download', None)
    def guarded_repair(url, path, *positional, **keywords):
        # 0.2.8 rewrites a mismatched file in place (open(path, 'r+b')); a preserved model must never
        # share that inode, so an in-place repair is allowed only on a file with no other link.
        path = Path(path)
        if path.exists() and path.stat().st_nlink != 1:
            raise RuntimeError('refusing the in-place repair of a multiply linked file: ' + path.name)
        return original_repair(url, path, *positional, **keywords)
    setup.run = guarded_run
    setup.download = verified_download
    if original_repair is not None:
        setup.repair_download = guarded_repair
    sys.argv = setup_argv(args.node, args.root, args.reserve_mib, backend)
    return setup.main()


def clone_or_copy(source, target):
    """Give `target` its own inode: a reflink where the filesystem clones, a full byte copy otherwise.

    Never a hard link. 0.2.8's repair_download reopens a finished file r+b and rewrites blocks in
    place, which would mutate a preserved model that shares the inode."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.cloning')
    with source.open('rb') as read, temporary.open('wb') as write:
        try:
            fcntl.ioctl(write.fileno(), FICLONE, read.fileno())
        except OSError:
            pass  # no clone support (ext4, another superblock): the byte copy below
    if temporary.stat().st_size != source.stat().st_size:
        shutil.copyfile(source, temporary)
    temporary.replace(target)


def reuse_files(backend, root, reuse_root, record):
    """Copy files marked same_as from reuse_root once size and SHA256 match; return the copied names."""
    copied = []
    for entry in backend['files']:
        if 'same_as' not in entry or reuse_root is None:
            continue
        target = root / 'data/models' / entry['path']
        source = reuse_root / 'data/models' / entry['same_as']
        if target.exists() or not source.is_file() or source.stat().st_size != entry['size']:
            continue
        if source.stat().st_dev != root.stat().st_dev:
            continue
        record('reuse', file=entry['path'])
        if digest(source) != entry['sha256']:
            raise RuntimeError('reuse candidate hash mismatch: ' + entry['same_as'])
        clone_or_copy(source, target)
        if target.stat().st_ino == source.stat().st_ino or target.stat().st_nlink != 1:
            raise RuntimeError('reuse copy shares an inode with the preserved model: ' + entry['same_as'])
        if digest(target) != entry['sha256']:
            target.unlink()
            raise RuntimeError('reuse copy hash mismatch: ' + entry['path'])
        copied.append(Path(entry['path']).name)
    return copied


def needed_bytes(backend, root, scratch_gib=SCRATCH_GIB):
    """Bytes still to download plus the retained floor and scratch space."""
    present = sum(size for name, (size, _) in backend['weights'].items()
                  if any(p.stat().st_size == size for p in (root / 'data/models').rglob(name)))
    return sum(size for size, _ in backend['weights'].values()) - present + (FLOOR_GIB + scratch_gib) * 1024**3


def reserve_then_reuse(backend, root, scratch_gib, reuse_root, record, free=None):
    """Check the headroom, then copy the reused weights — in that order, because a check after the copy
    counts a copied weight as present and asks for floor plus scratch (55 GiB) while an 85.6 GiB clone has
    already run the owner's file system down. A resumed preparation still passes: what the root already
    holds is counted as present here too.
    """
    need = needed_bytes(backend, root, scratch_gib)
    measured = shutil.disk_usage(root).free if free is None else free
    if measured < need:
        raise RuntimeError(f'need {need / 1024**3:.0f} GiB free including retained disk headroom, '
                           f'{measured / 1024**3:.0f} GiB free on {root}')
    return reuse_files(backend, root, reuse_root, record)


def pack_model_files(root, args):
    """What the pack this config names was built from, as {file name: (bytes, path)} — empty when no pack is named.

    A DeepSeek-V4.1 config carries no model paths: the engine is started with `--pack DIR` and the pack holds the
    model (setup.py's pack step through tools/dsv41/pack_gguf.py). setup.py records there the size and time of every
    file the pack was made from (`dsv41_write_pack_source`, setup.py:9179-9195, `pack-source.json` with `model_dir`
    and names that may be relative to it, like `../deepseek-v4.1-flash-shared/...`). Reading it is what lets a
    preparation say which bytes a measurement was taken from instead of assuming it.
    """
    for arg in map(str, args):
        record = Path(arg) / 'pack-source.json'
        if not arg.startswith(str(root) + '/') or not record.is_file():
            continue
        data = json.loads(record.read_text(encoding='utf-8-sig'))
        base = Path(data.get('model_dir') or arg)
        return {Path(name).name: (int(entry[0]), str(base / name))
                for name, entry in (data.get('files') or {}).items()}
    return {}


def find_config(root, weights):
    """The prepared engine config that names this backend's first shard.

    A Qwen preparation writes `strata-*.json` in the source checkout. A DeepSeek-V4.1 preparation writes it where the
    set was installed (under the root's data dir, beside the engine package and the shared files - setup.py keeps DSV41
    installs out of the Qwen engine's files), so the search covers both and still demands exactly one answer.

    Naming the shard is not enough to be *this* preparation's config: the upstream checkout ships `strata-*.json` of its
    own under `source/bench/results/**`, written for other people's machines. Those name model files that are not here,
    and a preparation that picked one would audit a config it never wrote (some ship with a UTF-8 BOM or invalid
    JSON). A candidate is the preparation's own only when it points at a file this root holds - the rule
    audit_config then applies to the chosen one - and a file whose bytes are not JSON at all is not a candidate
    either.
    """
    first = sorted(weights)[0]
    for where in ((root / 'source'), (root / 'data')):
        found = []
        for path in where.rglob('strata-*.json'):
            if 'shared-settings' in path.name:
                continue
            try:
                args = json.loads(path.read_text(encoding='utf-8-sig')).get('args', [])
            except (OSError, ValueError):
                continue
            if first not in ' '.join(map(str, args)):
                packed = pack_model_files(root, args)
                if first not in packed or packed[first][0] != weights[first][0]:
                    continue
            if not any(arg.startswith(str(root) + '/') for arg in map(str, args)):
                continue
            found.append(path)
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise RuntimeError('expected exactly one prepared config for ' + first + f' under {where}: '
                               + ', '.join(str(p) for p in found[:4]))
    raise RuntimeError('no prepared config names ' + first)


def audit_config(config, root, engine, weights=None):
    """Audit the generated config before trusting it: the engine is the binary we built and every model
    file it names is inside this root. Returns what the state records (lib_dirs may name system CUDA).

    When the config names a pack instead of model files (every DeepSeek-V4.1 config does), the pack's own record is
    audited: every file the manifest pins has to be in the pack, at the pinned length, and where this root holds it.
    A pack built from other bytes would be the thing a measurement is taken from.
    """
    prepared = json.loads(Path(config).read_text(encoding='utf-8-sig'))
    if Path(str(prepared.get('exe', ''))).resolve() != Path(engine).resolve():
        raise RuntimeError('prepared config does not name the built engine: ' + str(prepared.get('exe')))
    args = [str(arg) for arg in prepared.get('args', [])]
    model_files = [arg for arg in args if arg.startswith('/') and arg.endswith(('.gguf', '.bin'))]
    for path in model_files:
        if not Path(path).is_relative_to(root):
            raise RuntimeError('prepared config names a model file outside the root: ' + path)
    mtp_dir = Path(args[args.index('--mtp') + 1]).parent if '--mtp' in args else None
    pack = None
    if not model_files and weights:
        packed = pack_model_files(root, args)
        missing = sorted(name for name, (size, _) in weights.items()
                         if name not in packed or packed[name][0] != size)
        if missing:
            raise RuntimeError('the pack this config names was not built from what the manifest pins: '
                               + ', '.join(missing[:4]))
        outside = sorted(name for name, (_, path) in packed.items() if not Path(path).is_relative_to(root))
        if outside:
            raise RuntimeError('the pack names a model file outside the root: ' + ', '.join(outside[:4]))
        model_files = sorted(packed)
        pack = next(str(Path(arg)) for arg in args
                    if arg.startswith(str(root) + '/') and (Path(arg) / 'pack-source.json').is_file())
    return {'exe': prepared['exe'], 'model_files': len(model_files),
            'mtp_dir': str(mtp_dir) if mtp_dir else None, 'pack': pack}


def verify_mtp(python, source, mtp_dir, env):
    """Check the fetched MTP tensors against the pinned checkpoint's hashes (upstream tool, local files only)."""
    if mtp_dir is None or not (Path(mtp_dir) / 'tensors').is_dir():
        return None
    check = subprocess.run([str(python), str(terminal_root(source) / 'tools' / 'mtp_fetch.py'), 'verify', '--out', str(mtp_dir)],
                           capture_output=True, text=True, env=env, timeout=1800)
    if check.returncode:
        raise RuntimeError('MTP tensor verification failed: ' + (check.stdout or check.stderr)[-300:])
    return True


def drafter_choice(backend, node):
    """The drafter this host's worker block declares, or None when it declares none (Strata's own default applies)."""
    return backend['workers'][node].get('drafter')


def fetch_drafter_pack(python, source, root, backend, node, env):
    """Fetch the drafter file the manifest pins, through the engine's own pinned download, then check it here.

    Strata downloads nothing at a start (strata_control/acquisition.py), so a root built for dflash2 needs this
    explicit step: `strata drafting fetch` takes the 1.2 GB safetensors side, packs it with
    tools/dflash2_pack.py --q4_0 into the engine's file and removes the download. Strata checks its own pins; the
    manifest carries the same size and SHA-256 so the check is ours too. A missing or mismatched pack fails the
    preparation, because serve/server.py would otherwise start the MTP with a one-line note and the root would serve
    a drafter nothing declared.
    """
    declared = backend.get('drafter_pack')
    if not declared:
        raise SystemExit(f"{backend['id']} declares workers.{node}.drafter = {drafter_choice(backend, node)} "
                         'but names no drafter_pack: the file that drafter needs is a pin, not a guess')
    app_root = terminal_root(source)
    got = subprocess.run([str(python), str(app_root / 'strata.py'), 'drafting', 'fetch',
                          '--data-dir', str(root / 'data')], cwd=app_root, env=env,
                         capture_output=True, text=True, timeout=3600)
    target = root / 'data' / declared['path']
    if not target.is_file():
        raise RuntimeError('the drafter fetch left no pack: ' + declared['path'] +
                           ' (' + (got.stdout or got.stderr)[-300:] + ')')
    if target.stat().st_size != declared['size']:
        raise RuntimeError(f"drafter pack is {target.stat().st_size} bytes, the manifest pins "
                           f"{declared['size']}: {declared['path']}")
    measured = digest(target)
    if measured != declared['sha256']:
        raise RuntimeError('drafter pack hash mismatch: ' + declared['path'])
    return {'path': str(target), 'bytes': target.stat().st_size, 'sha256': measured,
            'fetched_for': drafter_choice(backend, node)}


def node_of(backend, node):
    """The worker block this run is for, or a refusal that names what the manifest does declare.

    The node was once a hardcoded list (`<four host names>`), which is the shape of a question this repo
    keeps being asked: a new host is declared in infra/inventory.yml and backends/*.json, and the worker script says
    it does not know the name. The manifest is the record of which hosts a backend was written for, so it decides here,
    and a name it does not carry is a refusal rather than a default.
    """
    workers = backend.get('workers') or {}
    if node not in workers:
        raise SystemExit(f"{backend['id']} has no worker settings for {node}; it declares "
                         + ', '.join(sorted(workers)) if workers else f"{backend['id']} declares no workers")
    return workers[node]


def verify_engine_package(root, backend):
    """The engine package this weight set's engine ships as, checked against the manifest before setup is handed it.

    Strata's Qwen engine is built from the source the preparation clones. The DeepSeek engine is a download of its
    own (setup.py's DSV41_ENGINE_DIR), and setup.py's card gate will not fetch it for a DGX Spark: "The DGX Spark
    (GB10) builds its own", or hand it the file from the release page (the same message's `--dsv41-package` /
    put-it-in-<dir> path). Handed over is what we do, so the file that lands in the root is pinned here by name,
    size and SHA-256 as the GitHub release carries it - and checked here. setup.py then verifies its manifest
    signature and every file's hash before using it; what this gate keeps is the other half, that the bytes we put
    in the root are the ones we say we put there, before any measurement names that engine as the one it measured.
    """
    pin = backend.get('engine_package')
    if not pin:
        return None
    where = root / pin['dir']
    package = where / pin['name']
    if not package.is_file():
        raise RuntimeError(f'{backend["id"]} runs on the engine package {pin["name"]}, which is not in {where}; '
                           f'fetch it from {pin["url"]} (pinned {pin["source_ref"]}) before preparing')
    size, measured = package.stat().st_size, digest(package)
    if size != pin['size'] or measured != pin['sha256']:
        raise RuntimeError(f'{package} is not what {backend["id"]} pins: {size} bytes {measured}, '
                           f'the manifest says {pin["size"]} {pin["sha256"]}')
    return {'path': str(package), 'bytes': size, 'sha256': measured, 'pinned_from': pin['url']}


def prepared_engine(root, source, backend):
    """The engine binary this preparation built, or a refusal that says where it looked.

    Two shapes exist and the manifest says which family it expects (engine_note in backends/strata-0.2.13-dsv41-coder-iq2_xxs.json):
    a Qwen root builds `source/<engine_dir>/<engine_binary>`; the DeepSeek engine is shipped as its own package under
    `<parent-of-engine>/engine-dsv41/<version>-<platform>-<backend>/` (setup.py's DSV41_ENGINE_DIR), where inside
    a prepared root that parent is the checkout (`source/engine-dsv41`). Guessing between them is what audit_config
    exists to prevent, so an ambiguous or missing binary fails here, before anything is recorded as measured.
    """
    binary = backend.get('engine_binary', 'strata')
    engine_dir = backend.get('engine_dir', 'engine-cuda12')
    app_direct = terminal_root(source) / engine_dir / binary
    if app_direct.is_file():
        return app_direct
    direct = source / engine_dir / binary
    if direct.is_file():
        return direct
    package = backend.get('engine_package_dir')
    if not package:
        raise RuntimeError(f'no engine at {direct} and {backend["id"]} names no engine_package_dir to look in instead')
    where = {Path(package) if str(package).startswith('/') else root / package}
    found = sorted({p for base in where if base.is_dir() for p in base.rglob(binary) if p.is_file()})
    if len(found) != 1:
        raise RuntimeError(f'{binary} is not where {backend["id"]} says it is: {len(found)} matches under '
                           f'{", ".join(str(base) for base in sorted(where))} '
                           f'({", ".join(str(p) for p in found[:4]) or "none"}); the engine this root built has to be one file')
    return found[0]


def verify_drafter_pack(root, backend):
    """Check a drafter file the preparation fetched as it is, rather than packing one here.

    `strata drafting fetch` is the DFlash2 path (strata_control/drafting_fetch.py: a pinned safetensors download packed by
    tools/dflash2_pack.py). A DeepSeek-V4.1 set arrives with its DSpark drafter already pinned in setup.py (DSV41_DSPARK,
    in every DSV41 set's `shared` list) and the pack step folds it in (dsv41_pack_cmd -> tools/dsv41/pack_gguf.py
    --dspark-gguf), so the only thing left to check is that the file is the one this manifest names.
    """
    declared = backend['drafter_pack']
    found = [p for p in (root / 'data').rglob(Path(declared['path']).name) if p.is_file()]
    if len(found) != 1:
        raise RuntimeError(f"{backend['id']} pins a drafter at {declared['path']} and this root holds {len(found)} "
                           'copies of that name; the drafter a pack carries is not a guess')
    if found[0].stat().st_size != declared['size'] or digest(found[0]) != declared['sha256']:
        raise RuntimeError(f"drafter pack is not what {backend['id']} pins: {found[0]}")
    return {'path': str(found[0]), 'bytes': found[0].stat().st_size, 'sha256': digest(found[0]),
            'fetched_by': declared.get('fetched_by', 'setup')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True, help='the host this preparation is for; the backend manifest decides which hosts it has settings for')
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--reserve-mib', type=int, help="VRAM to keep free for other programs; default: the manifest's per-host value")
    parser.add_argument('--backend', type=Path, required=True, help='backend manifest (backends/*.json)')
    parser.add_argument('--reuse-root', type=Path, help='prepared root whose identical files may be copied (never linked)')
    parser.add_argument('--resume-failed', action='store_true', help='continue after an inspected failed state')
    parser.add_argument('--phase', choices=('controller', 'setup'), default='controller')
    parser.add_argument('--isolated', action='store_true', help='new isolated root: sanitized environment, private settings, no hard links')
    args = parser.parse_args()
    args.root = args.root.resolve()
    if args.isolated and args.phase == 'controller' and args.root.exists():
        leftovers = isolated_leftovers(args.root)
        if leftovers:
            # The gate is the inspection `isolated_leftovers`'s own docstring asks for, and it has to stay passable:
            # a preparation that died partway (source cloned, venv built, the engine refused) leaves exactly these
            # names behind, and upstream's own words are "everything already done is kept and skipped". Without a
            # way back in, an isolated root that failed once could never be prepared again.
            stage = saved_stage(args.root, args.backend)
            if not (args.resume_failed and stage == 'failed'):
                parser.error('--isolated needs an empty root, or --resume-failed for a root this same manifest '
                             f'left a failed preparation in (saved stage: {stage or "none of this manifest"}); '
                             'still present: ' + ', '.join(leftovers[:8]))
    os.umask(0o077)
    backend = load_backend(args.backend)
    node_of(backend, args.node)
    args.reserve_mib = resolved_reserve(args, backend)
    args.scratch_gib = resolved_scratch(args.node, backend)
    if args.phase == 'setup':
        raise SystemExit(phase_setup(args))
    args.root.mkdir(parents=True, exist_ok=True)
    if args.isolated:
        home = args.root / 'home'
        home.mkdir(exist_ok=True)
        clean = isolated_environment(os.environ, args.root, args.root / '.venv')
        os.environ.clear()
        os.environ.update(clean)
    with (args.root / 'prepare.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = args.root / state_name(backend)
        if state.exists():
            previous = json.loads(state.read_text())
            if not (args.resume_failed and previous.get('stage') == 'failed'):
                raise SystemExit('Saved state exists; inspect before resuming')
        manifest = {'node': args.node, 'backend': backend['id'], 'revision': backend['source_revision'],
                    'model_repo': backend['model_repository'], 'model_revision': backend['model_revision'],
                    'family': backend['family'], 'model': backend['model'], 'weights': backend['weights'],
                    'context': backend['context'], 'cuda_toolkit': cuda_toolkit(backend), 'reserve_mib': args.reserve_mib,
                    'runner_sha256': digest(Path(__file__)), 'build_jobs': 4, 'starts_model': False}
        manifest['backend_sha256'] = digest(args.backend)
        (args.root / f"prepare-{backend['id']}-manifest.json").write_text(json.dumps(manifest, indent=2) + '\n')
        def record(stage, **fields):
            temporary = state.with_suffix('.tmp')
            temporary.write_text(json.dumps({'stage': stage, 'updated': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                            **fields}, indent=2) + '\n')
            temporary.replace(state)
        try:
            # The headroom gate comes before anything is copied: an uncopied weight is the largest part of
            # a preparation, and asking only after the copy lets the copy run the owner's file system down.
            record('source')
            source = args.root / 'source'
            if not source.exists():
                subprocess.run(['git', 'init', '-q', str(source)], check=True)
                subprocess.run(['git', '-C', str(source), 'remote', 'add', 'origin', 'https://github.com/Niko1221/Strata.git'], check=True)
                subprocess.run(['git', '-C', str(source), 'fetch', '-q', '--depth', '1', 'origin', backend['source_revision']], check=True)
                subprocess.run(['git', '-C', str(source), 'checkout', '-q', '--detach', backend['source_revision']], check=True)
            head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
            if head != backend['source_revision']:
                raise RuntimeError('existing source is not at the pinned revision')
            app_root = terminal_root(source)
            reused = reserve_then_reuse(backend, args.root, args.scratch_gib, args.reuse_root, record)
            record('python-environment', reused=reused)
            venv = args.root / '.venv'
            python = venv / 'bin/python'
            if not python.exists():
                subprocess.run(['python3', '-m', 'venv', str(venv)], check=True)
            subprocess.run([str(python), '-m', 'pip', 'install', '-q', '--only-binary=:all:', '-r', str(app_root / 'requirements.txt')], check=True)
            if backend.get('pip_build_tools'):  # e.g. a newer CMake than the system one, inside the venv only
                subprocess.run([str(python), '-m', 'pip', 'install', '-q', '--only-binary=:all:', *backend['pip_build_tools']], check=True)
            home = args.root / 'home'; home.mkdir(exist_ok=True)
            env = (isolated_environment(os.environ, args.root, venv) if args.isolated else
                   dict(os.environ, HOME=str(home), PATH=str(venv / 'bin') + ':' + os.environ.get('PATH', ''),
                        PYTHONUNBUFFERED='1', OMP_NUM_THREADS='4'))
            if args.isolated:
                (home / 'strata-settings.json').write_text(json.dumps({'data_dir': str(args.root / 'data'), 'installs': []}))
            for name in ('HF_ENDPOINT', 'MODELSCOPE_ENDPOINT', 'STRATA_SOURCE', 'STRATA_PREBUILT_URL', 'STRATA_ALLOW_UNPINNED'):
                env.pop(name, None)
            package = verify_engine_package(args.root, backend)
            if package:
                record('engine-package', package=package)
            record('build-and-download', reused=reused)
            command = [str(python), str(Path(__file__).resolve()), '--phase', 'setup', '--node', args.node,
                       '--root', str(args.root), '--reserve-mib', str(args.reserve_mib),
                       '--backend', str(args.backend.resolve())]
            if args.isolated:
                command.append('--isolated')
            child = subprocess.run(command, cwd=app_root, env=env, timeout=8 * 3600)
            if child.returncode:
                raise RuntimeError('Strata setup exited ' + str(child.returncode))
            record('verify', reused=reused)
            weights = {}
            for name, (size, expected) in backend['weights'].items():
                matches = list((args.root / 'data/models').rglob(name))
                if len(matches) != 1 or matches[0].stat().st_size != size:
                    raise RuntimeError('missing or ambiguous pinned model shard')
                measured = digest(matches[0])
                if measured != expected:
                    raise RuntimeError('final model hash mismatch')
                weights[name] = measured
            subprocess.run([str(python), '-m', 'pip', 'freeze'], stdout=(args.root / 'python-freeze.txt').open('w'), check=True)
            engine = prepared_engine(args.root, source, backend)
            fields = {'weights': weights, 'engine_sha256': digest(engine), 'engine': str(engine), 'reused': reused,
                      'disk_available_bytes': shutil.disk_usage(args.root).free}
            if backend.get('drafter_pack') and backend.get('drafter_pack', {}).get('fetched_by') == 'setup':
                record('drafter')
                fields['drafter_pack'] = verify_drafter_pack(args.root, backend)
            elif drafter_choice(backend, args.node) and drafter_choice(backend, args.node) != 'mtp':
                record('drafter')
                fields['drafter'] = drafter_choice(backend, args.node)
                fields['drafter_pack'] = fetch_drafter_pack(python, source, args.root, backend, args.node, env)
            config = find_config(args.root, backend['weights'])
            fields['config'] = str(config)
            fields.update(audit_config(config, args.root, engine, backend['weights']))
            fields['mtp_verified'] = verify_mtp(python, source, fields.get('mtp_dir'), env)
            record('prepared', **fields)
        except Exception as error:
            record('failed', error_type=type(error).__name__, error=str(error)[:500])
            raise


if __name__ == '__main__':
    main()
