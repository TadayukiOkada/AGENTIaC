"""Read-layer converters used by the API handlers.

They call the services and return typed, allowlisted DTOs. They do not run commands,
playbooks, SSH probes, model loads or route changes.
"""
import datetime
from pathlib import Path

from agentiac import backend_names, hardware
from agentiac.services import inventory, catalog, agents, configuration

from .dto import HardwareDTO, NodeDTO, AgentCapabilitiesDTO, AgentDTO, ConfigurationResourceDTO
from .runtime import current_target

GIB = 2 ** 30
HARDWARE_STALE_S = 30 * 24 * 3600


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def hardware_dto(record, now=None):
    if not record:
        return None
    facts = record.get('facts') or {}

    def value(name):
        entry = facts.get(name)
        return entry.get('value') if isinstance(entry, dict) else None

    total = value('memory_total')
    collected = record.get('collected_at')
    age = None
    stale = None
    if collected:
        try:
            collected_at = datetime.datetime.fromisoformat(collected)
            age = round(((now or _now()) - collected_at).total_seconds())
            stale = age > HARDWARE_STALE_S or age < -300
        except (ValueError, TypeError):
            age = None
            stale = None
    return HardwareDTO(gpu=value('gpu'), gpu_count=value('gpu_count'),
                      gpu_vram_mib=value('gpu_vram_mib'), driver=value('driver'),
                      memory_total_gib=round(total / GIB, 1) if isinstance(total, int) else None,
                      hw_fp=record.get('hw_fp') if stale is False else None, collected_at=collected, age_s=age,
                      stale=stale, unknowns=list(record.get('unknowns') or []))


def tensorfold_backend(backends, node_id, facts):
    """The TensorFold manifest a `spark_tensorfold` node serves, derived the way the claim derives it; None otherwise.

    Several manifests naming the node read as None here; a claim on that node refuses with the list (scripts/borrow.py).
    """
    if not facts.get('spark_tensorfold'):
        return None
    try:
        return backend_names.tensorfold_backend(backends, node_id)
    except ValueError:
        return None


def node_rows(root, ledger=None):
    nodes = inventory.nodes(root)
    backends = backend_names.manifests(root)
    out = []
    for node_id, facts in nodes.items():
        record = hardware.latest(hardware.directory(root), node_id)
        out.append(NodeDTO(id=node_id, host=facts.get('ansible_host'), user=facts.get('ansible_user'),
                          port=facts.get('backend_port'), node_class=facts.get('node_class'),
                          borrowed=bool(facts.get('borrowed')),
                          default_profile=facts.get('default_profile'),
                          spark_tensorfold=facts.get('spark_tensorfold'),
                          tensorfold_backend=tensorfold_backend(backends, node_id, facts),
                          default_backend=facts.get('default_backend'),
                          strata_default_backend=facts.get('strata_default_backend'),
                          runtime_image=facts.get('runtime_image'),
                          hardware=hardware_dto(record),
                          probe=ledger.probe_snapshot(node_id) if ledger else None))
    return out


def model_rows(root, current=None):
    current = current_target(root) if current is None else current
    rows = catalog.targets(root, current=current)
    return [{'target': row['target'], 'node': row['node'], 'current': row['current'],
            'declared': row['declared'], 'identity': row['identity'],
            'state': row['state'], 'evidence': row['evidence']} for row in rows]


def agent_rows(root):
    rows = agents.load(root)['agents']
    out = []
    for row in rows:
        capabilities = {}
        for feature, entry in row.get('capabilities', {}).items():
            capabilities[feature] = AgentCapabilitiesDTO(
                feature=feature, value=entry['value'], reason=entry['reason'],
                version=entry.get('version'), probed_at=entry.get('probed_at'))
        out.append(AgentDTO(id=row['id'], version=row.get('version'),
                          declared_version=row.get('declared_version'),
                          install=row.get('install'), entrypoint=row.get('entrypoint'),
                          endpoint=row.get('endpoint'), gateway_only=row.get('gateway_only'),
                          allowed_paths=row.get('allowed_paths') or [],
                          pin=row.get('pin'), capabilities=capabilities))
    return out


def safe_declaration(spec):
    """Public summary only. Never serialize arbitrary nested declaration dictionaries."""
    scalar_fields = {'id', 'name', 'schema_version', 'kind', 'owner', 'policy', 'repo',
                     'repository', 'revision', 'source_revision', 'source_version',
                     'model_repository', 'model_revision', 'family', 'quantization',
                     'publisher', 'version', 'endpoint', 'gateway_only', 'baseline', 'catalog_status', 'reason', 'evidence'}
    out = {key: value for key, value in spec.items()
           if key in scalar_fields and (value is None or isinstance(value, (str, bool, int, float)))}
    for key in ('hosts', 'nodes', 'allowed_paths'):
        if isinstance(spec.get(key), list):
            out[key] = [value for value in spec[key] if isinstance(value, str)]
    if isinstance(spec.get('files'), list):
        out['file_count'] = len(spec['files'])
    return out


def safe_route(route):
    return {'alias': route.get('alias'), 'default': route.get('default'),
            'targets': {target: {key: value for key, value in spec.items()
                                if key in {'node', 'backend', 'profile', 'context_window', 'max_tokens'}
                                and isinstance(value, (str, int, float, bool))}
                        for target, spec in route.get('targets', {}).items()}}


def configuration_rows(root, report=None):
    report = configuration.load(root) if report is None else report
    return [ConfigurationResourceDTO(id=res['id'], kind=res['kind'], declared=safe_declaration(res['declared']))
            for res in report['resources']]



# Snapshot builders for the cache: (rows, extra), each from one consistent read of the files.
# Node rows carry no probe snapshot; the API attaches the ledger's current probe per request.
def node_snapshot(root):
    return node_rows(root), {}


def model_snapshot(root):
    """Rows carry `name`, the node-first display name; the target key stays the row ID and URL."""
    from agentiac import model_names
    current = current_target(root)
    rows = model_rows(root, current)
    names = model_names.catalog_names(rows)
    from agentiac import route_aliases, declared
    retired = route_aliases.legacy(declared.load(root).route)
    return ([{**row, 'name': names[row['target']]} for row in rows],
            {'current': current, 'names': names, 'retired': dict(retired)})


def agent_snapshot(root):
    return agent_rows(root), {}


def configuration_snapshot(root):
    report = configuration.load(root)
    return configuration_rows(root, report), {'route': safe_route(report['route']),
                                               'references': report['references']}


SNAPSHOTS = {
    'nodes': (node_snapshot, lambda row: row.id),
    'models': (model_snapshot, lambda row: row['target']),
    'agents': (agent_snapshot, lambda row: row.id),
    'configuration': (configuration_snapshot, lambda row: row.id),
}
