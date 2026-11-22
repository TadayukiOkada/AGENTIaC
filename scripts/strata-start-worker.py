#!/usr/bin/env python3
"""Start only a prepared Strata instance with resource admission and private authentication.

--backend <manifest.json> names the prepared backend to start, at --context (one of the manifest's
contexts); a manifest with memory_cap false gets no MemoryMax, only OOMScoreAdjust=1000 so the kernel
kills our engine before the owner's work. There is no start without a manifest: the legacy no-manifest
path is retired, so every start names the manifest its declaration names.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request


def unified_memory(backend, node):
    """A Jetson: the GPU has no memory of its own, and nvidia-smi reports neither memory nor processes there."""
    return bool(backend['workers'].get(node, {}).get('unified_memory', False))


NVMAP_PARAMETERS = Path('/sys/module/nvmap/parameters')


def gpu_page_pool_properties(parameters=None):
    """On a Jetson, empty nvmap's page pool whenever the engine unit stops, however it stops.

    nvmap keeps the pages a GPU process frees in a pool (up to `pool_size`, 1/8 of RAM on a Jetson) for its
    next allocation, and MemAvailable does not count them, so after one Strata load and unload subsequent loads
    can be refused for apparent lack of free RAM. Writing a page count to `shrink_page_pools` frees that many pool pages; `pool_size` is the
    pool's capacity, so it empties the pool. The `+` prefix runs only this command as root, despite --uid and
    NoNewPrivileges; it runs after the unit's processes are gone, on an idle unload, a release, a crash or
    RuntimeMaxSec alike."""
    parameters = parameters or NVMAP_PARAMETERS
    if not (parameters / 'shrink_page_pools').exists():
        return []
    return [f'--property=ExecStopPost=+/bin/sh -c "cat {parameters}/pool_size > {parameters}/shrink_page_pools"']


def release_gpu_page_pool(parameters=None):
    """Empty nvmap's page pool now; False when this host has none or sudo refuses (the RAM check then refuses
    with its own figures). Only idle pool pages are freed, never pages a running GPU process holds, so it is safe
    beside the owner's work."""
    parameters = parameters or NVMAP_PARAMETERS
    shrink = parameters / 'shrink_page_pools'
    if not shrink.exists():
        return False
    pages = (parameters / 'pool_size').read_text().strip()
    done = subprocess.run(['sudo', '-n', 'tee', str(shrink)], input=pages + '\n', text=True,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return done.returncode == 0


def mem_available_bytes():
    return int(next(l.split()[1] for l in Path('/proc/meminfo').read_text().splitlines()
                    if l.startswith('MemAvailable:'))) * 1024


def admit_host_ram(need, unified, release=None, measure=None):
    """Return (available, released): free RAM as the engine will find it, and how much emptying nvmap's pool gave.

    On unified memory the pool holds RAM that MemAvailable omits but the kernel can have back, so a load short of
    NEED first empties the pool and measures again; only the second figure decides. Refused: a RuntimeError with
    both figures, and nothing started."""
    release, measure = release or release_gpu_page_pool, measure or mem_available_bytes
    available = before = measure()
    if available < need and unified and release():
        available = measure()
    if available < need:
        raise RuntimeError(f'insufficient free host RAM: {available / 1024**3:.1f} GiB available, '
                           f'{need / 1024**3:.1f} GiB needed; no workload stopped')
    return available, max(0, available - before)


GPU_DEVICE_NODES = ('/dev/nvmap', '/dev/nvgpu', '/dev/nvidia0', '/dev/nvidiactl', '/dev/nvidia-uvm',
                    '/dev/nvidia-caps')


def holds_gpu_device(pids, proc=Path('/proc')):
    """On unified memory, whether a process of the unit opened the GPU. A CUDA context on a Jetson keeps
    /dev/nvmap open; on the DGX Spark's GB10 the driver is the ordinary one and the nodes are /dev/nvidia0,
    /dev/nvidiactl, /dev/nvidia-uvm, /dev/nvidia-uvm-tools, /dev/nvidia-modeset and /dev/nvidia-caps/ —
    measured on the Spark, where /dev/nvmap and /dev/nvgpu do not exist and `nvidia-smi
    --query-gpu=memory.total` answers `[N/A]`, which is also why the non-unified branch (nvidia-smi's compute
    apps) cannot be used there. A server whose engine never reached the GPU opens none of them."""
    for pid in pids:
        try:
            links = [os.readlink(fd) for fd in (proc / str(pid) / 'fd').iterdir()]
        except OSError:
            continue
        if any(link.startswith(node) for node in GPU_DEVICE_NODES for link in links):
            return True
    return False


def add_unified_memory_flags(engine_args, backend=None):
    """--mmap-experts on unified memory: the experts are read from the model files in place, so the GPU's expert
    cache holds them instead of a second, pinned copy in the same RAM. Upstream setup applies this only on a GB10
    (setup.py, is_gb10); its default on an Orin pins a large arena and sizes the cache from the same unified
    memory, leaving almost no free RAM.

    The name written is the one this engine takes (engine_option_names): a bare --mmap-experts is llama.cpp's
    boolean, and strata-dsv41 refuses it (`--mmap-experts needs a value`). A manifest that says null for it
    gets nothing added, matching upstream's Spark defaults for this family.
    """
    name = engine_option_names(backend)['mmap_experts']
    if name is None:
        return
    flag = name.split(' ', 1)[0]
    if flag not in engine_args:
        engine_args.extend(name.split(' '))


def has_owned_gpu_process(output, pids):
    for line in output.splitlines():
        try:
            pid, memory = (int(value.strip()) for value in line.split(','))
        except ValueError:
            continue
        if pid in pids and memory > 1024:
            return True
    return False


HEADROOM_GIB = 4  # free RAM left beside a backend whose manifest gives its own need
POOL_WORKERS_DEFAULT = 8  # the count every start wrote before it was configured per host
POOL_WORKERS_CAP = 32     # upstream's kPoolWorkersCap (setup.py, include/strata/kernels/cpu/pool_plan.hpp)
CPU_QUOTA_DEFAULT = 800   # the CPUQuota every start-unit had before it was configured per host: eight cores
CPU_QUOTA_CAP = 4000      # no host of ours needs more than 16 cores' worth of CPU time
RESERVE_DEFAULT = 700     # upstream's own --vram-reserve-mib default: what the engine leaves free for other programs
RESERVE_MAX = 65536       # no card of ours is near this; a typo must not ask for the whole address space


def require_backend(path):
    """The manifest this start is declared to serve. Nothing here starts a root it cannot name.

    F10: with the 0.1.45 roots gone the branch that started them had no caller left - `claim` resolves a
    dedicated worker's `strata_backend` or a borrowed host's `strata_default_backend` and refuses a host that
    names neither (scripts/borrow.py), and `strata-comparison.py`, the one caller that omitted --backend,
    refuses the retired Q2_0 manifest before it reaches a host. A stale root is not a default any more, so a
    missing manifest is an error rather than a fallback.
    """
    if path is None:
        raise SystemExit('strata-start-worker.py needs --backend <manifest.json>: the 0.1.45 root it used to '
                         'start without one is retired, and no host has a host-wide root to fall back to')
    return Path(path)


def load_backend(path):
    backend = json.loads(require_backend(path).read_text())
    backend.setdefault('contexts', [backend['context']])
    return backend


def serve_command(root, runtime, port, properties, unit):
    """How the serve process is launched: confined HOME and Strata settings, and no acquisition at start
    (upstream 0.2.8 gates install/build/download behind an interactive acquire; a start must never do any)."""
    account = pwd.getpwuid(os.getuid()).pw_name   # the service account control logged in as (D8.2)
    return ['sudo', '-n', 'systemd-run', f'--unit={unit}', f'--uid={account}',
            *properties, '--setenv=HOME=' + str(root / 'home'),
            '--setenv=XDG_CONFIG_HOME=' + str(root / 'home' / '.config'),
            '--setenv=STRATA_SETTINGS_FILE=' + str(root / 'home' / 'strata-settings.json'),
            '--setenv=STRATA_NO_ACQUIRE=1',
            '--setenv=PYTHONDONTWRITEBYTECODE=1', '--working-directory=' + str(root / 'source'),
            str(root / '.venv/bin/python'), str(root / 'source/serve/server.py'),
            '--engine', 'strata', '--host', '127.0.0.1', '--port', str(port), '--config', str(runtime)]


def cpu_quota_percent(backend, node) -> int:
    """Write this host's cgroup CPU quota, from the manifest's worker count.

    It is a property of the CPU, like `pool_workers`: the engine pins one CPU expert worker to each real core
    (upstream plans one per physical core of the host's socket, minus the core the engine's own thread keeps), and
    the unit's cap has to cover those cores. `CPUQuota=800%` - what every start-unit always had, right for the
    eight-worker pool every root always had - throttles the RTX 4080 host's i7-13700F, whose 16 physical cores upstream plans at 15 workers, to eight
    cores' worth of CPU time: the workers stay pinned to cores they are not allowed to run on and the decode slows
    with no error anywhere. The RTX 3090 host (i9-11900, 8c/16t, 7 workers) keeps the 800% it has."""
    asked = backend['workers'][node].get('cpu_quota_percent', CPU_QUOTA_DEFAULT)
    if isinstance(asked, bool) or not isinstance(asked, int) or not 100 <= asked <= CPU_QUOTA_CAP:
        raise RuntimeError(f'invalid cpu_quota_percent for {node}: {asked!r} (want 100..{CPU_QUOTA_CAP})')
    return asked


def check_cpu_quota(pool_workers, quota: int) -> None:
    """Refuse a start whose cgroup cannot feed the pool the engine pins: below one core per worker, throttling
    starves the experts. The engine's own thread shares those cores rather than asking for a further one.

    None is a start the engine runs with its own thread plan (no --pool-workers handed to it): there is no pool
    count to feed, and CPUQuota still bounds the process."""
    if pool_workers is None:
        return
    if quota < pool_workers * 100:
        raise RuntimeError(f'CPUQuota {quota}% cannot feed {pool_workers} CPU expert workers '
                           f'(needs at least {pool_workers * 100}%)')


def resource_properties(backend, node, claim):
    """systemd-run properties: a RAM cap only for a backend that keeps one."""
    properties = [f'--property=CPUQuota={cpu_quota_percent(backend, node)}%']
    if backend.get('memory_cap', True):
        properties.append('--property=MemoryMax=' + backend['workers'][node]['memory_max'])
    else:
        properties.append('--property=OOMScoreAdjust=1000')
    return properties + ['--property=MemorySwapMax=0', '--property=LimitMEMLOCK=infinity',
                         '--property=RuntimeMaxSec=' + ('12h' if claim else '4h'), '--property=NoNewPrivileges=true',
                         '--property=KillMode=control-group']


def required_bytes(backend, node):
    need = backend['workers'][node]['min_available_gib']
    return (need + HEADROOM_GIB) * 1024**3


def set_context(engine_args, prepared, wanted):
    """Check the prepared --max-context, then serve WANTED (at most the prepared one)."""
    if '--max-context' not in engine_args or engine_args[engine_args.index('--max-context') + 1] != str(prepared):
        raise RuntimeError('prepared context differs')
    if wanted > prepared:
        raise RuntimeError('context above the prepared one')
    engine_args[engine_args.index('--max-context') + 1] = str(wanted)


def set_flag(engine_args, flag, value):
    """Set one engine flag in an argument list, appending it when the list does not carry it."""
    if flag in engine_args:
        engine_args[engine_args.index(flag) + 1] = value
    else:
        engine_args += [flag, value]


QWEN_ENGINE_OPTIONS = {'vram_reserve_mib': '--vram-reserve-mib', 'pool_workers': '--pool-workers',
                       'mmap_experts': '--mmap-experts'}


def engine_option_names(backend):
    """What this engine calls the settings we control: {setting: flag name}, None where it has no flag for it.

    A manifest with no `engine_options` is a root of the kind this project has always started, and gets the names
    every one of those roots carries. The DeepSeek engine is a different program with its own option set, and
    upstream's rule for it is explicit (apps/host/server/native-control.mjs:319: "Only the options strata-dsv41
    takes go to it (#4504): --vram-reserve-mib is its --reserve-mib, and --pool-workers and the other Qwen-engine
    names are left out, in the arguments and in the person's saved options"; apps/host/server/native-ds-args.mjs
    holds the allowed list and `DS_RENAMED`). serve/server.py passes the config's args to the engine as they are,
    so the leaving-out is ours to do — otherwise the engine fails at start with `unknown option --pool-workers`.
    """
    merged = dict(QWEN_ENGINE_OPTIONS)
    merged.update({key: value for key, value in (backend or {}).get('engine_options', {}).items()
                   if key in QWEN_ENGINE_OPTIONS})
    return merged


def drop_flag(args, flag):
    """Remove an option and its value from an engine command line (the options we hand carry exactly one value)."""
    while flag in args:
        at = args.index(flag)
        end = at + 2 if at + 1 < len(args) else at + 1
        del args[at:end]


def set_user_flag(cfg, flag, value):
    """Set the same flag in the config's "user_args" when it carries one.

    serve/server.py:3166 merges cfg["user_args"] over cfg["args"] at every start: those are "the options the person
    set by hand", and setup stores --vram-reserve-mib there precisely so it survives every later setup and start
    (setup.py:7024). Rewriting only "args" therefore leaves the prepared value in force when `user_args` also
    carries the flag.
    """
    if isinstance(cfg.get('user_args'), list) and flag in cfg['user_args']:
        set_flag(cfg['user_args'], flag, value)


def engine_flags(backend):
    """The engine options a claim may name by hand: only what the manifest read off the engine's own usage.

    A manifest with no `engine_flags` cannot be overridden at all. That is deliberate: `engine_flags` records
    the options read off the engine's own usage, and a manifest without that list is not a place to add a
    free-form flag. The list is also the reason a wrong name is refused before anything starts rather than dying
    as `unknown option --…` exit 2.
    """
    return (backend or {}).get('engine_flags') or {}


def check_engine_option(backend, name):
    """Refuse an engine option this manifest does not name, and name the list it does.

    The list is `engine_flags`, which is what the manifest read off the engine's own usage. A manifest with no list
    cannot be given options at all — that is every Qwen manifest, i.e. every host a route serves. A wrong name dies
    here rather than as `unknown option --…` exit 2 in an engine that has already been started.
    """
    allowed = engine_flags(backend)
    if not allowed:
        raise SystemExit(f"{backend.get('id', '?')} declares no engine_flags, so no engine option can be added to a "
                         'claim of it: measure what the engine takes and declare it in the manifest first')
    if name not in allowed:
        raise SystemExit(f'engine option {name}: {backend.get("id")} names no such engine option. The list this '
                         'manifest read off the engine is: ' + ', '.join(sorted(allowed)))


def manifest_engine_options(backend):
    """What this manifest declares the engine to run with: `engine_settings`, in the engine's own option names.

    This is the serving declaration, and it is why the Spark's Coder serves 6 experts per word rather than the
    engine's own `auto`: `--experts auto` prefers Faster (5 of 6) on an integrated / unified-memory GPU
    (strata_cli/launch/runner.py:469-491) and the Spark's GB10 is probed as `uma` with 115.7 GiB — measured on the
    host, where `faster_default(115.7)` is False, so the integrated branch fired and not the small-card one. Upstream
    writes an explicit choice the same way (runner.py:508: "--experts 5 (Faster) or --experts 6 (Full quality),
    replacing one already in the saved args"; runner.py:2555: "only an explicit choice is written as --experts").
    """
    pairs = []
    for name, value in (backend.get('engine_settings') or {}).items():
        check_engine_option(backend, name)
        pairs.append(('--' + name.replace('_', '-'), str(value)))
    return pairs


def parse_engine_options(backend, raw):
    """--engine-option NAME=VALUE, repeatable; a claim's own addition, checked against the same list.

    A measurement knob, not a serving knob: it is how one root is read both ways (docs/dsv41-spark-plan.md: the
    engine's `experts auto` picks 5 of the model's 6 experts per word, and re-declaring that as a second manifest
    would mean a second 177.6 GiB download for one flag). A claim's option wins over the manifest's own setting,
    because the claim is the narrower statement.
    """
    pairs = []
    for item in raw or []:
        name, _, value = item.partition('=')
        name, value = name.strip(), value.strip()
        if not value:
            raise SystemExit(f'--engine-option {item!r}: NAME=VALUE, e.g. --engine-option experts=6')
        check_engine_option(backend, name)
        pairs.append(('--' + name.replace('_', '-'), value))
    return pairs


def apply_engine_options(cfg, engine_args, pairs):
    """Put them where serve/server.py will actually read them: `args`, and `user_args` too.

    serve/server.py:3166 merges cfg["user_args"] over cfg["args"] at every start, so a value written only in `args`
    is overridden by whatever the prepared root stored in `user_args`. Returns what was applied, for the state file.
    """
    for flag, value in pairs:
        set_flag(engine_args, flag, value)
        set_user_flag(cfg, flag, value)
    return {flag.removeprefix('--').replace('-', '_'): value for flag, value in pairs}


def hand_engine_option(cfg, setting, name, value):
    """Put one setting on the engine command line, in the name this engine takes, and leave nothing behind in the
    person's saved options.

    `name` None means this engine has no such option: the Qwen flag comes out of `args` and out of `user_args`,
    because serve/server.py merges user_args over args at every start and leaving it there would hand the unknown
    option back to the engine (upstream says the same in native-control.mjs:319 — the Qwen names are left out "in
    the arguments and in the person's saved options"). Otherwise the Qwen name is renamed in both, so a root
    prepared with `--vram-reserve-mib` in its saved options starts with `--reserve-mib` instead of refusing it."""
    legacy = QWEN_ENGINE_OPTIONS[setting]
    for where in (cfg['args'], cfg.get('user_args') if isinstance(cfg.get('user_args'), list) else None):
        if where is None:
            continue
        if name is None:
            drop_flag(where, legacy)
        elif name != legacy and legacy in where:
            where[where.index(legacy)] = name
    if name is None:
        return None
    set_flag(cfg['args'], name, value)
    if isinstance(cfg.get('user_args'), list) and name in cfg['user_args']:
        set_flag(cfg['user_args'], name, value)
    return name


def digest(path):
    """SHA-256 of a file, read in 8 MiB blocks. Used on the drafter pack (0.52 GB), never on a model."""
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def set_drafter(cfg, backend, node, root):
    """Write the declared drafter into the config the front reads, and refuse when this root cannot serve it.

    serve/server.py applies cfg["drafting"] at every start (strata_control/drafting.py, engine_form/engine_flags):
    dflash2 becomes `--draft dflash2 --draft-model <pack> --draft-fallback none --mtp-draft-vocab <file> --spec 6
    --draft-conf 0.35` with no `--mtp`, because Strata runs one drafter per run. What it does when the file is not
    there is fall back - to the MTP if the root holds the draft layer, to nothing if it does not - and say why in one
    log line (`drafting.apply` silently falls back to `drafter='mtp'` when a DFlash2 pack is missing). A claim
    that silently served the MTP would then be measured as if it were DFlash2, so the absence is a refusal here.
    The check is the pack's own size and SHA-256 from the manifest, not a full model read.
    """
    asked = backend['workers'][node].get('drafter')
    if not asked:
        return None
    if asked != 'mtp':
        declared = backend.get('drafter_pack')
        if not declared:
            raise RuntimeError(f"{backend['id']} declares workers.{node}.drafter = {asked} but names no drafter_pack")
        pack = root / 'data' / declared['path']
        if not pack.is_file():
            raise RuntimeError(f'{backend["id"]} asks {node} to drafter with {asked} and this root holds no '
                               f'{declared["path"]}: prepare it, or claim a backend whose drafter this root serves')
        if pack.stat().st_size != declared['size'] or digest(pack) != declared['sha256']:
            raise RuntimeError(f'the drafter pack this manifest pins is not what the root holds: {declared["path"]}')
    cfg['drafting'] = asked
    return asked


def set_pool_workers(cfg, backend, node):
    """Write this host's CPU expert worker count into the engine arguments.

    The manifest carries it per host because it is a property of the CPU, not of the model: upstream plans one
    worker per physical core of the host's socket minus the core the host thread keeps (setup.py:plan_pool_workers,
    which mirrors include/strata/kernels/cpu/pool_plan.hpp). The RTX 3090 host (i9-11900, 8c/16t) is planned at 7, and setup
    itself warns about 8 there: "more than its physical cores: extra workers can make Strata slower". The RTX 4080 host
    (i7-13700F, 16c/24t) is planned at 15. A manifest that says nothing keeps POOL_WORKERS_DEFAULT, which is
    the count every root of ours was started with before it was written per host."""
    asked = backend['workers'][node].get('pool_workers', POOL_WORKERS_DEFAULT)
    if isinstance(asked, bool) or not isinstance(asked, int) or not 1 <= asked <= POOL_WORKERS_CAP:
        raise RuntimeError(f'invalid pool_workers for {node}: {asked!r} (want 1..{POOL_WORKERS_CAP})')
    name = hand_engine_option(cfg, 'pool_workers', engine_option_names(backend)['pool_workers'], str(asked))
    return None if name is None else asked


def set_vram_reserve(cfg, backend, node):
    """Write this host's GPU reserve into the engine arguments, down from what preparation was given.

    --vram-reserve-mib is VRAM the engine keeps free for *other programs* ("a game, another model", upstream's
    help for it; default 700 MiB), and the expert cache takes exactly that much less. Keeping an unnecessarily
    large reserve pushes displaced experts into page-locked host RAM.

    The value is written rather than checked because setup only records a reserve it was handed, so a root
    prepared with a larger reserve would otherwise keep it until re-prepared. Raising a prepared reserve is still
    refused: that is the direction which can leave "no VRAM ... for the expert cache" and fail the start.

    A manifest whose engine has no reserve option gets the flag taken out instead of renamed: setup.py's own rule for
    DeepSeek is that `--reserve-mib` is for a card with memory of its own and "nothing on a Mac or a unified-memory
    APU" (setup.py:9041-9044), and strata-dsv41 refuses `--vram-reserve-mib` outright (`unknown option`)."""
    asked = backend['workers'][node].get('vram_reserve_mib', RESERVE_DEFAULT)
    if isinstance(asked, bool) or not isinstance(asked, int) or not 0 <= asked <= RESERVE_MAX:
        raise RuntimeError(f'invalid vram_reserve_mib for {node}: {asked!r} (want 0..{RESERVE_MAX})')
    engine_args = cfg['args']
    prepared = (int(engine_args[engine_args.index('--vram-reserve-mib') + 1])
                if '--vram-reserve-mib' in engine_args else RESERVE_DEFAULT)
    if asked > prepared:
        raise RuntimeError('prepared GPU headroom differs')
    name = hand_engine_option(cfg, 'vram_reserve_mib', engine_option_names(backend)['vram_reserve_mib'], str(asked))
    return None if name is None else asked


def add_engine_env(cfg, backend, node):
    """Merge the manifest's per-host engine environment (e.g. STRATA_RESIDENT_HEADROOM_GIB) into the config."""
    env = backend['workers'][node].get('env')
    if env:
        cfg['env'] = {**(cfg.get('env') or {}), **{str(k): str(v) for k, v in env.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True, help='the host this start is for; the backend manifest decides which hosts it has settings for')
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--attempt', choices=('v3', 'bfcl-v1', 'context-v1', 'pi-v1', 'claude-v1', 'codex-v1', 'claude-read-v1', 'lease-v1', 'lease-diagnostic-v1', 'lease-loop-v1', 'pi-lease-v1', 'claim'), default='v3',
                        help='claim: agentiac claim lifecycle; repeatable, reuses the key the control host pushed')
    parser.add_argument('--port', type=int, default=18085, help='API port; the on-demand front moves it to an internal port')
    parser.add_argument('--unit', required=True, help="the engine's transient unit, as control names it: "
                                                       '<resource_prefix>-strata-inference (D6.8)')
    parser.add_argument('--backend', type=Path,
                        help='backend manifest prepared in ROOT; a start always names the manifest its declaration names')
    parser.add_argument('--context', type=int, help="served context, one of the manifest's contexts (default: prepared)")
    parser.add_argument('--engine-option', action='append', metavar='NAME=VALUE',
                        help="one engine option this claim adds, NAME as the engine spells it without the dashes; "
                             "only names the manifest's engine_flags lists are accepted")
    args = parser.parse_args()
    root = args.root.resolve()
    os.umask(0o077)
    backend = load_backend(args.backend)
    if args.node not in backend['workers']:
        raise SystemExit(f"{backend['id']} has no worker settings for {args.node}; it declares "
                         + ', '.join(sorted(backend['workers'])))
    context = args.context or backend['context']
    if context not in backend['contexts']:
        raise RuntimeError(f'context {context} is not one of {backend["contexts"]}')
    prefix = f"{backend['id']}-{context // 1024}k-{args.attempt}"
    prepared = json.loads((root / f"prepare-{backend['id']}-state.json").read_text())
    if prepared['stage'] != 'prepared':
        raise RuntimeError('preparation is not complete')
    state = root / f'start-{prefix}-state.json'
    claim = args.attempt == 'claim'
    if state.exists() and not claim:
        raise SystemExit('Saved start state exists; inspect service/artifacts before repeating')
    applied_options, engine_option_sources = {}, {}   # what the engine is being started with, and where each came from

    def record(stage, **fields):
        # Every stage writes the state whole, so what a start carries belongs in the base: a failed start is read
        # afterwards, and the stage that failed is the one someone needs to know how it was started.
        temporary = state.with_suffix('.tmp')
        temporary.write_text(json.dumps({'stage': stage, 'updated': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                        'engine_options': applied_options or None,
                                        'engine_option_sources': engine_option_sources or None, **fields}, indent=2) + '\n')
        temporary.replace(state)
    unified = unified_memory(backend, args.node)
    available, pool_released = admit_host_ram(required_bytes(backend, args.node), unified)
    used = None  # on unified memory the RAM check above is the GPU check too
    if not unified:
        gpu = subprocess.check_output(['nvidia-smi', '--query-gpu=memory.used,memory.total', '--format=csv,noheader,nounits'], text=True).strip().splitlines()
        if len(gpu) != 1:
            raise RuntimeError('expected one GPU')
        used, total = (int(x.strip()) for x in gpu[0].split(','))
        if used > 1024:
            raise RuntimeError('another GPU workload is present; no workload stopped')
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # closed client connections linger in TIME_WAIT
        probe.bind(('127.0.0.1', args.port))
    configs = [Path(prepared['config'])]
    cfg = json.loads(configs[0].read_text())
    for path in [str(a) for a in cfg.get('args', [])
                 if str(a).startswith('/') and str(a).endswith(('.gguf', '.bin'))]:
        if not Path(path).is_relative_to(root):   # a start serves the weights this root prepared, never another's
            raise RuntimeError('prepared config names a model file outside the root: ' + path)
    import sys
    sys.path.insert(0, str(root / 'source'))
    from serve.server import clean_shared_defaults
    clean_shared_defaults({'reasoning_effort': 'none'})
    key_file = root / f'api-key-{args.attempt}.env'
    if claim:
        key = key_file.read_text().split('=', 1)[1].strip()
    else:
        key = secrets.token_urlsafe(48)
        key_file.write_text('LITELLM_MASTER_KEY=' + key + '\n')
    cfg.update(host='127.0.0.1', port=args.port, api_key=key, reasoning_effort='none')
    add_engine_env(cfg, backend, args.node)
    engine_args = cfg['args']
    set_context(engine_args, backend['context'], context)
    reserve = set_vram_reserve(cfg, backend, args.node)
    pool_workers = set_pool_workers(cfg, backend, args.node)
    drafter = set_drafter(cfg, backend, args.node, root)
    for pairs, whence in ((manifest_engine_options(backend), 'manifest'),      # the declaration first,
                          (parse_engine_options(backend, args.engine_option), 'claim')):   # so a claim's word wins
        for flag, value in pairs:
            engine_option_sources[flag.removeprefix('--').replace('-', '_')] = whence
        applied_options.update(apply_engine_options(cfg, engine_args, pairs))
    if unified:
        add_unified_memory_flags(engine_args, backend)
    quota = cpu_quota_percent(backend, args.node)
    check_cpu_quota(pool_workers, quota)
    runtime = root / f'runtime-{prefix}-config.json'
    runtime.write_text(json.dumps(cfg, indent=2) + '\n')
    safe_cfg = {k: v for k, v in cfg.items() if k != 'api_key'}
    (root / f'runtime-{prefix}-public.json').write_text(json.dumps(safe_cfg, indent=2) + '\n')
    record('starting', mem_available_before=available, gpu_page_pool_released=pool_released,
           gpu_used_before_mib=used, context=context,
           pool_workers=pool_workers, vram_reserve_mib=reserve, cpu_quota_percent=quota, drafter=drafter,
           runtime_public_sha256=hashlib.sha256((root / f'runtime-{prefix}-public.json').read_bytes()).hexdigest())
    properties = resource_properties(backend, args.node, claim) + (gpu_page_pool_properties() if unified else [])
    command = serve_command(root, runtime, args.port, properties, args.unit)
    subprocess.run(['sudo', '-n', 'systemctl', 'reset-failed', args.unit], capture_output=True)
    subprocess.run(command, check=True)
    deadline = time.monotonic() + 1200
    missing_cuda = 0
    while time.monotonic() < deadline:
        status = subprocess.check_output(['systemctl', 'show', args.unit, '-p', 'ActiveState', '--value'], text=True).strip()
        if status == 'failed':
            record('failed', reason='inference service failed')
            raise SystemExit(1)
        try:
            request = urllib.request.Request(f'http://127.0.0.1:{args.port}/health', headers={'Authorization': 'Bearer ' + key})
            with urllib.request.urlopen(request, timeout=10) as response:
                health = json.load(response)
            if health.get('loaded') and health.get('max_context') == context and health.get('api_key'):
                group = subprocess.check_output(['systemctl', 'show', args.unit, '-p', 'ControlGroup', '--value'], text=True).strip()
                pids_path = Path('/sys/fs/cgroup') / group.lstrip('/') / 'cgroup.procs'
                pids = {int(p) for p in pids_path.read_text().split()}
                if unified:
                    owned = holds_gpu_device(pids)
                else:
                    gpu_output = subprocess.check_output(['nvidia-smi', '--query-compute-apps=pid,used_gpu_memory', '--format=csv,noheader,nounits'], text=True)
                    owned = has_owned_gpu_process(gpu_output, pids)
                if not owned:
                    missing_cuda += 1
                    if missing_cuda >= 3:
                        record('failed', reason='loaded API has no owned CUDA inference process')
                        raise SystemExit(1)
                    time.sleep(5)
                    continue
                record('ready', health=health, owned_cuda_process=True)
                print(json.dumps({'node': args.node, 'stage': 'ready', 'health': health}), flush=True)
                return
        except (OSError, ValueError):
            pass
        time.sleep(5)
    record('failed', reason='ready timeout; inspect service before taking further action')
    raise SystemExit(1)


if __name__ == '__main__':
    main()
