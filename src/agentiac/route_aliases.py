"""Explicit API aliases to existing route targets; no target copies, alias chains or prefix guessing.

Two route-file formats are understood:

- version 1: target keys are legacy names; the gateway publishes `local-coder-<target>` for every target, and
  `short_aliases` maps node-first names to target keys.
- version 2 (naming migration stage 2): target keys are the node-first names and are themselves the API names;
  `legacy_targets` maps each retired key to its new key. A retired key stays a valid selector and its old API
  name `local-coder-<old key>` keeps answering, so saved routes, logs and evaluation history resolve unchanged.
  No `local-coder-<new key>` is generated.
"""
import re

NAME = r'[a-z0-9][a-z0-9._-]*'


def version(routes):
    return routes.get('version', 1)


def legacy(routes):
    return routes.get('legacy_targets', {}) if version(routes) >= 2 else {}


def legacy_sources(routes):
    """Retired gateway transport names -> their current transport (naming stage 3). route.render publishes each
    retired name as a copy of its current transport, so old callers keep working until stage 4."""
    return routes.get('legacy_sources', {}) if version(routes) >= 2 else {}


def structural_problems(routes, base_names=()):
    found = []
    aliases = routes.get('short_aliases', {})
    retired = routes.get('legacy_targets', {})
    if not isinstance(aliases, dict):
        return ['short_aliases must be a mapping']
    if not isinstance(retired, dict):
        return ['legacy_targets must be a mapping']
    if version(routes) not in (1, 2):
        return [f'unknown route file version {version(routes)!r}']
    if retired and version(routes) < 2:
        found.append('legacy_targets needs route file version 2')
    sources = routes.get('legacy_sources', {})
    if not isinstance(sources, dict):
        return found + ['legacy_sources must be a mapping']
    if sources and version(routes) < 2:
        found.append('legacy_sources needs route file version 2')
    targets = set(routes['targets'])
    old_api = {f"{routes['alias']}-{old}" for old in retired}
    if version(routes) >= 2:
        published = targets | {routes['alias']} | old_api
        for target in targets:
            if target in base_names:
                found.append(f'target {target!r} collides with a gateway base alias')
            if not re.fullmatch(NAME, target):
                found.append(f'target {target!r} must be a lowercase API name')
    else:
        published = {routes['alias']} | {f"{routes['alias']}-{target}" for target in targets}
    reserved = set(base_names) | targets | published
    for alias, target in aliases.items():
        if not isinstance(alias, str) or not re.fullmatch(NAME, alias):
            found.append(f'short alias {alias!r} must be a lowercase API name')
        if alias in reserved:
            found.append(f'short alias {alias!r} collides with an existing name')
        if not isinstance(target, str) or target not in targets:
            found.append(f'short alias {alias!r} must reference an existing target directly: {target!r}')
    for old, new in sources.items():
        if not isinstance(old, str) or not re.fullmatch(NAME, old):
            found.append(f'legacy source {old!r} must be a lowercase name')
        if old in base_names or old in reserved or old in retired:
            found.append(f'legacy source {old!r} collides with a current name')
        if base_names and (not isinstance(new, str) or new not in base_names):
            found.append(f'legacy source {old!r} must map to a gateway base alias directly: {new!r}')
    for old, new in retired.items():
        if not isinstance(old, str) or not re.fullmatch(NAME, old):
            found.append(f'legacy target {old!r} must be a lowercase name')
        if old in targets or old in aliases or old == routes['alias']:
            found.append(f'legacy target {old!r} collides with a current target or alias')
        if f"{routes['alias']}-{old}" in set(base_names) | targets | set(aliases):
            found.append(f'legacy API name {routes["alias"]}-{old} collides with a current name')
        if not isinstance(new, str) or new not in targets:
            found.append(f'legacy target {old!r} must map to an existing target directly: {new!r}')
    return found


def require_valid(routes, base_names=()):
    problems = structural_problems(routes, base_names)
    if problems:
        raise ValueError('; '.join(problems))


def resolve_target(routes, name):
    """Resolve a route/model selector to a current target key. Retired keys resolve through legacy_targets."""
    require_valid(routes)
    if name in routes['targets']:
        return name
    if name in routes.get('short_aliases', {}):
        return routes['short_aliases'][name]
    if name in legacy(routes):
        return legacy(routes)[name]
    raise ValueError(f'unknown local-coder target or short alias {name!r}')


def canonical(routes, name):
    """NAME as a current target key: itself, or the new key of a retired one (version 2). An unknown name is
    returned unchanged so the caller's own lookup fails as before."""
    if name in routes['targets']:
        return name
    return legacy(routes).get(name, name)


def selectors(routes):
    """Every accepted selector: current targets, short aliases, then retired keys (still accepted)."""
    return [*routes['targets'], *routes.get('short_aliases', {}), *legacy(routes)]


def retired_names(routes):
    """Accepted but no longer offered: retired keys, their `local-coder-<old>` API names and retired transport
    names (version 2)."""
    return {name for old in legacy(routes) for name in (old, f"{routes['alias']}-{old}")} | set(legacy_sources(routes))


def api_targets(routes, selected):
    """All rendered API names and the target key each serves."""
    require_valid(routes)
    selected = resolve_target(routes, selected)
    if version(routes) >= 2:
        return {routes['alias']: selected,
                **{target: target for target in routes['targets']},
                **{f"{routes['alias']}-{old}": new for old, new in legacy(routes).items()},
                **routes.get('short_aliases', {})}
    return {routes['alias']: selected,
            **{f"{routes['alias']}-{target}": target for target in routes['targets']},
            **routes.get('short_aliases', {})}


def target_for_api(routes, alias, selected):
    require_valid(routes)
    if alias == routes['alias']:
        try:
            return resolve_target(routes, selected)
        except ValueError:
            return None
    if alias in routes.get('short_aliases', {}):
        return routes['short_aliases'][alias]
    if version(routes) >= 2 and alias in routes['targets']:
        return alias
    prefix = routes['alias'] + '-'
    if isinstance(alias, str) and alias.startswith(prefix):
        target = alias[len(prefix):]
        if version(routes) >= 2:
            return legacy(routes).get(target)
        return target if target in routes['targets'] else None
    return None
