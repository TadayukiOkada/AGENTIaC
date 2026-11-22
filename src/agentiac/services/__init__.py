"""Read services reused by the management API.

This package is a boundary, not a second source of truth: inventory comes from the YAML the
CLI reads, status uses the same transport and endpoint calls, catalog reads the target screen,
agents read pins and capabilities, and configuration reads the declaration graph. None of
these services runs playbooks, systemd, SSH or model requests; those remain the CLI/operations
surface.
"""
from . import inventory, status, catalog, agents, configuration
