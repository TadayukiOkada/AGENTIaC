"""Operator-configured declaration/state roots, independent of immutable release code."""
import os
from pathlib import Path

from agentiac import paths as roots

SOURCE_ROOT = Path(__file__).resolve().parents[3]
ROOT = Path(os.environ.get('AGENTIAC_MANAGEMENT_ROOT', roots.root())).resolve()
STATE_DIR = Path(os.environ.get('AGENTIAC_MANAGEMENT_STATE', ROOT / 'private/management')).absolute()
SESSION_PATH = STATE_DIR / 'session.sqlite'
# Where `dashboard plan --history` keeps receipts; the runbook's documented location by default.
DEPLOYMENT_HISTORY = Path(os.environ.get('AGENTIAC_DASHBOARD_HISTORY', ROOT / 'private/dashboard-deployment')).absolute()


def require_complete(state):
    state = Path(state)
    if (state / '.incomplete').exists():
        raise ValueError('management state is incomplete; use a verified restore destination')


def require_ready(state):
    require_complete(state)
    if (Path(state) / '.credential-review-required').exists():
        raise ValueError('restored credentials require offline owner rotation/recovery before service startup')
