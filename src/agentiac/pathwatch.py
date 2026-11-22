"""T6.4 / Stage 5 discipline: is the declared path to a front answering, and did an attempt measure anything at all?

When an outside outage drops a control -> worker tunnel during a Suite run, every attempt that runs against
the dead path lands in the database as an ordinary `failed` case (`usage` all zero, `tools {}`, `stop_reason
"error"`, every acceptance check failed because nothing was written), turning connection failures into false
model failures.

So this module holds the two rules, in one place:

* **`front_status`** — the passive read of what a front says it serves (`GET /ondemand/status`, no `X-Ondemand-Load`
  header). `scripts/ondemand-proxy.py` answers it before it judges a request as use, so it can never load a model,
  touch an idle timer or bill a request. It is the same read `scripts/ladder.py` uses to refuse a mismatched backend
  (T4.1) and the same read `agentiac doctor` is limited to.
* **`void_attempt`** — an attempt whose stored fields say nothing reached the model is an infrastructure error, which
  `src/agentiac/failures.py` already classes as `infrastructure` and `scripts/suite.py` already keeps out of the
  score ("infrastructure errors (not scored)"). Named here rather than in the runner so the Suite, the failure report
  and any later reader get the same answer.

`path_ready` ties them to a declaration: the alias's base comes from the rendered `artifacts/gateway/routes.json`, and
the host whose `tunnel_port` (infra/inventory.yml) matches that base supplies the key name. An alias whose base is not
one of the declared tunnels is answered as ready with that said out loud — the gate reports what it checked.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from agentiac import paths

ROOT = paths.root()

# What an attempt has to look like before "nothing reached the model" is the honest reading of it. Each field is one
# the runner already stored (`scripts/suite.py`), so the reading can be checked against a row without opening a trace.
VOID_MAX_WALL_S = 60.0


def status_root(base):
    """The front's root, whatever shape the base arrived in.

    `artifacts/gateway/routes.json` stores a bare `http://host:port` while `gateway/config.yaml` writes
    `http://127.0.0.1:18103/v1`, and the status lives at the root of the front, not under `/v1`. A read that assumed
    one shape would report a live front as "did not answer", which is the kind of wrong that makes people look at the
    wrong machine.
    """
    root = str(base).rstrip('/')
    if root.endswith('/v1'):
        root = root[:-len('/v1')].rstrip('/')
    return root + '/ondemand/status'


def front_answer(base, key, timeout=15):
    """Read the front once and keep what the reading itself says: (report, http, error).

    `report` is None unless a front answered with a JSON object. `http` and `error` are kept because a watch has to
    tell a refused credential (401, the path is up and the key is wrong) from nothing listening at all (the tunnel is
    down) - the two look the same from the caller's side and mean different repairs.
    """
    headers = {'Authorization': 'Bearer ' + key} if key else {}   # an empty key is no credential: ask without one
    request = urllib.request.Request(status_root(base), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode()
            code = response.status
    except urllib.error.HTTPError as refused:
        return None, refused.code, (refused.read().decode() or '')[:120]
    except Exception as failure:            # connect failure, timeout, no listener, unreadable body
        return None, None, f'{type(failure).__name__}: {failure}'[:120]
    try:
        report = json.loads(body)
    except json.JSONDecodeError:
        return None, code, 'the answer was not JSON'
    return (report if isinstance(report, dict) else None), code, None


def front_status(base, key, timeout=15):
    """What the front at BASE reports, or None when it does not answer. Never asks for a load.

    The whole status document (`state`, the idle countdown, `identity`), not only the identity: the same read answers
    "is the path up", "what is loaded" and "is this the backend the alias declares".
    """
    report, _code, _error = front_answer(base, key, timeout)
    return report


def tunnel_hosts(root=None):
    """`{port: (host, key_name)}` for the control tunnels infra/inventory.yml declares.

    The file nests hosts under `all.children.<group>.hosts.<host>` (ansible's own shape), and a tunnel is declared
    per host as `tunnel_port`, so this walks rather than assuming one depth - a host moved between groups stays found.
    """
    inventory = yaml.safe_load(((Path(root) if root else ROOT) / 'infra/inventory.yml').read_text())

    def walk(node, found):
        if isinstance(node, dict):
            for name, facts in node.items():
                if isinstance(facts, dict) and facts.get('tunnel_port'):
                    found[int(facts['tunnel_port'])] = (str(name), f'{str(name).upper()}_API_KEY')
                elif isinstance(facts, dict):
                    walk(facts, found)
        return found

    return walk(inventory.get('all') or {}, {})


def declared_path(root=None, alias=None, routes=None, tunnels=None):
    """(base, key_name, host) for an alias, read from the rendered route table, or None when there is none.

    `tunnels` may be supplied by a test; left alone it reads the inventory. A test that reached the real ports would
    be reading a live front, which is exactly what this module is supposed to leave alone.
    """
    routes = routes or json.loads(((Path(root) if root else ROOT) / 'artifacts/gateway/routes.json').read_text())
    entry = routes.get(alias)
    if not entry:
        return None
    base = entry[0] if isinstance(entry, list) else entry.get('api_base')
    port = int(str(base).rsplit(':', 1)[1].split('/')[0])
    host_key = (tunnels if tunnels is not None else tunnel_hosts(root)).get(port)
    if not host_key:
        return base, None, None
    host, key_name = host_key
    return base, key_name, host


def void_attempt(result):
    """Why an attempt that measured nothing is not a model result, or None when it is one.

    When a control -> worker tunnel is down, the Agent's first request fails at the gateway's connect guard, pi
    stops on an error, nothing is written, and the grader then fails every acceptance check of the untouched base.
    A model that was asked and answered is not that: its attempt has tokens, tool calls and a diff. Only a failure
    to reach anything is.
    """
    if result.get('passed') or result.get('infrastructure_error'):
        return None
    if result.get('timed_out'):
        return None                                       # a timeout is its own class, and a real run produced it
    if (result.get('wall_s') or 0.0) > VOID_MAX_WALL_S:
        return None
    if (result.get('usage') or {}).get('totalTokens'):
        return None                                       # something was generated; the model answered
    if result.get('tools'):
        return None                                       # the Agent did work, whatever the reason it stopped for
    if result.get('diff_stat'):
        return None                                       # it changed the workspace, so it was measured
    if result.get('stop_reason') not in ('error', None):
        return None
    return (f'the run stopped on an error after {result.get("wall_s")} s with no tokens counted, no tool call and no '
            'change to the workspace, so nothing reached the model to measure')


def gateway_keys(root=None):
    """The gateway service's own credential file, or nothing when it is not there. Values are never returned to a log."""
    try:
        from agentiac.secrets import read_env
        return read_env((Path(root) if root else ROOT) / 'secrets/gateway.env')
    except (OSError, ValueError):
        return {}


def path_ready(root=None, alias=None, env_keys=None, routes=None, tunnels=None):
    """Ask the front the alias is declared on whether it answers. Returns (ready, what was checked, or why not).

    Nothing here loads, claims, warms or unloads anything: it is the passive read, once. It reports what it checked so
    a run can say so out loud, and it only ever says *not ready* when a declared front was asked and did not answer -
    a missing route table, a base that is not a declared tunnel or an unreadable credential is said as what it is and
    does not stop a run, because those are configuration or credential problems, not a path being down.
    """
    try:
        found = declared_path(root, alias, routes=routes, tunnels=tunnels)
    except (OSError, ValueError, KeyError, IndexError):
        return True, 'no rendered route table to check the alias against'
    if found is None:
        return True, f'{alias} is not in the rendered route table, so there is nothing to check'
    base, key_name, host = found
    if key_name is None:
        return True, f'{base} is not a declared control tunnel, so the path was not read'
    keys = gateway_keys(root) if env_keys is None else env_keys
    if not keys.get(key_name):
        return True, f'no {key_name} credential was readable, so the path to {host} was not read'
    status, http, _error = front_answer(base, keys[key_name])
    if http in (401, 403):
        # Something answered, so the path is up; the credential is what is wrong. Saying "down" here would send
        # whoever reads it to look at a tunnel that is fine.
        return False, f'{base} (host {host}) refused the {key_name} credential with http {http}: the path is up, the key is not accepted'
    if status is None:
        return False, (f'the front declared for {alias} at {base} (host {host}) did not answer GET /ondemand/status, '
                       'so the declared path to it is down')
    identity = status.get('identity') or {}
    return True, (f'{host} answered at {base}: state {status.get("state")}, serving '
                  f'{identity.get("backend_id") or "nothing declared"}')
