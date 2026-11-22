"""Catalog service: the declared, state and evidence blocks, wrapped without merging them.

The caller must supply `current` from the runtime route. A catalog target screen must not
default to the declared route while a temporary route is active.
"""
from agentiac import catalog


def targets(root, current, now=None):
    return catalog.targets(root, current=current, now=now)
