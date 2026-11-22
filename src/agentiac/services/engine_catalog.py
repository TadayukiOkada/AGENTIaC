"""Pure projection of backend uses and catalogue history (Engines UX E1).

No runtime reads, transport calls or retirement gates. The caller supplies one declaration
graph and, optionally, the selected route. Catalogue metadata never feeds inference identity.
Release grouping stays with the existing Engines view until E2 integrates this projection.
"""
from pathlib import Path

from agentiac import backend_lifecycle, backend_names, retention, route_aliases

ROLES = {'serving': 'default', 'rollback': 'rollback', 'candidate': 'evaluation',
         'quarantine': 'quarantined', 'retired': 'retired'}
ROLE_ORDER = ('default', 'rollback', 'evaluation', 'quarantined', 'retired', 'declared_only')


def project(graph, current=None):
    """Return per-backend lifecycle plus all issues, including unassignable references.

    Existing malformed metadata raises ValueError. Semantic contradictions remain visible as
    issues; strict declaration checks can reject their error severity without hiding rows.
    """
    metadata = getattr(graph, 'backend_lifecycle', None)
    if metadata is not None:
        backend_lifecycle.validate(metadata)
    decisions = (metadata or {}).get('backends', {})
    legacy = getattr(graph, 'backend_legacy_names', {})
    rows = {stem: {'catalog_states': [{'backend': stem, 'status': 'declared'}],
                   'associations': [], 'references': [], 'issues': []}
            for stem in sorted(graph.backends)}
    issues = []

    def issue(code, backend, node, source, message, severity='error'):
        value = {'code': code, 'backend': backend, 'node': node, 'source_resource_id': source,
                 'message': message, 'severity': severity}
        if value not in issues:
            issues.append(value)
            if backend in rows:
                rows[backend]['issues'].append(value)

    def resolve(name):
        return backend_names.resolve(graph.backends, legacy, name)

    def reference(backend, node, kind, name, source):
        backend = resolve(backend)
        if backend not in rows:
            if backend in getattr(graph, 'profiles', {}):
                return  # llama.cpp profiles have no engine release in this catalogue
            issue('unknown_backend', backend, node, source, f'backend {backend} is not declared')
            return
        if node is not None and node not in graph.nodes:
            issue('unknown_node', backend, node, source, f'node {node} is not declared')
        value = {'backend': backend, 'node': node, 'kind': kind, 'id': name, 'source_resource_id': source}
        if value not in rows[backend]['references']:
            rows[backend]['references'].append(value)

    if current is not None:
        current = route_aliases.canonical(graph.route, current)
        if current not in graph.route['targets']:
            issue('unknown_current_target', None, None, 'route', 'the selected route target is not declared')
    for name, spec in sorted(graph.route['targets'].items()):
        backend = spec.get('backend')
        if backend is not None:
            reference(backend, spec['node'], 'target', name, f'models/{name}')
            if name == current:
                reference(backend, spec['node'], 'current', name, f'models/{name}')
    for node, facts in sorted(graph.nodes.items()):
        for key, backend in backend_lifecycle.defaults(node, facts, graph.backends):
            reference(backend, node, 'claim_default', key, f'nodes/{node}')
    for component in graph.components:
        lock = Path(component.get('pin', {}).get('lock', ''))
        if lock.parent.as_posix() != 'backends' or lock.suffix != '.json':
            continue
        for node in component.get('hosts') or [None]:
            reference(lock.stem, node, 'component', component['id'], f'component/{component["id"]}')

    # Exact ledger addresses only. No inference from an installation path, version or name prefix.
    entries = {}
    for node, block in sorted(graph.retention.items()):
        for state in retention.STATES:
            for entry in block.get(state, []):
                key = (node, state, retention.handle(entry))
                entries.setdefault(key, []).append(entry)
    claims = {}
    for stem, decision in sorted(decisions.items()):
        source = f'backend_lifecycle/{stem}'
        if stem not in rows:
            issue('unknown_catalog_backend', stem, None, source, f'catalogue key {stem} is not a canonical backend')
        else:
            rows[stem]['catalog_states'] = [{'backend': stem, 'status': decision['catalog_status'],
                                            'reason': decision['reason'], 'evidence': decision['evidence']}]
        evidence_known = getattr(graph, 'backend_lifecycle_evidence', {}).get(decision['evidence'])
        if evidence_known is False:
            issue('missing_evidence', stem, None, source, f'evidence {decision["evidence"]} does not exist')
        elif evidence_known is None:
            issue('unverified_evidence', stem, None, source, 'evidence availability was not captured', 'warning')
        for ref in decision.get('retention_refs', []):
            key = (ref['node'], ref['state'], ref['handle'])
            claims.setdefault(key, []).append(stem)
            matches = entries.get(key, [])
            if len(matches) != 1:
                issue('unresolved_retention_ref', stem, ref['node'], source,
                      'retention reference must name exactly one ledger entry')
            elif matches[0].get('backend'):
                issue('redundant_retention_ref', stem, ref['node'], source,
                      'backend-bearing ledger entries must be linked directly, not through the sidecar')
                direct = resolve(matches[0]['backend'])
                if direct != stem:
                    issue('conflicting_retention_ref', direct, ref['node'], source,
                          'the sidecar and ledger name different backends')

    def associate(stem, node, state, entry, source):
        if stem not in rows:
            issue('unknown_backend', stem, node, source['resource_id'], f'backend {stem} is not declared')
            return
        if node not in graph.nodes:
            issue('unknown_node', stem, node, source['resource_id'], f'node {node} is not declared')
        rows[stem]['associations'].append({'backend': stem, 'node': node, 'role': ROLES[state],
                                          'form': entry.get('form'),
                                          'retirement_intent': entry.get('retirement', 'unspecified'),
                                          'reason': entry.get('reason'), 'source': source})

    for key, matches in sorted(entries.items()):
        node, state, handle = key
        source = {'resource_id': f'retention/{node}', 'state': state, 'handle': handle}
        linked = claims.get(key, [])
        if len(linked) > 1:
            for stem in linked:
                issue('ambiguous_retention_ref', stem, node, source['resource_id'],
                      'multiple backends claim the same ledger entry')
        for entry in matches:
            direct = resolve(entry['backend']) if entry.get('backend') else None
            owners = [direct] if direct else linked
            for stem in owners:
                if len(matches) > 1:
                    issue('duplicate_ledger_entry', stem, node, source['resource_id'],
                          'multiple ledger entries share the same address')
                associate(stem, node, state, entry, source)

    for stem, row in rows.items():
        associations = row['associations']
        for ref in row['references']:
            same_node = [a for a in associations if a['node'] == ref['node']]
            if ref['kind'] == 'claim_default' and not any(a['role'] == 'default' for a in same_node):
                if any('state' in a['source'] for a in same_node):
                    issue('default_retention_conflict', stem, ref['node'], ref['source_resource_id'],
                          'the claim default disagrees with the ledger use')
                else:
                    issue('missing_retention', stem, ref['node'], ref['source_resource_id'],
                          'the default has no retention entry', 'warning')
                associations.append({'backend': stem, 'node': ref['node'], 'role': 'default', 'form': None,
                                     'retirement_intent': 'unspecified', 'reason': None,
                                     'source': {'resource_id': ref['source_resource_id']}})
            elif not same_node:
                associations.append({'backend': stem, 'node': ref['node'], 'role': 'declared_only', 'form': None,
                                     'retirement_intent': 'unspecified', 'reason': None,
                                     'source': {'resource_id': ref['source_resource_id']}})
                issue('missing_retention', stem, ref['node'], ref['source_resource_id'],
                      'the referenced backend has no structured retention use', 'warning')
        if not associations:
            if row['catalog_states'][0]['status'] != 'archived':
                associations.append({'backend': stem, 'node': None, 'role': 'declared_only', 'form': None,
                                     'retirement_intent': 'unspecified', 'reason': None,
                                     'source': {'resource_id': f'backend/{stem}'}})
                issue('unclassified_backend', stem, None, f'backend/{stem}',
                      'no structured use or archival decision is declared', 'warning')
        for node in {a['node'] for a in associations}:
            ledger_roles = {a['role'] for a in associations if a['node'] == node and 'state' in a['source']}
            if ledger_roles & {'retired', 'quarantined'} and any(
                    ref['node'] in (node, None) for ref in row['references']):
                issue('retired_still_referenced', stem, node, f'retention/{node}',
                      'a retired or quarantined backend still has declared references')
            if len(ledger_roles) > 1:
                issue('conflicting_retention_states', stem, node, f'retention/{node}',
                      'the backend has multiple retention states on this node')
        archived = row['catalog_states'][0]['status'] == 'archived'
        if archived and (row['references'] or any(a['role'] != 'retired' for a in associations)):
            issue('archived_in_use', stem, None, f'backend_lifecycle/{stem}',
                  'an archived backend still has declared references or retained uses')
        row['roles'] = [role for role in ROLE_ORDER if any(a['role'] == role for a in associations)]
        row['nodes'] = sorted({a['node'] for a in associations if a['node'] is not None})
        row['coverage'] = ('unknown' if metadata is None else 'partial' if row['issues'] else 'complete')
        row['history_only'] = bool(metadata is not None and archived and not row['issues']
                                   and not row['references'] and all(a['role'] == 'retired' for a in associations))
    return {'backends': rows, 'issues': issues}


def release_lifecycle(projection, backends):
    """Aggregate already-projected backend uses without collapsing mixed releases."""
    rows = [projection['backends'][stem] for stem in backends]
    issues = [issue for row in rows for issue in row['issues']]
    # An unassignable error may conceal a reference to any release: keep every release visible.
    unassigned = [issue for issue in projection['issues'] if issue['backend'] not in projection['backends']]
    coverage = ('unknown' if any(row['coverage'] == 'unknown' for row in rows)
                else 'partial' if issues or unassigned else 'complete')
    return {'coverage': coverage,
            'catalog_states': [state for row in rows for state in row['catalog_states']],
            'roles': [role for role in ROLE_ORDER if any(role in row['roles'] for row in rows)],
            'nodes': sorted({node for row in rows for node in row['nodes']}),
            'history_only': bool(rows and not unassigned and all(row['history_only'] for row in rows)),
            'associations': [a for row in rows for a in row['associations']],
            'references': [ref for row in rows for ref in row['references']], 'issues': issues}
