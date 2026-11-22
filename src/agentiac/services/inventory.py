"""Inventory service: the declared hosts and the inventory vars merged exactly as the CLI does.

This is the read layer the management API can reuse. It never returns credentials: the inventory
file declares host address, user and port, not a secret.
"""
from pathlib import Path

import yaml

from agentiac import paths

ROOT = paths.root()


def nodes(root=ROOT):
    data = yaml.safe_load((Path(root) / 'infra/inventory.yml').read_text())['all']
    return {name: {**data['vars'], **host}
            for name, host in data['children']['workers']['hosts'].items()}
