"""The two roots: where the code is, and where one installation's declarations and state are (M6, T6.2).

docs/implementation-plan/site-separation.md D6.2. `CODE` is the core checkout this module was imported from:
scripts, templates, lock files, virtual environments. The instance root holds the declarations
(`infra/inventory.yml`, `gateway/config.yaml`, `backends/`, ...) and the state (`secrets/`, `artifacts/`,
`private/`), at the same relative paths as today. `AGENTIAC_ROOT` names it once the site repository pins the core
as a submodule; unset or empty means an unsplit checkout, where both roots are the same directory and nothing changes.
"""
import os
from pathlib import Path

CODE = Path(__file__).resolve().parents[2]      # the core checkout this module was imported from
ENV = 'AGENTIAC_ROOT'
CORE_DIR = 'core'     # where a site checkout pins the core (D6.1)


def root(env=os.environ):
    """The instance root: declarations and state. $AGENTIAC_ROOT, or CODE in an unsplit checkout."""
    value = env.get(ENV)
    return Path(value).resolve() if value else CODE


def installed_core(instance=None):
    """The core an installation runs: INSTANCE/core once the site pins it there (D6.1), INSTANCE itself before.

    Not necessarily `CODE`: the dashboard runs from a release copy of part of the code, and a development clone can
    run against the site, while the installation's declarations describe the core the site pins.
    """
    instance = Path(instance) if instance is not None else root()
    pinned = instance / CORE_DIR
    return pinned if pinned.is_dir() else instance


def locate(relative, instance=None):
    """The file a repository-relative path names in an installation: a pin's lock, an agent's pin, a task manifest.

    The side comes from `layout`, the one table that decides it: site paths are in INSTANCE, code paths in the core
    INSTANCE runs. Code this process itself imports, executes or renders is `CODE` instead.
    """
    from agentiac import layout
    instance = Path(instance) if instance is not None else root()
    return (installed_core(instance) if layout.side(str(relative)) == 'core' else instance) / relative
