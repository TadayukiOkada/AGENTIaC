"""class_id and config_id: the two identity hashes of PA §6 (OA §6.2), computed, never written by hand.

The route target in `gateway/local-coder.json` and its backend manifest are the inputs
(T8.1's graph is the one read of them), so the ids say something about the declared
system that an alias cannot: two targets share a `class_id` exactly when the declared
facts predict the same output — weights, quantization, engine, context, thinking, chat
template, max_tokens and the launch arguments that affect output — and share a
`config_id` exactly when the same output is expected from the same configuration on the
same node: `class_id` plus the node, the performance-only arguments, the slots and the
KV format. `source_alias` is never an input: the RTX 4080 and RTX 3090 hosts serve the 0.2.8 Gyro-M
128K target as two replicas under different aliases (OA §6.2), and a `config_id` is
worth reusing only on its own node.

The split between output-affecting and performance-only arguments is not known in
advance (PA §6), so a manifest declares its own `class_args` and `config_args` lists.
An argument that is ambiguous belongs in `class_args`: over-splitting costs a rerun,
under-splitting costs a wrong conclusion. A field nothing declares hashes as the string
`unknown` and is listed in `unknowns` — visible, never silently dropped, and never a
reason to merge two configurations that did declare it.

`hw_fp` is the third key of PA §6 and joins the hardware facts T10.1 collects;
`hardware_fingerprint` hashes whatever it is given and refuses to guess what is missing.
"""
import hashlib
import json
from dataclasses import dataclass, field

UNKNOWN = 'unknown'

#: the hardware facts PA §6 puts into hw_fp; ops inventory (T10.1) must provide them all
HW_FP_FACTS = ('cpu', 'gpu', 'gpu_count', 'memory_total', 'driver')


@dataclass
class TargetIdentity:
    """The computed identity of one route target, with the inputs behind each hash."""

    target: str
    node: str
    class_id: str
    config_id: str
    class_inputs: dict = field(repr=False)
    config_inputs: dict = field(repr=False)
    unknowns: dict = field(repr=False)   # field -> why it could not be read; empty means fully declared

    def as_dict(self):
        return {'target': self.target, 'node': self.node,
                'class_id': self.class_id, 'config_id': self.config_id,
                'class_inputs': self.class_inputs, 'config_inputs': self.config_inputs,
                'unknowns': self.unknowns}


def canonical_json(value):
    """The one serialization the hashes are taken over: sorted keys, no spaces, no surprises."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def sha256_16(value):
    """The id for one value: 16 hex digits of the canonical-JSON sha256, long enough to be unique here."""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()[:16]


def _manifest_field(manifest, *names):
    for name in names:
        if isinstance(manifest.get(name), str) and manifest[name]:
            return manifest[name]
    return None


def _engine_of(manifest):
    """Engine repository plus version and revisions, from either manifest shape (Strata flat, TensorFold nested)."""
    nested = manifest.get('engine') if isinstance(manifest.get('engine'), dict) else {}
    return {'repository': _manifest_field(manifest, 'repository') or nested.get('repository') or UNKNOWN,
            'version': _manifest_field(manifest, 'source_version', 'version') or nested.get('version') or UNKNOWN,
            'revision': _manifest_field(manifest, 'source_revision', 'revision') or nested.get('revision') or UNKNOWN,
            'ggml_revision': manifest.get('ggml_revision') or None}


def model_directory(manifest):
    """Where a manifest that carries its model as files says they are, relative to a host's project root.

    `backends/tf-1.1.4-qfn-mxfp8-nvfp4.json` declares `evaluations/spark-flash-next/nvidia-fp4local-b9ce18a6887e`
    and Spark's TensorFold worker reads its shards from there; `ops storage` lists that same path as a weight
    directory the host holds. A manifest that keeps its weights inside its engine root - the flat `files` shape -
    declares no directory, and its weights are read from `<root>/data/models` instead.
    """
    model = manifest.get('model') if isinstance(manifest, dict) else None
    if not isinstance(model, dict) or not model.get('directory'):
        return None
    return str(model['directory']).strip('/')


def service_root(manifest, host_name):
    """The worker directory `workers.<host>.service.root` declares for HOST_NAME, or None when it declares none.

    Read as declared, not validated: whether a service block is *usable* is decided by the path that starts a claim
    (`scripts/borrow.py`'s `service_spec`), which refuses a root that is relative, empty or contains `..`. The
    readers of this answer only need to know which directories this checkout already names for the host - the
    storage ledger, the retirement ledger, `ops storage` - where an unusable declaration is reported by the command
    that reads it.
    """
    workers = manifest.get('workers') if isinstance(manifest, dict) else None
    worker = workers.get(host_name) if isinstance(workers, dict) else None
    service = worker.get('service') if isinstance(worker, dict) else None
    root = service.get('root') if isinstance(service, dict) else None
    if not isinstance(root, str) or not root.strip('/'):
        return None
    return root.rstrip('/')


def compute_target(graph, target):
    """The TargetIdentity of one route target: every input read from the declared graph, nothing typed by hand.
    A retired key (naming stage 2) names its new target; names are not inputs of the identity."""
    from agentiac import route_aliases
    target = route_aliases.canonical(graph.route, target)
    spec = graph.route['targets'][target]
    node = spec.get('node', UNKNOWN)
    backend = spec.get('backend') or spec.get('source_alias')
    manifest = graph.backends.get(backend)
    unknowns = {}
    if manifest is None:
        # no manifest means no weights, quantization or engine: the id stays stable and says so (T4.1 must refuse it)
        manifest, unknowns['backend'] = {}, f'{backend} has no backend manifest'

    nested_model = manifest.get('model') if isinstance(manifest.get('model'), dict) else {}
    files = manifest.get('files') or nested_model.get('files') or []
    weight_hashes = sorted(f['sha256'] for f in files if f.get('sha256'))
    if not weight_hashes:
        unknowns['weights'] = f'{backend} declares no file sha256'
    quantization = _manifest_field(manifest, 'quantization', 'model')
    if quantization is None:
        unknowns['quantization'] = f'{backend} declares no quantization'
    chat_template = spec.get('thinking') or manifest.get('served_model') or manifest.get('family')
    if chat_template is None:
        unknowns['chat_template'] = f'neither the target nor {backend} declares thinking, served_model or family'
    for name, where in (('context_window', 'the route target'), ('max_tokens', 'the route target')):
        if name not in spec:
            unknowns[name] = f'{where} declares no {name}'
    class_args = manifest.get('class_args')
    if class_args is None:
        unknowns['class_args'] = f'{backend} declares no class_args (output-affecting launch arguments)'
    config_args = manifest.get('config_args')
    if config_args is None:
        unknowns['config_args'] = f'{backend} declares no config_args (performance-only launch arguments)'

    class_inputs = {
        'weights': {'repository': manifest.get('model_repository') or nested_model.get('repository') or UNKNOWN,
                    'revision': manifest.get('model_revision') or nested_model.get('revision') or UNKNOWN,
                    'sha256': weight_hashes},
        'quantization': quantization or UNKNOWN,
        'engine': _engine_of(manifest),
        'context': spec.get('context_window', UNKNOWN),
        'thinking': spec.get('thinking', UNKNOWN),
        'chat_template': chat_template or UNKNOWN,
        'max_tokens': spec.get('max_tokens', UNKNOWN),
        'class_args': sorted(class_args) if class_args is not None else UNKNOWN,
    }
    class_id = sha256_16(class_inputs)

    worker = (manifest.get('workers') or {}).get(node, {})
    slots = worker.get('slots', worker.get('pool_workers', UNKNOWN))
    kv_format = worker.get('kv_cache_type', manifest.get('kv_cache_type', UNKNOWN))
    for name, value in (('slots', slots), ('kv_format', kv_format)):
        if value == UNKNOWN:      # an input the id is taken over, so it is listed like the class inputs above
            unknowns[name] = f'{backend} declares no {name} for {node}'
    from agentiac import declared
    config_inputs = {
        'class_id': class_id,
        'node': declared.identity_name(node, getattr(graph, 'nodes', None) or {}),   # a renamed host keeps its ids (T8.10)
        'slots': slots,
        'kv_format': kv_format,
        'config_args': sorted(config_args) if config_args is not None else UNKNOWN,
    }
    return TargetIdentity(target=target, node=node, class_id=class_id,
                          config_id=sha256_16(config_inputs),
                          class_inputs=class_inputs, config_inputs=config_inputs, unknowns=unknowns)


def compute_all(graph):
    """target name -> TargetIdentity, for every target of the rendered route."""
    return {target: compute_target(graph, target) for target in graph.route['targets']}


def hardware_fingerprint(facts):
    """hw_fp from PA §6: hash(CPU, GPU type and count, memory total, driver); unknown if any is missing."""
    if not all(facts.get(name) not in (None, '') for name in HW_FP_FACTS):
        return UNKNOWN
    return sha256_16({name: facts[name] for name in HW_FP_FACTS})
