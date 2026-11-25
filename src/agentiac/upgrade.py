"""What an engine upgrade has to move, and which prepared root it can copy its weights from.

Two questions have to be answered on every Strata upgrade: *which declarations move together*, and *does this
host already hold the bytes the new manifest pins*. Reusing an existing full root avoids re-downloading tens of
GB of identical weights when consecutive engine versions pin the same SHA-256. This module performs that reading
in code.

It reads and refuses. It never prepares, deploys, claims or deletes anything:

  manifest   the backend manifest must name the host in `workers`. `infra/tasks/prepare-strata.yml` asserts the
             same thing before it copies anything; this says it before the playbook is even run, and says what is
             missing rather than failing an assertion.
  root       the preparation chooses a root by `strata_trial_roots[manifest id]` or `[source_version]` (the
             playbook's rule, mirrored here). A manifest with no declared root on this host has nowhere to go.
  weights    a reuse candidate is a root this host's ledger calls `full` whose own manifest pins every byte the
             target asks a reuse root for, by name, size and SHA-256 — the check
             scripts/strata-prepare-worker.py performs before it copies. A `thin` root is not a source: it holds
             no weights.
  ledger     the candidate comes from a `serving` / `rollback` / `candidate` line of `infra/retention.yml`, so the
             same file the retirement gates read is what says a root is there and full. A manifest whose roots the
             ledger does not name — a retired pin, for instance — never becomes a source.
  decision   `ops prepare` has to say which way the weights come: `--reuse-root PATH`, `--reuse-declared` (this
             module's candidate, when there is exactly one), or `--download` to mean it. With a candidate on the
             host and no decision given, the preparation refuses and names the candidate.

`plan` is the reading a person uses before the first commit: the current pin, what the ledger says the host holds,
what the new manifest pins, the reuse answer, and the ordered list of declarations and commands with every fact it
got from a declared file. Where it cannot answer it says `unknown` and names the command that would read it, in the
same shape `doctor` uses.
"""
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request

import yaml

from agentiac import backend_names, declared
from agentiac.errors import explain
from agentiac.errors import command as cli_name

GIB = 2 ** 30
LEDGER_STATES = ('serving', 'rollback', 'candidate')
MEASURE = ('claim with no --route, then the ladder and the Suite on the alias of this backend',
           f'{cli_name()} ops deploy, then {cli_name()} claim HOST (no --route unless the owner moves the route)',
           f'{cli_name()} warm HOST, {cli_name()} ops drift (front identity), {cli_name()} doctor')


def weight_entries(manifest):
    """The weight files a manifest pins, wherever that manifest keeps them.

    A Strata manifest lists them flat (`files`). Spark's TensorFold manifest - and every engine manifest written in
    the nested `recipe`/`engine`/`image`/`model` shape since - carries them under `model.files`. Reading only the flat
    key is not a harmless omission: `backends/tf-1.1.4-qfn-mxfp8-nvfp4.json` pins 32 files totalling
    102,262,310,123 bytes, and every weight read that asks the manifest answered "no file, no bytes" about a
    95 GiB checkpoint the host actually holds. F6 ("a root without its weights does not start") and
    `ops upgrade reuse`'s byte count both read through here, so they say the same thing about a nested manifest as
    about a flat one.

    Not every nested entry is hash-pinned: 9 of those 32 name a size and no `sha256`. The hash stays optional here -
    a size is still a pin, and a missing hash is an unknown to report rather than a refusal. Where two manifests are
    compared (`supplies`), any missing hash on either side (including both sides unhashed) reads as a mismatch: only a
    hash-bearing pin on both sides can prove the bytes are the same file, and pretending otherwise would let a reuse
    copy past a check it never passed.
    """
    model = manifest.get('model')
    raw = manifest.get('files') or (model.get('files') if isinstance(model, dict) else None) or []
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw
            if isinstance(entry, dict) and isinstance(entry.get('path'), str) and entry['path']
            and isinstance(entry.get('size'), int) and not isinstance(entry.get('size'), bool)
            and entry['size'] >= 0]


def unhashed_weights(manifest):
    """The pinned weight entries that name a size and no SHA-256: what a size-only pin cannot prove."""
    return [entry['path'] for entry in weight_entries(manifest) if not entry.get('sha256')]


def pinned(manifest):
    """What a manifest says a prepared root holds: {path: (bytes, sha256)} — where the worker writes the weight."""
    return {entry['path']: (int(entry['size']), entry.get('sha256') or None) for entry in weight_entries(manifest)}


def asks(manifest):
    """What a manifest asks a `--reuse-root` to hold: {same_as: (bytes, sha256)} — where it reads one.

    scripts/strata-prepare-worker.py looks for a candidate at `reuse_root/data/models/<same_as>` and copies it to
    `root/data/models/<path>`, checking size first and SHA-256 before and after. The two names may differ (a
    quantization filed under another name); only the bytes have to agree, which is why the comparison below is a
    coverage check rather than an equality of sets. The declared manifests use the same name on both sides.
    """
    return {entry.get('same_as') or entry['path']: (int(entry['size']), entry.get('sha256') or None)
            for entry in weight_entries(manifest)}


def weight_bytes(manifest):
    return sum(size for size, _ in pinned(manifest).values())


def supplies(source, target):
    """Can the root SOURCE pins give what TARGET asks a reuse root to hold? Returns the gaps, by name."""
    held = pinned(source)
    missing = sorted(name for name in asks(target) if name not in held)
    mismatched = sorted(
        name for name, want in asks(target).items()
        if name in held and (want[1] is None or held[name][1] is None or held[name] != want)
    )
    return missing, mismatched


def strata_shaped(manifest):
    """Whether this is the manifest shape `ops upgrade plan` and `ops upgrade reuse` read.

    Those two commands take the engine from `source_version`/`source_revision`/`ggml_revision` and the root from
    `infra/inventory.yml <host> strata_trial_roots`, and their steps are the Strata preparation: a root on the host,
    a drafter pack, a `components.yml` pin moved to a manifest lock. A manifest in the nested
    `recipe`/`engine`/`image`/`model` shape has none of those keys: it prepares an image or a container, not a root.
    Saying which shape a manifest is lets those commands refuse in words instead of printing `0.0 GiB` for a 95 GiB
    checkpoint (reuse) or dying in the formatter (plan).
    """
    return bool(manifest.get('source_version') or manifest.get('source_revision') or manifest.get('files'))


def container_shaped(manifest):
    """Whether this manifest declares a container-built engine (`build` + `engine`)."""
    return isinstance(manifest.get('build'), dict) and isinstance(manifest.get('engine'), dict)


def not_strata_shaped(host, backend_id, manifest):
    """The refusal `ops upgrade plan|reuse` gives a manifest it does not read, with what does read it."""
    blocks = [key for key in ('recipe', 'engine', 'image', 'model') if manifest.get(key)]
    shape_note = (f'it declares {", ".join(blocks)}, which is the shape of an engine that serves from an image or a '
                  f'container rather than from a prepared root' if blocks
                  else 'it declares none of recipe/engine/image/model either')
    return explain(
        f'backends/{backend_id}.json is not the kind of manifest {cli_name()} ops upgrade plan|reuse reads',
        ['those commands read the engine from `source_version`, `source_revision` and `ggml_revision` and the root '
         f'from infra/inventory.yml {host} `strata_trial_roots`; this manifest declares none of them',
         shape_note],
        f'{cli_name()} model show <target> for what is declared, {cli_name()} ops storage {host}, '
        f'{cli_name()} ops prepare {host} --check, {cli_name()} ops drift',
        f'a plan/reuse reading of an image-or-container engine is its own design: name it before bending these '
        f'commands to a manifest they were not written for')


def manifest_for(graph, backend_id):
    """The manifest named BACKEND_ID: by file name, by a retired file name (backends/legacy-names.yml) or by the id
    inside it. Refuses a name none of them has."""
    from agentiac import backend_names
    backend_id = backend_names.resolve(graph.backends, backend_names.legacy(graph.root), backend_id)
    if backend_id in graph.backends:
        return graph.backends[backend_id]
    for manifest in graph.backends.values():
        if manifest.get('id') == backend_id:
            return manifest
    raise SystemExit(f'no backend manifest names {backend_id}: `ls backends/` and check the id inside the file')


def name_of(graph, manifest):
    """MANIFEST's file name: what the ledger, the inventory and `--backend` call it (its `id` may predate the name)."""
    for stem, spec in graph.backends.items():
        if spec is manifest:
            return stem
    return manifest['id']


def trial_root(graph, host, manifest):
    """The root `infra/tasks/prepare-strata.yml` would prepare this manifest into on HOST.

    The playbook prefers the manifest's file name as the `strata_trial_roots` key, then the id inside it, and falls
    back to `source_version`; the same order here (and in scripts/borrow.py strata_root) is what makes `ops prepare`,
    `ops upgrade plan` and a claim agree about where a root belongs.
    """
    roots = graph.nodes.get(host, {}).get('strata_trial_roots') or {}
    for key in (name_of(graph, manifest), manifest['id']):
        if key in roots:
            return key, roots[key]
    if manifest.get('source_version') in roots:
        return manifest['source_version'], roots[manifest['source_version']]
    return None, None


def root_owner(graph, host, manifest, root):
    """Another backend's ledger line on HOST that already owns ROOT, as (state, backend), or None.

    `trial_root` prefers the manifest id as the `strata_trial_roots` key and falls back to the engine version, so a
    manifest whose id key is gone resolves to the root its engine version names - and when this host holds that
    version as another backend, that root is somebody else's. Overwriting it is not a preparation: it moves what the
    host serves or what its rollback is (ADR 0008 §2.2), which is why `prepare_gate` refuses it and `plan` blocks it.
    """
    for state, entry in ledger_lines(graph, host):
        backend, path = entry.get('backend'), entry.get('path')
        if backend and backend != name_of(graph, manifest) and path and path == root:
            return state, backend
    return None


def ledger_lines(graph, host):
    """Every (state, line) the retention ledger holds for HOST, oldest state first."""
    held = graph.retention.get(host) or {}
    return [(state, entry) for state in LEDGER_STATES for entry in (held.get(state) or [])]


def ledger_line(graph, host, backend_id):
    for state, entry in ledger_lines(graph, host):
        if entry.get('backend') == backend_id:
            return state, entry
    return None, None


def pinned_by(graph, host):
    """The `infra/components.yml` entry that pins HOST's current `strata_backend`.

    `src/agentiac/declared.py` refuses a host whose `strata_backend` is pinned by a component that does not name
    that host, so exactly one entry answers here once the declaration is consistent.
    """
    backend = graph.nodes.get(host, {}).get('strata_backend')
    for entry in graph.components:
        pin = entry.get('pin') or {}
        if backend and pin.get('lock') == f'backends/{backend}.json' and host in (entry.get('hosts') or []):
            return entry
    return None


def drafter_asked(manifest, host):
    """The drafter this manifest asks of one host, or None where it says nothing and the engine's own default rules."""
    return ((manifest.get('workers') or {}).get(host) or {}).get('drafter')


def drafter_pack(manifest, host):
    """The drafter pack the file that drafter needs, as this host's manifest pins it, or None for an MTP root.

    The drafter is a per-host choice (`workers.<host>.drafter`) because one card may run the draft layer while
    another is being measured with DFlash2; the pack itself is one file, pinned once in `drafter_pack`.
    """
    asked, pack = drafter_asked(manifest, host), manifest.get('drafter_pack')
    return None if not asked or asked == 'mtp' or not pack else {
        'drafter': asked, 'path': pack['path'], 'size': pack['size'], 'sha256': pack['sha256']}


def _container_reuse_candidates(graph, host, manifest):
    """Held backends on HOST that share the same verified loose `model.directory` and pinned bytes."""
    from agentiac import identities
    out, why_not = [], []
    model_dir = identities.model_directory(manifest)
    wanted = asks(manifest)
    if not model_dir or not wanted:
        return {'candidates': [], 'download_bytes': weight_bytes(manifest), 'why_not': [], 'packs': []}
    project_root = (graph.nodes.get(host) or {}).get('project_root') or (getattr(graph, 'vars', None) or {}).get('project_root') or '/srv/agentiac'
    shared_path = f"{project_root.rstrip('/')}/{model_dir}"
    for state, line in ledger_lines(graph, host):
        source_id = line.get('backend')
        if not source_id or source_id == name_of(graph, manifest):
            continue
        try:
            source = manifest_for(graph, source_id)
        except SystemExit as missing:
            why_not.append(f'{source_id} (ledger line {state}): the manifest is not in backends/ ({missing})')
            continue
        if identities.model_directory(source) != model_dir:
            continue
        missing, mismatched = supplies(source, manifest)
        if missing or mismatched:
            continue
        if line.get('form') != 'full':
            why_not.append(f'{source_id} ({state} line): shares {model_dir}, but the ledger calls it `form: {line.get("form")}`')
            continue
        out.append({'backend': source_id, 'state': state, 'root': shared_path,
                    'engine_version': (source.get('engine') or {}).get('version'),
                    'bytes': weight_bytes(manifest), 'files': len(wanted)})
    out.sort(key=lambda entry: (entry['engine_version'] or '', entry['backend']))
    return {'candidates': out, 'download_bytes': 0 if out else weight_bytes(manifest),
            'why_not': why_not, 'packs': []}


def reuse_candidates(graph, host, manifest):
    """Roots on HOST the ledger calls full whose manifest pins the identical bytes MANIFEST pins.

    Returns `{'candidates': [...], 'download_bytes': N, 'why_not': [...]}`. `download_bytes` is what a preparation
    without a reuse source pulls from upstream, and `why_not` names each root that matched on weights but is not a
    source — a `thin` line being the important one.
    """
    if container_shaped(manifest) and not strata_shaped(manifest):
        return _container_reuse_candidates(graph, host, manifest)
    out, why_not = [], []
    pack = drafter_pack(manifest, host)
    if not asks(manifest):
        return {'candidates': [], 'download_bytes': 0, 'why_not': [], 'packs': [pack] if pack else []}
    for state, line in ledger_lines(graph, host):
        source_id = line.get('backend')
        if not source_id or source_id == name_of(graph, manifest):
            continue
        try:
            source = manifest_for(graph, source_id)
        except SystemExit as missing:
            why_not.append(f'{source_id} (ledger line {state}): the manifest is not in backends/ '
                           f'({missing})')
            continue
        missing, mismatched = supplies(source, manifest)
        if missing or mismatched:
            continue
        if line.get('form') != 'full':
            why_not.append(f'{source_id} ({state} line, root {line.get("path")}): pins the same bytes, but the '
                           f'ledger calls it `form: {line.get("form")}` — a root that holds no weights cannot be '
                           f'copied from')
            continue
        out.append({'backend': source_id, 'state': state, 'root': line.get('path'),
                    'engine_version': source.get('source_version'), 'bytes': weight_bytes(manifest),
                    'files': len(asks(manifest))})
    out.sort(key=lambda entry: (entry['engine_version'] or '', entry['backend']))
    # A reuse answer that copies the weights is not the whole job: a drafter trial still fetches the pack its manifest
    # pins, and saying so here is what keeps a ladder reading honest about the one thing that changed.
    return {'candidates': out, 'download_bytes': weight_bytes(manifest), 'why_not': why_not,
            'packs': [pack] if pack else []}


def prepare_gate(graph, host, backend_id, reuse_root=None, reuse_declared=False, download=False):
    """What `ops prepare` is allowed to do about weights, or the refusal that stops it (T5.8, S §9 F12).

    Three answers: `('skip', None)` for a manifest that is not a Strata backend manifest (Spark's TensorFold names
    no `workers`, and a manifest that declares its container `build` fetches no weights here, so nothing is decided
    for either); `('reuse', path, source manifest id)` when the operator named
    a root that checks out or `--reuse-declared` picks the one candidate found; `('download', bytes)` when there is
    nothing to copy or the operator said `--download`. A candidate on the host and no decision given is a refusal.

    Returns `(manifest, trial root key, trial root path, answer)`.
    """
    manifest = manifest_for(graph, backend_id)
    if 'build' in manifest:   # the same test infra/prepare.yml makes (`manifest.build is defined`)
        # A declared container build is prepared from that declaration (scripts/container-prepare.py), which fetches
        # no weights: asking it the Strata trial-root question would refuse it for a root it never uses.
        return manifest, None, None, ('skip', None)
    if not manifest.get('files'):
        # Deliberately the flat key, not `weight_entries`: this gate decides the *Strata trial-root* preparation, and
        # a manifest in the nested shape has no trial root to prepare - its weights arrive with an image or a
        # container through its own prepare path (`infra/tasks/prepare-tensorfold.yml` today). Making it ask
        # `strata_trial_roots` about an image engine would be the wrong refusal; the right reading of such a manifest
        # is the multi-engine dispatch that does not exist yet.
        return manifest, None, None, ('skip', None)   # a manifest that pins no weights has nothing to decide
    if host not in (manifest.get('workers') or {}):
        raise SystemExit(f'backends/{backend_id}.json does not name {host} in `workers`: the preparation of a '
                         f'backend on a host starts by declaring that host (the playbook asserts the same thing)')
    key, root = trial_root(graph, host, manifest)
    if root is None:
        raise SystemExit(explain(f'{backend_id} has no declared root on {host}',
                                 [f'infra/inventory.yml {host} `strata_trial_roots` holds '
                                  f'{sorted(graph.nodes.get(host, {}).get("strata_trial_roots") or {})}',
                                  f'the playbook needs a key equal to the manifest file name ({name_of(graph, manifest)}) or its '
                                  f'engine version ({manifest.get("source_version")})'],
                                 f'grep -n -A 4 "strata_trial_roots" infra/inventory.yml',
                                 f'add "{manifest.get("source_version")}": <path> (or "{name_of(graph, manifest)}" when this '
                                 f'engine version holds more than one weight set), then `{cli_name()} ops upgrade plan '
                                 f'{host} --backend {backend_id}` again'))
    owner = root_owner(graph, host, manifest, root)
    if owner:
        raise SystemExit(explain(f'{backend_id} would be prepared into {root}, which {host} holds as {owner[1]}',
                                 [f'infra/inventory.yml {host} `strata_trial_roots` has no key equal to '
                                  f'{name_of(graph, manifest)}, so the target root fell back to the engine version key '
                                  f'"{manifest.get("source_version")}"',
                                  f'infra/retention.yml {host} `{owner[0]}:` names that path for {owner[1]}'],
                                 f'grep -n -A 6 "strata_trial_roots" infra/inventory.yml',
                                 f'declare "{name_of(graph, manifest)}": <a path of its own> there - that key is what a second '
                                 f'weight set on one engine version needs - or stop: a retired manifest is the record '
                                 f'of its pins, not a preparation target (T5.8)'))
    answer = reuse_answer(graph, host, manifest, reuse_root, reuse_declared, download)
    return manifest, key, root, answer


def reuse_answer(graph, host, manifest, reuse_root, reuse_declared, download):
    """The reuse side of a preparation: named root checked, declared candidate chosen, or the download explained."""
    found = reuse_candidates(graph, host, manifest)
    if reuse_root:
        return checked_reuse(graph, host, manifest, reuse_root, found)
    if reuse_declared:
        if not found['candidates']:
            raise SystemExit(f'--reuse-declared found nothing to copy on {host}: nothing the ledger calls full '
                             f'holds the {len(asks(manifest))} file(s) {name_of(graph, manifest)} asks a reuse root for. Run '
                             f'it with --download ({round(found["download_bytes"] / GIB, 1)} GiB) instead')
        if len(found['candidates']) > 1:
            names = ', '.join(f'{entry["backend"]} at {entry["root"]}' for entry in found['candidates'])
            raise SystemExit(f'--reuse-declared cannot choose: {host} holds more than one full root with these '
                             f'bytes — {names}. Name one with --reuse-root PATH')
        entry = found['candidates'][0]
        return ('reuse', entry['root'], entry['backend'])
    if found['candidates'] and not download:
        entry = found['candidates'][0]
        raise SystemExit(explain(
            f'{host} already holds what {name_of(graph, manifest)} pins: {entry["files"]} file(s), '
            f'{round(entry["bytes"] / GIB, 1)} GiB, byte for byte with backends/{entry["backend"]}.json '
            f'(the {entry["state"]} line, root {entry["root"]})',
            [f'preparing without a reuse source downloads {round(found["download_bytes"] / GIB, 1)} GiB again']
            + [f'not offered: {reason}' for reason in found['why_not']],
            f'{cli_name()} ops upgrade reuse {host} --backend {name_of(graph, manifest)}',
            f'pass --reuse-root {entry["root"]}, or --reuse-declared to take that candidate, or --download to say '
            f'the download is meant'))
    return ('download', found['download_bytes'])


def checked_reuse(graph, host, manifest, reuse_root, found):
    """Validate a `--reuse-root` the operator named: declared for this host, full, and pinning the same bytes."""
    roots = sorted((graph.nodes.get(host, {}).get('strata_trial_roots') or {}).values())
    staging = [entry.get('path') for _, entry in ledger_lines(graph, host) if entry.get('path')]
    if reuse_root not in roots + staging:
        raise SystemExit(explain(f'--reuse-root {reuse_root} is not a root this declaration knows on {host}',
                                 [f'infra/inventory.yml {host} strata_trial_roots: {roots or "none"}',
                                  f'infra/retention.yml {host} names: {staging or "none"}'],
                                 f'{cli_name()} ops storage {host} --candidates',
                                 'name one of those paths, or drop --reuse-root and let the preparation download'))
    source_id = _ledger_backend_at(graph, host, reuse_root)
    state, line = ledger_line(graph, host, source_id) if source_id else (None, None)
    if line is not None and line.get('form') != 'full':
        raise SystemExit(f'--reuse-root {reuse_root}: the ledger calls this root `form: {line["form"]}` '
                         f'({state} line). A thin root holds no weights to copy; `{cli_name()} ops restore {host} '
                         f'--backend {line["backend"]}` puts them back first')
    matching = {entry['backend'] for entry in found['candidates'] if entry['root'] == reuse_root}
    if matching:
        return ('reuse', reuse_root, sorted(matching)[0])
    if source_id is None:
        raise SystemExit(f'--reuse-root {reuse_root}: no manifest can be matched to this root from the ledger, so '
                         f'its bytes cannot be checked against {name_of(graph, manifest)}. Name a root the ledger declares, '
                         f'or drop --reuse-root')
    source = manifest_for(graph, source_id)
    missing, mismatched = supplies(source, manifest)
    if missing or mismatched:
        raise SystemExit(explain(f'--reuse-root {reuse_root} does not hold what {name_of(graph, manifest)} asks for',
                                 [f'{source_id} pins {len(pinned(source))} file(s); {name_of(graph, manifest)} asks a reuse '
                                  f'root for {len(asks(manifest))}',
                                  f'not there at all: {", ".join(missing) or "none"}',
                                  f'there at a different size or sha256: {", ".join(mismatched) or "none"}'],
                                 f'{cli_name()} ops upgrade reuse {host} --backend {name_of(graph, manifest)}',
                                 'drop --reuse-root to download, or reuse a root that holds the same bytes'))
    return ('reuse', reuse_root, source_id)


def _ledger_backend_at(graph, host, path):
    for _, entry in ledger_lines(graph, host):
        if entry.get('path') == path:
            return entry.get('backend')
    return None


def route_targets(graph, host, backend_id):
    """The `local-coder` target names on HOST that serve BACKEND_ID; the gateway publishes each as an alias."""
    alias = graph.route.get('alias', 'local-coder')
    return sorted(f'{alias}-{target}' for target, spec in (graph.route.get('targets') or {}).items()
                  if spec.get('node') == host and spec.get('backend') == backend_id)


def measure_alias(graph, host, backend_id):
    """The alias a measurement runs on, or what has to be declared before one exists.

    `scripts/route.py` publishes `local-coder-<target>` for every target of gateway/local-coder.json, so an alias is
    a read of the route table rather than a name to invent.
    """
    targets = route_targets(graph, host, backend_id)
    if targets:
        return ', '.join(targets)
    return f'no target of gateway/local-coder.json serves {backend_id} on {host} yet: declare one, then measure on ' \
           f'that alias'



def _component_for_backend(graph, host, backend_id):
    if not backend_id:
        return None
    for entry in graph.components:
        pin = entry.get('pin') or {}
        if pin.get('lock') == f'backends/{backend_id}.json' and host in (entry.get('hosts') or []):
            return entry
    return None


def _container_plan(graph, host, backend_id, manifest):
    """The ordered reading of a container-built engine upgrade or candidate on HOST."""
    facts = graph.nodes.get(host, {})
    engine_block = manifest.get('engine') or {}
    worker_cfg = (manifest.get('workers') or {}).get(host) or {}
    service = worker_cfg.get('service') if isinstance(worker_cfg, dict) else None
    patches = manifest.get('patches') if isinstance(manifest.get('patches'), list) else []
    tf_default = None
    if facts.get('spark_tensorfold'):
        try:
            tf_default = backend_names.tensorfold_backend(graph.backends, host)
        except ValueError:
            tf_default = None
    current_backend = (facts.get('default_backend') or tf_default
                       or facts.get('strata_default_backend') or facts.get('strata_backend'))
    component = _component_for_backend(graph, host, current_backend) or _component_for_backend(graph, host, backend_id)
    found = reuse_candidates(graph, host, manifest)
    state_now, line_now = ledger_line(graph, host, backend_id)
    state_current, _ = ledger_line(graph, host, current_backend)
    blockers, notes = [], []

    if host not in (manifest.get('workers') or {}):
        blockers.append(f'backends/{backend_id}.json does not name {host} in `workers`')
    if not isinstance(service, dict) or not all(service.get(k) for k in ('unit', 'port', 'root')):
        blockers.append(f'backends/{backend_id}.json declares no complete `workers.{host}.service` (`unit`, `port`, `root`)')
    if state_now is None:
        blockers.append(f'infra/retention.yml {host} names nothing for {backend_id}: a preparation starts as a '
                        f'`candidate:` line with `form: full`')
    if line_now is not None and line_now.get('form') not in (None, 'full'):
        blockers.append(f'infra/retention.yml calls {backend_id} `form: {line_now["form"]}` on {host}')
    patches_valid = True
    root_resolved = Path(graph.root).resolve()
    for entry in patches:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or not isinstance(entry.get('sha256'), str):
            patches_valid = False
            blockers.append(f'backends/{backend_id}.json has a malformed `patches` entry')
            continue
        patch_path = (root_resolved / entry['path']).resolve()
        if not patch_path.is_relative_to(root_resolved) or not patch_path.is_file():
            patches_valid = False
            blockers.append(f'pinned patch {entry["path"]} declared by backends/{backend_id}.json is missing')
            continue
        actual = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        if actual != entry['sha256']:
            patches_valid = False
            blockers.append(f'pinned patch {entry["path"]} has sha256 {actual}, manifest declares {entry["sha256"]}')
    if found['candidates']:
        first = found['candidates'][0]
        notes.append(f'shared model.directory matches {first["backend"]} ({first["state"]}); 0 GiB weight download needed')
    else:
        notes.append(f'nothing held on {host} shares this model.directory: {round(found["download_bytes"] / GIB, 1)} GiB weight download')
    if patches:
        notes.append(f'{len(patches)} pinned patch(es) will be SHA-256 verified and applied before Docker build')
    for reason in found['why_not']:
        notes.append(f'reuse: {reason}')

    service_root = service.get('root') if isinstance(service, dict) else None
    steps = [
        {'step': 'declare the backend and patches',
         'where': f'backends/{backend_id}.json',
         'state': 'done' if (host in (manifest.get('workers') or {}) and isinstance(service, dict) and patches_valid) else 'open',
         'what': f'engine {engine_block.get("version")} ({str(engine_block.get("revision") or "")[:12]}), {len(patches)} patch(es), service.root {service_root or "unset"}'},
        {'step': 'declare what the host holds',
         'where': f'infra/retention.yml {host}',
         'state': 'done' if state_now else 'open',
         'what': f'a `candidate:` line for {backend_id} with `form: full`, plus area entries for service.root and image.tag'},
        {'step': 'read the host before preparing',
         'where': 'read-only',
         'state': 'open',
         'what': f'{cli_name()} ops storage {host} --candidates, then {cli_name()} ops prepare {host} --backend {backend_id} --check'},
        {'step': 'weights',
         'where': 'the worker',
         'state': 'done' if found['candidates'] else 'open',
         'what': f'shared model.directory ready via {found["candidates"][0]["backend"]}' if found['candidates'] else f'{cli_name()} ops download {host} --model <profile>'},
        {'step': 'prepare',
         'where': 'the worker',
         'state': 'open',
         'what': f'{cli_name()} ops prepare {host} --backend {backend_id}'},
        {'step': 'measure before applying',
         'where': 'the host',
         'state': 'open',
         'what': f'{cli_name()} claim {host} --backend {backend_id} (no --route), then the ladder and the Suite on {measure_alias(graph, host, backend_id)}'},
        {'step': 'move what a claim resolves to',
         'where': f'infra/inventory.yml {host} default_backend',
         'state': 'done' if current_backend == backend_id else 'open',
         'what': f'{current_backend or "unset"} -> {backend_id} (keep spark_tensorfold / N-1 rollback)'},
        {'step': 'move the pin & ledger',
         'where': f'infra/components.yml & infra/retention.yml {host}',
         'state': 'open',
         'what': f'{backend_id} to `serving:` and {current_backend or "prior"} to `rollback:`'},
        {'step': 'deploy and claim',
         'where': 'control, then the host',
         'state': 'open',
         'what': f'{cli_name()} ops deploy, then {cli_name()} claim {host}'},
        {'step': 'verify',
         'where': 'read-only',
         'state': 'open',
         'what': f'{cli_name()} warm {host}, {cli_name()} ops drift, {cli_name()} doctor'},
    ]
    return {
        'host': host,
        'backend': backend_id,
        'shape': 'container',
        'engine': {'version': engine_block.get('version'), 'revision': engine_block.get('revision'),
                   'ggml_revision': None, 'cuda_toolkit_major': None},
        'patches': [{'path': p.get('path'), 'sha256': p.get('sha256')} for p in patches if isinstance(p, dict)],
        'weights': {'files': len(asks(manifest)), 'bytes': weight_bytes(manifest)},
        'root': {'key': 'service.root', 'path': service_root},
        'current': {'backend': current_backend,
                    'component': component.get('id') if component else None,
                    'pin': (component.get('pin') if component else None),
                    'ledger_state': state_now, 'current_ledger_state': state_current},
        'reuse': found, 'blockers': blockers, 'notes': notes, 'steps': steps,
    }


def plan(graph, host, backend_id):
    """The ordered reading of an upgrade on HOST: declared facts, blockers, and the steps with their commands."""
    manifest = manifest_for(graph, backend_id)
    if not strata_shaped(manifest):
        if container_shaped(manifest):
            return _container_plan(graph, host, backend_id, manifest)
        raise SystemExit(not_strata_shaped(host, name_of(graph, manifest), manifest))
    key, root = trial_root(graph, host, manifest)
    current_backend = graph.nodes.get(host, {}).get('strata_backend')
    component = pinned_by(graph, host)
    found = reuse_candidates(graph, host, manifest)
    state_now, line_now = ledger_line(graph, host, backend_id)
    state_current, line_current = ledger_line(graph, host, current_backend)
    blockers, notes = [], []

    if host not in (manifest.get('workers') or {}):
        blockers.append(f'backends/{backend_id}.json does not name {host} in `workers`')
    if root is None:
        blockers.append(f'infra/inventory.yml {host} declares no strata_trial_roots key for {backend_id} '
                        f'(engine {manifest.get("source_version")})')
    owner = root_owner(graph, host, manifest, root) if root else None
    if owner:
        blockers.append(f'{root} is what {host} holds as {owner[1]} (infra/retention.yml {owner[0]} line): the '
                        f'root lookup for {backend_id} fell back to the engine version key, and preparing it here '
                        f'would overwrite that root. Declare a `strata_trial_roots` key equal to {backend_id} - the '
                        f'key a second weight set on one engine version needs - or leave the retired pin alone '
                        f'(T5.8)')
    if state_now is None:
        blockers.append(f'infra/retention.yml {host} names nothing for {backend_id}: a preparation starts as a '
                        f'`candidate:` line with `form: full`')
    if line_now is not None and line_now.get('form') not in (None, 'full'):
        blockers.append(f'infra/retention.yml calls {backend_id} `form: {line_now["form"]}` on {host}; a root that '
                        f'is prepared from a manifest is `full`')
    if current_backend is None:
        blockers.append(f'infra/inventory.yml {host} declares no strata_backend, so there is no current serving '
                        f'backend to move from and no N-1 to record')
    if component is None and current_backend:
        blockers.append(f'no infra/components.yml entry pins {current_backend} for {host}; declared.py refuses that '
                        f'mismatch, so the pin has to name this host before a claim resolves to it')
    if current_backend and line_current is not None and line_current.get('form') == 'thin':
        notes.append(f'the backend {host} serves today ({current_backend}) is `form: thin` in the ledger: it cannot '
                     f'become the rollback of this move, so the rollback has to come from another full root or be '
                     f'restored first (`{cli_name()} ops restore {host} --backend {current_backend}`)')
    if not found['candidates']:
        notes.append(f'nothing on {host} pins these bytes: this preparation downloads '
                     f'{round(found["download_bytes"] / GIB, 1)} GiB')
    for reason in found['why_not']:
        notes.append(f'reuse: {reason}')
    for entry in found['packs']:
        notes.append(f"the drafter {host} is asked to run is {entry['drafter']}: the weights may be copied, but "
                     f"scripts/strata-prepare-worker.py fetches {entry['path']} ({entry['size']} bytes, "
                     f"{entry['sha256'][:16]}…) through the engine's own pinned download and refuses the preparation "
                     f"if the bytes differ. The MTP draft layer is not fetched for this root: Strata runs one "
                     f"drafter per run (strata_control/drafting.py)")
    if drafter_asked(manifest, host) and drafter_asked(manifest, host) != 'mtp' and not found['packs']:
        blockers.append(f'backends/{backend_id}.json asks {host} to drafter with {drafter_asked(manifest, host)} '
                        f'and names no `drafter_pack`: the file that drafter needs is a pin, not a guess')

    steps = [
        {'step': 'declare the backend for this host',
         'where': f'backends/{backend_id}.json',
         'state': 'done' if host in (manifest.get('workers') or {}) else 'open',
         'what': f'`workers` names {host} with this host\'s flags (cuda_arch, vram_reserve_mib, low_ram, '
                 f'unified_memory, min_available_gib, pool_workers, cpu_quota_percent)'},
        {'step': 'declare the root',
         'where': f'infra/inventory.yml {host} strata_trial_roots',
         'state': 'done' if root else 'open',
         'what': f'key "{key or manifest.get("source_version")}": <path>'},
        {'step': 'declare what the host holds',
         'where': f'infra/retention.yml {host}',
         'state': 'done' if state_now else 'open',
         'what': f'a `candidate:` line for {backend_id} at that path with `form: full` and the reason'},
        {'step': 'read the host before preparing',
         'where': 'read-only',
         'state': 'open',
         'what': f'{cli_name()} ops storage {host} --candidates, then {cli_name()} ops prepare {host} '
                 f'--backend {backend_id} --check'},
        {'step': 'prepare',
         'where': 'the worker',
         'state': 'open',
         'what': _prepare_command(host, backend_id, found)},
        {'step': 'measure before applying',
         'where': 'the host',
         'state': 'open',
         'what': f'{cli_name()} claim {host} --backend {backend_id} (no --route), then the ladder and the Suite on '
                 f'{measure_alias(graph, host, backend_id)}'},
        {'step': 'move the pin',
         'where': 'infra/components.yml',
         'state': 'open',
         'what': f'{component["id"] if component else "the entry naming " + host}: pin '
                 f'version {manifest.get("source_version")}, revision {manifest.get("source_revision")}, lock '
                 f'backends/{backend_id}.json'},
        {'step': 'move what a claim resolves to',
         'where': f'infra/inventory.yml {host} strata_backend',
         'state': 'done' if current_backend == backend_id else 'open',
         'what': f'{current_backend} -> {backend_id}'},
        {'step': 'move the ledger',
         'where': f'infra/retention.yml {host}',
         'state': 'open',
         'what': f'{backend_id} to `serving:` and {current_backend} to `rollback:` (both `form: full`); that pair '
                 f'is what an N-1 looks like (ADR 0008, §2.2)'},
        {'step': 'deploy and claim',
         'where': 'control, then the host',
         'state': 'open',
         'what': f'{cli_name()} ops deploy, then {cli_name()} claim {host}'},
        {'step': 'verify',
         'where': 'read-only',
         'state': 'open',
         'what': f'{cli_name()} warm {host}, {cli_name()} ops drift (route, front identity), {cli_name()} doctor'},
    ]
    return {'host': host, 'backend': backend_id,
            'engine': {'version': manifest.get('source_version'), 'revision': manifest.get('source_revision'),
                       'ggml_revision': manifest.get('ggml_revision'), 'cuda_toolkit_major':
                       manifest.get('cuda_toolkit_major')},
            'weights': {'files': len(asks(manifest)), 'bytes': weight_bytes(manifest)},
            'root': {'key': key, 'path': root},
            'current': {'backend': current_backend,
                        'component': component.get('id') if component else None,
                        'pin': (component.get('pin') if component else None),
                        'ledger_state': state_now, 'current_ledger_state': state_current},
            'reuse': found, 'blockers': blockers, 'notes': notes, 'steps': steps}


def _prepare_command(host, backend_id, found):
    candidates = found['candidates']
    if not candidates:
        flag = '--download'
    elif len(candidates) == 1:
        flag = '--reuse-declared'
    else:
        preferred = next((c for c in candidates if c.get('state') == 'serving'), candidates[-1])
        flag = f"--reuse-root {preferred['root']}"
    return f'{cli_name()} ops prepare {host} --backend {backend_id} {flag} --isolated'


def graph_fixture(root=None):
    """A fresh read of the declared files (used by `plan`/`reuse` and by the tests)."""
    return declared.load(root or declared.ROOT)


def with_ledger(graph, host, mutate):
    """A copy of the graph with one host's ledger changed, for asking what the reading would then say."""
    clone = copy.deepcopy(graph)
    mutate(clone.retention.setdefault(host, {}))
    return clone


def _github_slug(repository):
    cleaned = repository.rstrip('/').removesuffix('.git')
    parts = cleaned.split('/')
    if len(parts) < 2:
        raise SystemExit(f'cannot derive GitHub owner/repo from {repository!r}')
    return f'{parts[-2]}/{parts[-1]}'


def _resolve_tag_revision(repository, version):
    tag = f'v{version}' if not str(version).startswith('v') else str(version)
    done = subprocess.run(
        ['git', 'ls-remote', '--tags', repository, f'refs/tags/{tag}', f'refs/tags/{tag}^{{}}'],
        capture_output=True, text=True, timeout=30,
    )
    if done.returncode != 0:
        raise SystemExit(f'git ls-remote failed for {repository} {tag}: {done.stderr.strip()}')
    refs = {}
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            refs[parts[1]] = parts[0]
    revision = refs.get(f'refs/tags/{tag}^{{}}') or refs.get(f'refs/tags/{tag}')
    if not revision:
        raise SystemExit(f'tag {tag} not found in {repository}')
    return revision


def _fetch_raw(slug, revision, rel_path):
    url = f'https://raw.githubusercontent.com/{slug}/{revision}/{rel_path}'
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode('utf-8')
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise SystemExit(f'failed to fetch {url}: HTTP {error.code}') from error
    except OSError as error:
        raise SystemExit(f'failed to fetch {url}: {error}') from error


def fetch_strata_upstream(repository, version, revision=None, setup_file=None, build_reqs=None):
    """Fetch upstream setup.py and requirements-build.txt for VERSION (or read local files for offline/test use)."""
    rev = revision or _resolve_tag_revision(repository, version)
    if setup_file is not None:
        setup_path = Path(setup_file)
        setup_text = setup_path.read_text(encoding='utf-8')
        layout_kind = 'terminal' if setup_path.parent.name == 'terminal' else 'root'
        reqs_text = Path(build_reqs).read_text(encoding='utf-8') if build_reqs is not None else None
        return rev, layout_kind, setup_text, reqs_text
    slug = _github_slug(repository)
    setup_text = _fetch_raw(slug, rev, 'terminal/setup.py')
    layout_kind = 'terminal'
    if setup_text is None:
        setup_text = _fetch_raw(slug, rev, 'setup.py')
        layout_kind = 'root'
    if setup_text is None:
        raise SystemExit(f'neither terminal/setup.py nor setup.py found at {slug}@{rev[:12]}')
    if build_reqs is not None:
        reqs_text = Path(build_reqs).read_text(encoding='utf-8')
    else:
        prefix = 'terminal/' if layout_kind == 'terminal' else ''
        reqs_text = _fetch_raw(slug, rev, f'{prefix}requirements-build.txt')
    return rev, layout_kind, setup_text, reqs_text


def parse_strata_setup(setup_text, model_repository, base_files, build_reqs_text=None):
    """Extract ggml_revision, model_revision, pinned files, and build tools from upstream setup.py."""
    warnings = []
    ggml_match = re.search(r'^\s*LLAMA_CPP_COMMIT\s*=\s*"([a-f0-9]{40})"', setup_text, re.M)
    ggml_revision = ggml_match.group(1) if ggml_match else None
    if ggml_revision is None:
        warnings.append('LLAMA_CPP_COMMIT not found in upstream setup.py')

    repo_match = re.search(rf'"{re.escape(model_repository)}"\s*:\s*"([a-f0-9]{{40}})"', setup_text)
    model_revision = repo_match.group(1) if repo_match else None
    if model_revision is None:
        warnings.append(f'{model_repository} not found in HF_REVISIONS')

    files = []
    unchanged = True
    for entry in base_files:
        basename = Path(entry['path']).name
        pin_match = re.search(rf'"{re.escape(basename)}"\s*:\s*\(\s*(\d+)\s*,\s*"([a-f0-9]{{64}})"\s*\)', setup_text)
        item = copy.deepcopy(entry)
        if pin_match:
            size, sha256 = int(pin_match.group(1)), pin_match.group(2)
            same = (size == int(entry['size']) and sha256 == entry['sha256'])
            item['size'] = size
            item['sha256'] = sha256
            if same:
                item['same_as'] = entry.get('same_as') or entry['path']
            else:
                item.pop('same_as', None)
                unchanged = False
        else:
            unchanged = False
            warnings.append(f'{basename} pin not found in upstream setup.py; kept base entry')
        files.append(item)

    pip_build_tools = None
    if build_reqs_text is not None:
        tools = [line.strip() for line in build_reqs_text.splitlines()
                 if line.strip() and not line.strip().startswith('#')]
        if tools:
            pip_build_tools = tools

    return {
        'ggml_revision': ggml_revision,
        'model_revision': model_revision,
        'files': files,
        'weights_unchanged': unchanged,
        'pip_build_tools': pip_build_tools,
        'warnings': warnings,
    }


def _default_root_path(host_facts, version):
    roots = host_facts.get('strata_trial_roots') or {}
    if roots:
        sample = next(iter(roots.values()))
        parent = str(Path(sample).parent)
        base = Path(sample).name
        if base.startswith('strata-'):
            return f'{parent}/strata-{version}'
        prefix = base.split('-strata-', 1)[0]
        if '-strata-' in base and prefix:
            return f'{parent}/{prefix}-strata-{version}'
    project_root = host_facts.get('project_root', '/srv/agentiac')
    return f'{project_root}/strata-{version}'


def _base_target(graph, host, base_stem):
    matches = [(name, spec) for name, spec in (graph.route.get('targets') or {}).items()
               if spec.get('node') == host and spec.get('backend') == base_stem]
    if matches:
        return matches[0]
    host_targets = [(name, spec) for name, spec in (graph.route.get('targets') or {}).items()
                    if spec.get('node') == host]
    if host_targets:
        return host_targets[0]
    raise SystemExit(f'gateway/local-coder.json has no target for {host} to clone route settings from')


def draft_upgrade(graph, host, version, base_backend=None, revision=None, context=None,
                  root_path=None, setup_file=None, build_reqs=None):
    """Build a complete candidate declaration set for VERSION on HOST without writing any file."""
    if host not in graph.nodes:
        raise SystemExit(f'{host} is not a host in infra/inventory.yml')
    host_facts = graph.nodes[host]
    base_stem = base_backend or host_facts.get('strata_backend') or host_facts.get('strata_default_backend')
    if not base_stem:
        raise SystemExit(f'{host} has no strata_backend or strata_default_backend; pass --from BACKEND')
    base_manifest = manifest_for(graph, base_stem)
    resolved_base_stem = name_of(graph, base_manifest)
    if not strata_shaped(base_manifest):
        raise SystemExit(not_strata_shaped(host, resolved_base_stem, base_manifest))
    if host not in (base_manifest.get('workers') or {}):
        raise SystemExit(f'backends/{resolved_base_stem}.json does not name {host} in workers')

    rev, layout_kind, setup_text, reqs_text = fetch_strata_upstream(
        base_manifest['repository'], version, revision=revision, setup_file=setup_file, build_reqs=build_reqs,
    )
    parsed = parse_strata_setup(setup_text, base_manifest['model_repository'], base_manifest['files'], reqs_text)

    candidate = copy.deepcopy(base_manifest)
    candidate['source_version'] = str(version)
    candidate['source_revision'] = rev
    if parsed['ggml_revision']:
        candidate['ggml_revision'] = parsed['ggml_revision']
    if parsed['model_revision']:
        candidate['model_revision'] = parsed['model_revision']
    candidate['files'] = parsed['files']
    if parsed['pip_build_tools']:
        candidate['pip_build_tools'] = parsed['pip_build_tools']
    candidate['workers'] = {host: copy.deepcopy(base_manifest['workers'][host])}
    if context is not None:
        candidate['context'] = int(context)
        contexts = list(candidate.get('contexts') or [candidate['context']])
        if candidate['context'] not in contexts:
            contexts.insert(0, candidate['context'])
        candidate['contexts'] = contexts

    stem = backend_names.derived(candidate)
    if not stem:
        raise SystemExit(f'cannot derive canonical backend name from drafted manifest for {version}')
    candidate['id'] = stem
    weight_note = 'weights unchanged' if parsed['weights_unchanged'] else 'weights changed'
    candidate['status'] = f'candidate Strata {version} on {host} (drafted from {resolved_base_stem}; {weight_note})'

    chosen_root = root_path or _default_root_path(host_facts, version)
    ctx = candidate['context']
    _, base_target_spec = _base_target(graph, host, resolved_base_stem)
    target_spec = copy.deepcopy(base_target_spec)
    target_spec['backend'] = stem
    target_spec['context_window'] = ctx
    target_name = f'{host}-{stem}-{ctx // 1024}k'

    retention_entry = {
        'backend': stem,
        'path': chosen_root,
        'class': 'persistent',
        'form': 'full',
        'reason': f'candidate Strata {version} drafted from {resolved_base_stem} ({weight_note})',
    }
    reuse = reuse_candidates(graph, host, candidate)
    return {
        'host': host,
        'version': str(version),
        'revision': rev,
        'layout': layout_kind,
        'base_backend': resolved_base_stem,
        'backend': stem,
        'manifest': candidate,
        'root_key': str(version),
        'root_path': chosen_root,
        'target_name': target_name,
        'target_spec': target_spec,
        'retention_entry': retention_entry,
        'weights_unchanged': parsed['weights_unchanged'],
        'warnings': parsed['warnings'],
        'reuse': reuse,
    }


def _insert_inventory_root(text, host, key, root_path):
    lines = text.splitlines(keepends=True)
    host_re = re.compile(rf'^(\s+){re.escape(host)}:\s*(?:#.*)?$')
    host_idx = next((i for i, line in enumerate(lines) if host_re.match(line)), None)
    if host_idx is None:
        raise SystemExit(f'host {host} not found in infra/inventory.yml')
    host_indent = len(host_re.match(lines[host_idx]).group(1))

    end_idx = len(lines)
    roots_idx = None
    roots_indent = None
    for i in range(host_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(lines[i]) - len(lines[i].lstrip(' '))
        if indent <= host_indent:
            end_idx = i
            break
        if re.match(r'^\s+strata_trial_roots:\s*(\{\})?\s*(?:#.*)?$', lines[i]):
            roots_idx = i
            roots_indent = indent

    if roots_idx is None:
        pad = ' ' * (host_indent + 2)
        block = [f'{pad}strata_trial_roots:\n', f'{pad}  "{key}": {root_path}\n']
        return ''.join(lines[:end_idx] + block + lines[end_idx:])

    if re.match(r'^\s+strata_trial_roots:\s*\{\}\s*(?:#.*)?$', lines[roots_idx]):
        lines[roots_idx] = re.sub(r'\s*\{\}', '', lines[roots_idx])
        entry_pad = ' ' * (roots_indent + 2)
        lines.insert(roots_idx + 1, f'{entry_pad}"{key}": {root_path}\n')
        return ''.join(lines)

    insert_at = roots_idx + 1
    entry_indent = roots_indent + 2
    for i in range(roots_idx + 1, end_idx):
        stripped = lines[i].strip()
        if not stripped:
            continue
        indent = len(lines[i]) - len(lines[i].lstrip(' '))
        if stripped.startswith('#'):
            if indent > roots_indent:
                insert_at = i + 1
            continue
        if indent <= roots_indent:
            break
        entry_indent = indent
        insert_at = i + 1
    lines.insert(insert_at, f'{" " * entry_indent}"{key}": {root_path}\n')
    return ''.join(lines)


def _insert_retention_candidate(text, host, entry):
    lines = text.splitlines(keepends=True)
    host_re = re.compile(rf'^{re.escape(host)}:\s*(?:#.*)?$')
    host_idx = next((i for i, line in enumerate(lines) if host_re.match(line)), None)
    if host_idx is None:
        raise SystemExit(f'host {host} not found in infra/retention.yml')

    end_idx = len(lines)
    cand_idx = None
    cand_indent = None
    for i in range(host_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(lines[i]) - len(lines[i].lstrip(' '))
        if indent == 0:
            end_idx = i
            break
        if re.match(r'^\s+candidate:\s*(\[\])?\s*(?:#.*)?$', lines[i]):
            cand_idx = i
            cand_indent = indent

    if cand_idx is None:
        raise SystemExit(f'candidate section not found for {host} in infra/retention.yml')

    item_lines = yaml.safe_dump([entry], sort_keys=False).splitlines(keepends=True)
    if re.match(r'^\s+candidate:\s*\[\]\s*(?:#.*)?$', lines[cand_idx]):
        lines[cand_idx] = re.sub(r'\s*\[\]', '', lines[cand_idx])
        pad = ' ' * (cand_indent + 2)
        block = [pad + line for line in item_lines]
        return ''.join(lines[:cand_idx + 1] + block + lines[cand_idx + 1:])

    insert_at = cand_idx + 1
    for i in range(cand_idx + 1, end_idx):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(lines[i]) - len(lines[i].lstrip(' '))
        if indent <= cand_indent:
            break
        insert_at = i + 1
    pad = ' ' * (cand_indent + 2)
    block = [pad + line for line in item_lines]
    return ''.join(lines[:insert_at] + block + lines[insert_at:])


def apply_draft(root, draft):
    """Write the drafted declarations to ROOT, validate cross-file references, and roll back on any error."""
    root = Path(root)
    stem = draft['backend']
    host = draft['host']
    manifest_path = root / f'backends/{stem}.json'
    inventory_path = root / 'infra/inventory.yml'
    retention_path = root / 'infra/retention.yml'
    route_path = root / 'gateway/local-coder.json'

    if manifest_path.exists():
        raise SystemExit(f'backends/{stem}.json already exists; refusing to overwrite')
    current_graph = declared.load(root)
    if draft['root_key'] in (current_graph.nodes[host].get('strata_trial_roots') or {}):
        raise SystemExit(f"infra/inventory.yml {host} strata_trial_roots already has key {draft['root_key']!r}")
    if draft['target_name'] in (current_graph.route.get('targets') or {}):
        raise SystemExit(f"gateway/local-coder.json already declares target {draft['target_name']!r}")

    originals = {p: p.read_text(encoding='utf-8') for p in (inventory_path, retention_path, route_path)}
    try:
        manifest_path.write_text(json.dumps(draft['manifest'], indent=2) + '\n', encoding='utf-8')
        inventory_path.write_text(
            _insert_inventory_root(originals[inventory_path], host, draft['root_key'], draft['root_path']),
            encoding='utf-8',
        )
        retention_path.write_text(
            _insert_retention_candidate(originals[retention_path], host, draft['retention_entry']),
            encoding='utf-8',
        )
        route_data = json.loads(originals[route_path])
        route_data['targets'][draft['target_name']] = draft['target_spec']
        route_path.write_text(json.dumps(route_data, indent=2) + '\n', encoding='utf-8')

        updated_graph = declared.load(root)
        problems = declared.references(updated_graph)
        if problems:
            raise SystemExit('drafted declarations failed cross-file validation:\n' + '\n'.join(f'  {p}' for p in problems))
    except BaseException:
        if manifest_path.exists():
            manifest_path.unlink()
        for path, content in originals.items():
            path.write_text(content, encoding='utf-8')
        raise

    written = [
        str(manifest_path.relative_to(root)),
        str(inventory_path.relative_to(root)),
        str(retention_path.relative_to(root)),
        str(route_path.relative_to(root)),
    ]
    from agentiac import catalog, model_names
    compat_path = root / 'docs/compatibility.md'
    if compat_path.exists():
        compat_text = catalog.compatibility(root)
        if compat_path.read_text(encoding='utf-8') != compat_text:
            compat_path.write_text(compat_text, encoding='utf-8')
            written.append(str(compat_path.relative_to(root)))
    names_path = root / 'docs/model-names.md'
    if names_path.exists():
        names_text = model_names.documentation(updated_graph)
        if names_path.read_text(encoding='utf-8') != names_text:
            names_path.write_text(names_text, encoding='utf-8')
            written.append(str(names_path.relative_to(root)))
    return written
