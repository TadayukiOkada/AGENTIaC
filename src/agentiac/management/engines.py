"""Engine releases derived from the cached model and configuration snapshots.

An engine row is one source release — repository, version and revision — with the backend
manifests that pin it and the targets and nodes that use those backends. A target's own declared
`engine` block wins for its backend; otherwise the backend manifest's `repository`/`source_version`/
`source_revision` are used. A backend that declares neither keeps its own row with the unknown fields
`None`, instead of a guessed engine. llama.cpp profiles carry no engine pin here and are not listed.
"""
import re
from dataclasses import replace
from types import SimpleNamespace

from agentiac import catalog, declared, model_names, route_aliases
from agentiac.services import engine_catalog
from .runtime import current_target


def _name(repository):
    if not repository:
        return None
    return re.sub(r'\.git$', '', repository.rstrip('/')).rsplit('/', 1)[-1].lower() or None


def _release(repository, version, revision):
    return (repository or None, version or None, revision or None)


def engine_rows(models, configuration):
    """Rows (dicts, sorted by id) and the snapshot fields they were derived from."""
    by_backend = {}
    for model in models.rows:
        declared = model['declared']
        backend, engine = declared.get('backend'), declared.get('engine')
        if isinstance(backend, str) and isinstance(engine, dict) and engine.get('repository'):
            by_backend[backend] = (_release(engine.get('repository'), engine.get('version'), engine.get('revision')),
                                   engine.get('ggml_revision'))
    groups = {}

    def group(release, ggml=None):
        entry = groups.setdefault(release, {'backends': set(), 'targets': set(), 'nodes': set(), 'current': False,
                                            'ggml_revisions': set()})
        if ggml:
            entry['ggml_revisions'].add(ggml)
        return entry

    for resource in configuration.rows:
        if resource.kind != 'backend':
            continue
        stem = resource.id.partition('/')[2]
        declared = resource.declared
        if stem in by_backend:
            release, ggml = by_backend[stem]
        else:
            release, ggml = _release(declared.get('repository'), declared.get('source_version'),
                                     declared.get('source_revision')), None
        if release[0] is None:
            release = ('backend:' + stem, None, release[2])  # undeclared engine: one row per backend
        group(release, ggml)['backends'].add(stem)
    for model in models.rows:
        backend = model['declared'].get('backend')
        for release, entry in groups.items():
            if backend in entry['backends']:
                entry['targets'].add(model['target'])
                entry['nodes'].add(model['node'])
                entry['current'] = entry['current'] or bool(model.get('current'))

    rows = []
    for (repository, version, revision), entry in groups.items():
        undeclared = repository.startswith('backend:')
        name = None if undeclared else _name(repository)
        base = f"{name}-{version}" if name and version else (name or repository.replace(':', '-'))
        rows.append({'id': base, 'name': name, 'repository': None if undeclared else repository,
                     'version': version, 'revision': revision,
                     'ggml_revisions': sorted(entry['ggml_revisions']),
                     'backends': sorted(entry['backends']), 'targets': sorted(entry['targets']),
                     'nodes': sorted(entry['nodes']), 'current': entry['current'],
                     'declared': not undeclared})
    # The same name/version at different revisions gets the short revision appended to stay unique.
    counts = {}
    for row in rows:
        counts[row['id']] = counts.get(row['id'], 0) + 1
    for row in rows:
        if counts[row['id']] > 1:
            row['id'] = f"{row['id']}@{(row['revision'] or 'unknown')[:7]}"
    return sorted(rows, key=lambda row: row['id'])


def engine_snapshot(models, configuration):
    """A Snapshot of engines, as old as the older of its two sources and stale if either is."""
    rows = engine_rows(models, configuration)
    older = models if models.built <= configuration.built else configuration
    error = models.error or configuration.error
    return replace(older, kind='engines', rows=tuple(rows), ids=tuple(row['id'] for row in rows), extra={},
                   error=error)



def build_engine_snapshot(root):
    """Build all lifecycle rows from one declaration graph and the passive route file."""
    graph = declared.load(root)
    current = current_target(root, graph.route)
    targets = catalog.targets(root, current=current, graph=graph)
    models = SimpleNamespace(rows=[{'target': row['target'], 'node': row['node'],
                                    'current': row['current'], 'declared': row['declared']}
                                   for row in targets])
    configuration = SimpleNamespace(rows=[SimpleNamespace(id=f'backend/{stem}', kind='backend', declared=spec)
                                          for stem, spec in graph.backends.items()])
    projection = engine_catalog.project(graph, current=current)
    rows = engine_rows(models, configuration)
    for row in rows:
        row['lifecycle'] = engine_catalog.release_lifecycle(projection, row['backends'])
    unassigned = [issue for issue in projection['issues'] if issue['backend'] not in graph.backends]
    names = model_names.catalog_names(targets)
    names.update({old: names.get(new, new) for old, new in route_aliases.legacy(graph.route).items()})
    return rows, {'lifecycle_issues': unassigned, 'names': names}
