"""Runtime route selection, read-only.

The deployed route file is runtime state; the tracked local-coder.json is the fallback
only when no deployment record exists.
"""
import json
from pathlib import Path

from agentiac import paths

ROOT = paths.root()


def current_target(root=ROOT, routes=None):
    """The current target key; a route saved under a retired key resolves to its new key (naming stage 2)."""
    from agentiac import route_aliases
    routes = routes if routes is not None else json.loads((Path(root) / 'gateway/local-coder.json').read_text())
    deployed = Path(root) / 'artifacts/gateway/route.json'
    if not deployed.exists():
        return routes['default']
    saved = json.loads(deployed.read_text())['target']
    try:
        return route_aliases.resolve_target(routes, saved)
    except ValueError:
        return saved
