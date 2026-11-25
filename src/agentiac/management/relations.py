"""Relations between declared resources, derived from the cached snapshots.

A relation names another resource list (`nodes`, `models`, `engines`, `agents`, `configuration` or `jobs`), the
other resource's ID and how it relates. Relations are read from fields the declarations already carry
(a target's node and backend, a node's defaults, a component's hosts, per-node retention). A declared
reference whose resource is not in its snapshot is kept with `exists: false` instead of being dropped,
so a broken declaration stays visible. Nothing here reads files, runs commands or opens connections.
"""
def _node_defaults(node):
    """(configuration ID, relation) for each default a node declares."""
    out = []
    if node.default_profile:
        out.append((f'profile/{node.default_profile}', 'default profile'))
    if node.strata_default_backend:
        out.append((f'backend/{node.strata_default_backend}', 'default Strata backend'))
    default_backend = getattr(node, 'default_backend', None)
    if default_backend:
        out.append((f'backend/{default_backend}', 'default backend'))
    if node.spark_tensorfold and node.tensorfold_backend and not default_backend:   # derived from the manifests by the node read (reads.py)
        out.append((f'backend/{node.tensorfold_backend}', 'TensorFold backend'))
    return out


def _backend_resource(configuration, backend):
    """A target's `backend` names a backend declaration or, for llama.cpp, a profile."""
    for candidate in (f'backend/{backend}', f'profile/{backend}'):
        if configuration.by_id(candidate) is not None:
            return candidate
    return f'backend/{backend}'


def _hosts(resource):
    hosts = resource.declared.get('hosts')
    return [host for host in hosts if isinstance(host, str)] if isinstance(hosts, list) else []


def related(kind, item_id, snapshots, jobs=()):
    """Relations of one resource. `snapshots` maps each list name to its Snapshot; `jobs` are job rows
    (newest first) whose target is this node. Returns None when the resource itself is not declared."""
    nodes, models, agents, configuration = (snapshots[name] for name in ('nodes', 'models', 'agents', 'configuration'))
    lists = {'nodes': nodes, 'models': models, 'agents': agents, 'configuration': configuration}
    engines = snapshots.get('engines')
    if engines is not None:
        lists['engines'] = engines

    def engine_of(backend_stem):
        for engine in engines.rows if engines is not None else ():
            if backend_stem in engine['backends']:
                return engine['id']
        return None
    row = lists[kind].by_id(item_id)
    if row is None:
        return None
    found = []

    def add(other, other_id, relation):
        found.append((other, other_id, relation))

    if kind == 'nodes':
        for model in models.rows:
            if model['node'] == item_id:
                add('models', model['target'], 'target on this node')
        for resource_id, relation in _node_defaults(row):
            add('configuration', resource_id, relation)
        if configuration.by_id(f'retention/{item_id}') is not None:
            add('configuration', f'retention/{item_id}', 'retention policy')
        for resource in configuration.rows:
            if resource.kind == 'component' and item_id in _hosts(resource):
                add('configuration', resource.id, 'component on this node')
    elif kind == 'models':
        add('nodes', row['node'], 'runs on')
        backend = row['declared'].get('backend')
        if isinstance(backend, str) and backend:
            resource_id = _backend_resource(configuration, backend)
            add('configuration', resource_id, 'backend' if resource_id.startswith('backend/') else 'profile')
            if engine_of(backend):
                add('engines', engine_of(backend), 'engine release')
    elif kind == 'engines':
        for backend in row['backends']:
            add('configuration', f'backend/{backend}', 'backend pinning it')
            if configuration.by_id(f'backend_lifecycle/{backend}') is not None:
                add('configuration', f'backend_lifecycle/{backend}', 'catalogue lifecycle')
        for target in row['targets']:
            add('models', target, 'target using it')
        for node_id in row['nodes']:
            add('nodes', node_id, 'node using it')
        for association in (row.get('lifecycle') or {}).get('associations', []):
            node_id = association.get('node')
            if node_id:
                add('nodes', node_id, 'node with declared use')
            source = association.get('source', {}).get('resource_id', '')
            if source.startswith('retention/'):
                add('configuration', source, 'retention record')
    elif kind == 'agents':
        add('configuration', f'agent/{item_id}', 'agent declaration')
        if configuration.by_id(f'component/{item_id}') is not None:
            add('configuration', f'component/{item_id}', 'component pin')
    else:
        resource_kind, _, stem = item_id.partition('/')
        if resource_kind == 'backend' and engine_of(stem):
            add('engines', engine_of(stem), 'engine release')
        if resource_kind in ('backend', 'profile'):
            for model in models.rows:
                backend = model['declared'].get('backend')
                if isinstance(backend, str) and _backend_resource(configuration, backend) == item_id:
                    add('models', model['target'], 'used by target')
            for node in nodes.rows:
                for resource_id, relation in _node_defaults(node):
                    if resource_id == item_id:
                        add('nodes', node.id, f'node {relation}')
        elif resource_kind == 'retention':
            add('nodes', stem, 'retention for node')
        elif resource_kind == 'agent':
            add('agents', stem, 'agent')
            if configuration.by_id(f'component/{stem}') is not None:
                add('configuration', f'component/{stem}', 'component pin')
        elif resource_kind == 'component':
            for host in _hosts(row):
                add('nodes', host, 'installed on')
            if agents.by_id(stem) is not None:
                add('agents', stem, 'agent')

    unique = sorted(set(found), key=lambda item: (item[0], item[2], item[1]))
    out = [{'kind': other, 'id': other_id, 'relation': relation,
            'exists': lists[other].by_id(other_id) is not None} for other, other_id, relation in unique]
    if kind == 'nodes':
        out += [{'kind': 'jobs', 'id': job['id'], 'relation': f"{job['operation']} job, {job['status']}",
                 'exists': True} for job in jobs]
    return out
