"""Backend manifest names: `<engine>-<engine version>-<model>-<quantization>[-<variant>]`.

A backend is the node- and context-independent half of a route target, so a node-first target key is exactly
`<node>-<backend name>-<context>` (docs/model-naming.md). The name is the manifest's file stem and is what
inventory, retention, components, route targets and `--backend` refer to.

The `id` inside a manifest is a different thing: it is the runtime identity a worker reports (the on-demand front's
`backend_id`, Strata's `prepare-<id>-state.json`), compared by the gateway's identity guard together with the hash of
the file's bytes. Renaming a file therefore changes nothing a running worker reports. A manifest keeps its old `id`
until the host is next claimed with an id that follows its file name (naming migration, backend step 3); new
manifests start with `id` equal to their file name.

`backends/legacy-names.yml` maps every retired file name to its current one, so old names given on the command line,
in records and in history still resolve.
"""
import json
from pathlib import Path
import re

import yaml

LEGACY_FILE = 'backends/legacy-names.yml'
NAME = re.compile(r'[a-z0-9][a-z0-9._-]*')
# Engine repository -> label; the same labels lead node-first target keys.
ENGINES = {'TensorFold': 'tf', 'Strata': 'strata'}
# Model repository -> label, for the exact declared model families only.
MODELS = {'Qwen3.8-Flash-Next-FP4-Local': 'qfn',
          'Qwen3.8-Flash-Next-GSQ-RCO-GGUF': 'qfn-gsq-rco',
          'Qwen3.8-Flash-Next-GGUF': 'qfn',
          'DeepSeek-V4.1-Flash-1017-GSQ-RCO-Coder-GGUF': 'dsv41-coder'}
# Manifests the rule cannot name yet: they pin an image but no model, engine version or quantization.
UNNAMED = {'vllm-qwen38-spark': 'an image-only compatibility probe: no model, engine version or quantization is pinned'}
_QUANT = re.compile(r'(?<![A-Za-z0-9])(?:UD[-_])?(?:IQ|TQ|Q)\d+_[A-Za-z0-9_]+(?![A-Za-z0-9])', re.I)


def slug(value):
    return re.sub(r'[^a-z0-9._]+', '-', str(value).lower()).strip('-') or 'unknown'


def parts(manifest):
    """(engine, version, model, quantization) labels of MANIFEST; a part it does not declare is 'unknown'."""
    from agentiac import identities
    engine = identities._engine_of(manifest)
    repo = engine['repository'].rstrip('/').rsplit('/', 1)[-1].removesuffix('.git')
    engine_label = 'unknown' if repo == identities.UNKNOWN else ENGINES.get(repo, slug(repo))
    nested = manifest.get('model') if isinstance(manifest.get('model'), dict) else {}
    model_repo = (manifest.get('model_repository') or nested.get('repository') or 'unknown').rsplit('/', 1)[-1]
    model = MODELS.get(model_repo, slug(model_repo))
    if model_repo == 'Qwen3.8-Flash-Next-Gyro-GGUF':
        model = 'qfn-' + slug(manifest.get('model'))
    quant = manifest.get('quantization')
    if not quant:
        # the main weight names it; auxiliary Engram/drafter pins remain in the manifest
        files = manifest.get('files') or nested.get('files') or []
        match = _QUANT.search(files[0]['path']) if files else None
        quant = match.group() if match else 'unknown'
    version = engine['version']
    return (engine_label, 'unknown' if version == identities.UNKNOWN else slug(version), model,
            slug(quant).removeprefix('mixed-'))


def derived(manifest):
    """The name the rule gives MANIFEST, or None when it does not declare all four parts."""
    found = parts(manifest)
    return None if 'unknown' in found else '-'.join(found)


def legacy(root):
    path = Path(root) / LEGACY_FILE
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text()) or {}
    return loaded.get('legacy_names', {}) if isinstance(loaded, dict) else {}


def resolve(backends, legacy_names, name):
    """NAME as a current backend file name: itself, the new name of a retired one, or the file whose manifest `id`
    is NAME (what a worker reports). An unknown name is returned unchanged, so the caller's own lookup refuses it."""
    if name is None or name in backends:
        return name
    if name in legacy_names:
        return legacy_names[name]
    owners = [stem for stem, manifest in backends.items() if manifest.get('id') == name]
    return owners[0] if len(owners) == 1 else name


def manifests(root):
    """{file stem: manifest} of the referenceable manifests under ROOT/backends, read as declared.load reads them.

    For callers that need only the manifests (scripts/borrow.py): a launch-argument list or the schema is not one.
    """
    found = {}
    for path in sorted((Path(root) / 'backends').glob('*.json')):
        if path.stem == 'schema':
            continue
        manifest = json.loads(path.read_text())
        if isinstance(manifest, dict) and 'id' in manifest:
            found[path.stem] = manifest
    return found


def tensorfold_backend(backends, host):
    """The TensorFold manifest HOST serves as its own backend (an inventory host with `spark_tensorfold`).

    It is the manifest whose engine label is `tf` and whose `workers` names HOST, so the backend is declared where
    every other backend is, not as a constant in code. BACKENDS maps file stems to manifests. None when HOST has no
    such manifest; ValueError when it has several, because the host would then serve whichever one a reader guessed.
    """
    found = sorted(stem for stem, manifest in backends.items()
                   if isinstance(manifest, dict) and host in (manifest.get('workers') or {})
                   and parts(manifest)[0] == ENGINES['TensorFold'])
    if len(found) > 1:
        raise ValueError(f'{host} has {len(found)} TensorFold manifests ({", ".join(found)}); its workers entry '
                         'must name one')
    return found[0] if found else None


def resolve_in(root, name):
    """resolve() for callers that hold a checkout root instead of a declared graph."""
    from agentiac import declared
    graph = declared.load(root)
    return resolve(graph.backends, legacy(root), name)


def problems(backends, legacy_names):
    """Naming problems: a file name the rule does not give, and a legacy table that does not resolve directly."""
    found = []
    if not isinstance(legacy_names, dict):
        return [f'{LEGACY_FILE} legacy_names must be a mapping']
    for stem, manifest in sorted(backends.items()):
        if stem in UNNAMED:
            continue
        expected = derived(manifest)
        if expected is None:
            found.append(f'backends/{stem}.json: a backend name needs a declared engine, engine version, model and '
                         f'quantization (read {"-".join(parts(manifest))})')
        elif stem != expected and not (stem.startswith(expected + '-') and NAME.fullmatch(stem[len(expected) + 1:])):
            found.append(f'backends/{stem}.json must be named engine-version-model-quant[-variant]: rename it to '
                         f'{expected}.json and add the old name to {LEGACY_FILE}')
    for old, new in legacy_names.items():
        if not isinstance(old, str) or not NAME.fullmatch(old):
            found.append(f'{LEGACY_FILE}: {old!r} must be a lowercase name')
        elif old in backends:
            found.append(f'{LEGACY_FILE}: {old} is still a current backend file')
        if new not in backends:
            found.append(f'{LEGACY_FILE}: {old} must map to an existing backend directly: {new!r}')
    return found
