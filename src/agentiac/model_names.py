"""Descriptive names for declared configurations, without renaming API aliases or historical cases.

A node is a machine; a target is a selectable declaration; an API alias is a published endpoint name.
The full name below describes a declaration, not a claim that the worker has loaded it. The suffix covers
all backend/target fields, including pins omitted from the readable prefix. It is independent of alias spelling.
"""
import hashlib
import re

from agentiac import identities

_QUANT = re.compile(r'(?<![A-Za-z0-9])(?:UD[-_])?(?:IQ|TQ|Q)\d+_[A-Za-z0-9_]+(?![A-Za-z0-9])', re.I)


def slug(value):
    return re.sub(r'[^a-z0-9._]+', '-', str(value).lower()).strip('-') or 'unknown'


def describe(graph, target):
    from agentiac import route_aliases
    target = route_aliases.canonical(graph.route, target)
    spec = graph.route['targets'][target]
    backend = spec.get('backend') or spec.get('source_alias')
    manifest = graph.backends.get(backend) or {}
    identity = identities.compute_target(graph, target)
    model = manifest.get('model') if isinstance(manifest.get('model'), dict) else {}
    repository = manifest.get('model_repository') or model.get('repository') or 'unknown'
    revision = manifest.get('model_revision') or model.get('revision') or 'unknown'
    quantization = manifest.get('quantization')
    quantization_source = 'manifest.quantization'
    if not quantization:
        labels = sorted({match.group().lower() for entry in manifest.get('files', [])
                         for match in _QUANT.finditer(entry['path'])})
        quantization = '+'.join(labels) or 'unknown'
        quantization_source = 'weight filename labels' if labels else 'not declared'
    variant = manifest.get('model') if isinstance(manifest.get('model'), str) else None
    model_label = repository + (f'-{variant}' if variant and slug(variant) != slug(quantization) else '')
    engine = identity.class_inputs['engine']
    engine_name = engine['repository'].rstrip('/').rsplit('/', 1)[-1].removesuffix('.git')
    # Alias names, the backend's file name and transport selection are deliberately separate from the named
    # configuration: the manifest itself (its content, including its runtime id) is part of the definition below.
    configuration = {key: value for key, value in spec.items() if key not in ('source_alias', 'backend')}
    sources = [entry for entry in graph.gateway_config.get('model_list', [])
               if entry['model_name'] == spec['source_alias']]
    # Include effective inference defaults while keeping transport credentials out of both names and JSON.
    source_parameters = {key: value for key, value in sources[0]['litellm_params'].items()
                         if key not in ('api_key', 'api_base')} if len(sources) == 1 else None
    if source_parameters is not None and 'extra_body' in spec:
        source_parameters['extra_body'] = spec['extra_body']  # route.target_params replaces this field
    definition = {'backend_manifest': manifest, 'configuration': configuration,
                  'source_parameters': source_parameters}
    declaration_id = hashlib.sha256(identities.canonical_json(definition).encode()).hexdigest()[:16]
    full_name = '--'.join((slug(spec['node']), slug(f"{engine_name}-{engine['version']}"),
                           slug(model_label), slug(quantization), f"ctx{spec.get('context_window', 'unknown')}",
                           f"out{spec.get('max_tokens', 'unknown')}", f'decl{declaration_id}'))
    return {'full_name': full_name, 'scope': 'declared-configuration', 'declaration_id': declaration_id,
            'backend': backend, 'backend_file': f'backends/{backend}.json',
            'engine': engine, 'engine_binary': manifest.get('engine_binary'),
            'engine_package': {key: value for key, value in (manifest.get('engine_package') or {}).items()
                               if key not in ('note', 'license_read_from')},
            'model_repository': repository, 'model_revision': revision, 'variant': variant,
            'quantization': quantization, 'quantization_source': quantization_source,
            'configuration': configuration, 'source_parameters': source_parameters,
            'short_aliases': [alias for alias, selected in graph.route.get('short_aliases', {}).items()
                              if graph.route['targets'].get(selected) == spec],
            # Version 2: retired keys that now resolve to this target (naming migration stage 2).
            'legacy_names': [old for old, new in graph.route.get('legacy_targets', {}).items()
                             if graph.route.get('version', 1) >= 2 and new == target],
            'worker_settings': (manifest.get('workers') or {}).get(spec['node'], {}),
            'identity_unknowns': identity.unknowns}


def bindings(graph):
    """One explicit binding per declared configuration; duplicate legacy targets remain aliases."""
    groups = {}
    for target in graph.route['targets']:
        description = describe(graph, target)
        name = description['full_name']
        if name not in groups:
            groups[name] = {**description, 'target_aliases': [], 'short_aliases': [], 'api_aliases': []}
        groups[name]['target_aliases'].append(target)
        if graph.route.get('version', 1) >= 2:
            groups[name]['api_aliases'].append(target)
            groups[name]['api_aliases'] += [f"{graph.route['alias']}-{old}" for old in description['legacy_names']]
        else:
            groups[name]['api_aliases'].append(f"{graph.route['alias']}-{target}")
    for alias, target in graph.route.get('short_aliases', {}).items():
        if target in graph.route['targets']:
            group = groups[describe(graph, target)['full_name']]
            group['short_aliases'].append(alias)
            group['api_aliases'].append(alias)
    for group in groups.values():
        try:
            group['recommended_short_alias'] = short_name(graph, group['target_aliases'][0])
        except ValueError:
            group['recommended_short_alias'] = None
    return list(groups.values())


def documentation(graph):
    lines = ['# Model names and alias bindings', '',
             'Generated from the route targets and backend manifests by `agentiac model names --write`.',
             'These are **declared configuration names**, not newly published gateway model IDs.',
             'Node-first short aliases and legacy aliases below share target definitions; new names require gateway deployment.', '',
             '| short aliases | legacy target aliases | declared full name | declared API aliases |', '|---|---|---|---|']
    if graph.route.get('version', 1) >= 2:
        lines[2:5] = ['Generated from the route targets and backend manifests by `agentiac model names --write`.',
                      'Route targets are named node-first (route file version 2); retired names still resolve.', '',
                      '| name | retired names (still accepted) | declared full name | API names |']
    for row in bindings(graph):
        first = row['short_aliases'] if graph.route.get('version', 1) < 2 else row['target_aliases']
        second = row['target_aliases'] if graph.route.get('version', 1) < 2 else row['legacy_names']
        lines.append(f"| {', '.join(first)} | {', '.join(second)} | `{row['full_name']}` | {', '.join(row['api_aliases'])} |")
    return '\n'.join(lines) + '\n'


def short_name(graph, target):
    """Recommended node-first API spelling: `<node>-<backend name>-<context>`, the backend name being what
    backend_names.derived gives the target's manifest (abbreviations apply only to the exact declared families)."""
    from agentiac import backend_names
    spec = graph.route['targets'][target]
    manifest = graph.backends.get(spec.get('backend') or spec.get('source_alias')) or {}
    backend = backend_names.derived(manifest)
    context = spec.get('context_window')
    if backend is None or not isinstance(context, int) or isinstance(context, bool) or context <= 0:
        raise ValueError(f'{target}: a short alias needs declared node/engine/version/model/quantization/context')
    window = f'{context // 1024}k' if context % 1024 == 0 else str(context)
    return '-'.join((slug(spec['node']), backend, window))


def names_from(entries):
    """Display name per target: its own node-first alias, else one of a target with the same declaration
    (a duplicate target such as a bare node name), else the target key itself. ENTRIES maps target -> (declaration_id,
    [node-first aliases that reference it directly]), in route order. Target keys stay the identifiers;
    this only chooses what people read (naming migration stage 1)."""
    by_declaration = {}
    for target, (declaration_id, aliases) in entries.items():
        if aliases:
            by_declaration.setdefault(declaration_id, aliases[0])
    return {target: (aliases[0] if aliases else by_declaration.get(declaration_id, target))
            for target, (declaration_id, aliases) in entries.items()}


def display_names(graph):
    if graph.route.get('version', 1) >= 2:
        return {target: target for target in graph.route['targets']}
    direct = {}
    for alias, target in graph.route.get('short_aliases', {}).items():
        direct.setdefault(target, []).append(alias)
    return names_from({target: (describe(graph, target)['declaration_id'], direct.get(target, []))
                       for target in graph.route['targets']})


def catalog_names(rows):
    """display_names for catalogue rows (`catalog.targets`), which already carry each target's naming block."""
    return names_from({row['target']: (row['declared']['naming']['declaration_id'],
                                       list(row['declared']['naming'].get('short_aliases') or []))
                       for row in rows})


def label(name, target):
    """`name (route key target)` when they differ, else just the name."""
    return name if name == target else f'{name} (route key {target})'


def alias_problems(graph):
    """Validate explicit bindings and their semantic hierarchy, without changing existing targets or ids."""
    from agentiac import route_aliases
    found = route_aliases.structural_problems(graph.route, graph.gateway_models)
    if found:
        return found

    for alias, target in graph.route.get('short_aliases', {}).items():
        try:
            expected = short_name(graph, target)
            qualified = expected + '-d' + describe(graph, target)['declaration_id']
            if alias not in (expected, qualified):
                found.append(f'short alias {alias!r} must follow node-engine-version-model-quant-context: '
                             f'use {expected!r} (or its declaration-qualified form)')
        except (KeyError, ValueError) as error:
            found.append(f'short alias {alias!r}: {error}')
    return found


def short_alias_coverage(graph):
    """Every declared configuration is reachable by a node-first name. A target that only repeats another
    target's declaration (the same declaration_id) is covered by that target's short alias. This is a
    declaration-completeness rule for `make check`, not a rendering precondition: a route switch or gateway
    render does not depend on it. In version 2 the target key itself must be the node-first name."""
    found = []
    if graph.route.get('version', 1) >= 2:
        for target in graph.route['targets']:
            try:
                expected = short_name(graph, target)
                qualified = expected + '-d' + describe(graph, target)['declaration_id']
                if target not in (expected, qualified):
                    found.append(f'target {target!r} must be named node-engine-version-model-quant-context: '
                                 f'rename it to {expected!r} (or its declaration-qualified form) and add the old key '
                                 'to legacy_targets')
            except (KeyError, ValueError) as error:
                found.append(f'target {target!r}: {error}')
        return found
    declaration = {target: describe(graph, target)['declaration_id'] for target in graph.route['targets']}
    covered = {declaration[target] for target in graph.route.get('short_aliases', {}).values()}
    for target, declaration_id in declaration.items():
        if declaration_id not in covered:
            try:
                suggestion = short_name(graph, target)
            except (KeyError, ValueError) as error:
                suggestion = f'<none: {error}>'
            found.append(f'target {target!r} has no node-first short alias: add '
                         f'"{suggestion}": "{target}" to gateway/local-coder.json short_aliases')
            covered.add(declaration_id)
    return found
