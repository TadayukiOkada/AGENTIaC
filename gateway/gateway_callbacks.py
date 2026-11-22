"""Only allowlisted request metadata; never dump kwargs, prompts or responses."""
import asyncio
import datetime
import json
import logging
import os
from litellm.integrations.custom_logger import CustomLogger

import chatgpt_auth
import identity_policy
import task_policy

log = logging.getLogger('agentiac_metadata')

# What one inference line may contain (IP-06, T4.2). The record is built key by key from
# this list, so a field litellm adds later cannot widen it: kwargs carries `messages`,
# `original_response` and a whole standard_logging_object with the response in it, and none
# of that belongs in a log. tests/test_gateway_log.py fails on any key outside this set.
LOG_FIELDS = ('event', 'status', 'alias', 'node', 'model', 'stream',
              'latency_s', 'ttft_s', 'input_tokens', 'output_tokens')


class MetadataLogger(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if getattr(user_api_key_dict, 'user_id', None) == task_policy.TASK_USER \
                and not task_policy.model_allowed(data.get('model'), TASK_ALIASES):
            # The task key is pinned to the rendered local-coder aliases; refusing here also
            # refuses cloud-gpt-sol before it can reach the sign-in check (IP-01, T4.3).
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail=f'Alias {data.get("model")!r} is not allowed for the task key')
        user = getattr(user_api_key_dict, 'user_id', None)
        if user in task_policy.CODE_USERS and not task_policy.model_allowed(data.get('model'), _code_aliases(user)):
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail=f'Alias {data.get("model")!r} is not allowed for this key')
        if data.get('model') in SUBSCRIPTION:
            await asyncio.to_thread(_require_chatgpt_signin)
        else:
            await _verify_loaded_model(data.get('model'))
        return data

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        usage = getattr(response_obj, 'usage', None)
        self._log('success', kwargs, start_time, end_time,
                  input_tokens=getattr(usage, 'prompt_tokens', None),
                  output_tokens=getattr(usage, 'completion_tokens', None))

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        # a failure has to say which host failed, or two hosts serving one model look identical
        self._log('error', kwargs, start_time, end_time)

    def _log(self, status, kwargs, start_time, end_time, input_tokens=None, output_tokens=None):
        """One line per request: who served it, how long, how many tokens. Never the body."""
        record = {'event': 'inference', 'status': status,
                  'alias': _requested_alias(kwargs), 'node': _served_by(kwargs),
                  'model': kwargs.get('model'), 'stream': bool(kwargs.get('stream')),
                  'latency_s': (end_time - start_time).total_seconds(),
                  'ttft_s': _time_to_first_token(kwargs, start_time),
                  'input_tokens': input_tokens, 'output_tokens': output_tokens}
        log.warning(json.dumps({field: record[field] for field in LOG_FIELDS}))

metadata_logger = MetadataLogger()

# A single-model backend may ignore the request's model name. Check its actual
# loaded identity so a profile switch cannot silently change the served model.
# The tables come from artifacts/gateway/nodes.json, which scripts/route.py renders from gateway/config.yaml and
# infra/inventory.yml (D3.1). There is no built-in copy (M8, T8.6): a process started without the file has empty
# tables, so every local alias is refused (404, or 503 for a port with no key) instead of being checked against a
# list that no longer matches the declarations, and the start is logged as an error.
_GENERATED = {}
_NODES = os.environ.get('AGENTIAC_NODES')
if _NODES and os.path.exists(_NODES):
    with open(_NODES) as _nodes:
        _GENERATED = json.load(_nodes)
else:
    log.error(json.dumps({'event': 'nodes_missing', 'path': _NODES,
                          'detail': 'no generated node tables: local aliases are refused until the gateway '
                                    'restarts with artifacts/gateway/nodes.json'}))
ROUTES = {alias: tuple(route) for alias, route in _GENERATED.get('routes', {}).items()}
# Naming stage 3: retired transport names stay callable until stage 4 and are checked exactly like the
# transport they now copy. The one list of them is gateway/local-coder.json `legacy_sources`, next to this file.
# It is a declaration, so it is read from the instance root (D6.2): AGENTIAC_ROOT, which the unit sets, or the
# checkout this file is in. This process cannot import agentiac.paths (its PYTHONPATH is gateway/).
_INSTANCE = os.environ.get('AGENTIAC_ROOT') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(_INSTANCE, 'gateway', 'local-coder.json')) as _declared:
    RETIRED_TRANSPORTS = json.load(_declared).get('legacy_sources', {})
ROUTES.update({old: ROUTES[new] for old, new in RETIRED_TRANSPORTS.items() if new in ROUTES})
# Rendered by scripts/route.py together with the deployed config: the current
# local-coder target is verified exactly like the fixed aliases. The same file's
# keys are the aliases the task key may call (IP-01, T4.3): the rendered
# local-coder set, which is exactly what the suite and the smoke address.
TASK_ALIASES = set()
if os.environ.get('AGENTIAC_ROUTES') and os.path.exists(os.environ['AGENTIAC_ROUTES']):
    with open(os.environ['AGENTIAC_ROUTES']) as _routes:
        _rendered = json.load(_routes)
    # each entry is [base, served id] plus, for a manifested backend, the expected identity (IP-02, T4.1)
    ROUTES.update({alias: tuple(route) for alias, route in _rendered.items()})
    TASK_ALIASES = set(_rendered)

# ChatGPT-subscription aliases (ADR 0005) have no local worker to verify; they
# need the sign-in made by `agentiac chatgpt login` instead. They are the aliases gateway/config.yaml forwards to
# the subscription gateway (litellm_proxy/), listed in the same generated file. This module also runs in
# the separate agentiac-chatgpt process (AGENTIAC_CHATGPT=1); only that one
# refreshes tokens, so two processes never rotate the same refresh token.
SUBSCRIPTION = set(_GENERATED.get('subscription', ()))
REFRESH = os.environ.get('AGENTIAC_CHATGPT') == '1'

# The API key of a worker, by the control-side port its tunnel publishes it on. A port missing here is an
# error rather than a default: a default once sent one host's key to any unknown port, so the first new host
# would have been authenticated with the wrong credential (IP-03). Generated with ROUTES (nodes.json).
NODE_KEY = dict(_GENERATED.get('node_key', {}))
# the inventory name of each worker, so the log names the host and not its key
HOST_BY_KEY = dict(_GENERATED.get('host_by_key', {}))
NODE_BY_PORT = {port: HOST_BY_KEY[node] for port, node in NODE_KEY.items()}  # ':18115' -> '<inventory host name>'


def _node_of(base):
    """The host a tunnel publishes this api_base on, or None when it is not one of ours."""
    if not isinstance(base, str):
        return None
    for port, name in NODE_BY_PORT.items():
        if port in base:
            return name
    return None


def _served_by(kwargs):
    """Who actually ran the request (IP-06): the api_base litellm used, not what was asked for.

    Two Strata hosts can serve the same model name, so `model` alone cannot tell them apart; the
    tunnel port can. None for an alias with no local worker (the ChatGPT subscription).
    """
    params = kwargs.get('litellm_params') or {}
    base = params.get('api_base') or (kwargs.get('standard_logging_object') or {}).get('api_base')
    return _node_of(base)


def _requested_alias(kwargs):
    """The alias the client named, when litellm passes it through; else the backend model name.

    Measured against a real proxy (not assumed): `standard_logging_object.model_group` carries
    the requested alias while `model` carries the served name, which two hosts can share.
    The last source is what the log used before T4.2, so a line is never lost over a missing
    alias, and stamping the alias in the pre-call hook turned out to be unnecessary.
    """
    group = (kwargs.get('standard_logging_object') or {}).get('model_group')
    for value in (group, kwargs.get('model')):
        if isinstance(value, str) and value:
            return value
    return None


def _time_to_first_token(kwargs, start_time):
    """Seconds until the first streamed token, or None.

    Only a stream has a first token: litellm sets completion_start_time to end_time for a
    non-streaming call, which would report the whole call as the time to first token.
    """
    if not kwargs.get('stream'):
        return None
    first, began = kwargs.get('completion_start_time'), start_time
    if not isinstance(first, datetime.datetime) or not isinstance(began, datetime.datetime):
        return None
    return (first - began).total_seconds()


# IP-01 (T4.3), wired as general_settings.custom_auth: the task key a suite or smoke
# container carries is authenticated here and nowhere else. Returning None hands the
# request to litellm's own auth, so the master key, public routes and unknown keys
# behave exactly as before; only an exact match of LITELLM_TASK_KEY is intercepted.
# custom_auth_run_common_checks then applies litellm's standard key-model check on
# top of the pinned list, and the pre-call hook above enforces it as well.
TASK_KEY = os.environ.get('LITELLM_TASK_KEY')
CODE_KEYS = ((task_policy.CODE_USER, os.environ.get('LITELLM_CODE_KEY')),
             (task_policy.CODE_CLOUD_USER, os.environ.get('LITELLM_CODE_CLOUD_KEY')))


def _code_aliases(user):
    return task_policy.code_aliases(user, ROUTES, SUBSCRIPTION)


async def custom_auth(request, api_key):
    """UserAPIKeyAuth for the task key, None for every other credential.

    The two code keys of the daily agent (F01) are authenticated the same way, each with its own alias list.
    """
    if task_policy.is_task_key(api_key, TASK_KEY):
        user, models = task_policy.TASK_USER, TASK_ALIASES
    else:
        user = task_policy.key_user(api_key, CODE_KEYS)
        if user is None:
            return None
        models = _code_aliases(user)
    from litellm.proxy._types import LitellmUserRoles, ProxyException, UserAPIKeyAuth
    if not task_policy.route_allowed(request.url.path):
        raise ProxyException(message=f'This key may only call inference routes, not {request.url.path}',
                             type='auth_error', param='route', code=403)
    return UserAPIKeyAuth(user_id=user, user_role=LitellmUserRoles.INTERNAL_USER, api_key=user, models=sorted(models))


def _require_chatgpt_signin():
    from fastapi import HTTPException
    from litellm.llms.chatgpt.authenticator import Authenticator
    problem = chatgpt_auth.signin_problem(Authenticator(), refresh=REFRESH)
    if problem:
        raise HTTPException(status_code=503, detail=problem)

async def _verify_loaded_model(alias):
    import httpx
    from fastapi import HTTPException
    if alias not in ROUTES:
        raise HTTPException(status_code=404, detail='Unknown local model alias')
    route = ROUTES[alias]
    base, expected = route[0], route[1]
    declared_identity = route[2] if len(route) > 2 else None
    node = NODE_KEY.get(base[-6:])
    if node is None:  # a port we have no key for is a configuration error, not a worker outage
        raise HTTPException(status_code=503, detail=f'No API key configured for {base}')
    problem = await _identity_problem(base, node, declared_identity)
    if problem:
        raise HTTPException(status_code=503, detail=f'Alias {alias!r} is not served by its declared backend: {problem}')
    try:
        # An on-demand worker loads its model on this first request (about 1 min; Strata
        # up to 20); without the header it treats the GET as a passive poll. A missing
        # tunnel or backend still fails at connect.
        async with httpx.AsyncClient(timeout=httpx.Timeout(1300, connect=10), trust_env=False) as client:
            response = await client.get(base+'/v1/models',headers={'Authorization':'Bearer '+os.environ[node+'_API_KEY'],
                                                                       'X-Ondemand-Load':'1'})
            refused = _ondemand_refusal(response)
            if not refused:
                response.raise_for_status()
                actual = {m['id'] for m in response.json()['data']}
    except Exception:
        raise HTTPException(status_code=503, detail='Required inference worker unavailable') from None
    if refused:  # a refused on-demand load (owner's GPU work, host memory) keeps its reason
        raise HTTPException(status_code=503, detail='Required inference worker unavailable: ' + refused)
    if expected not in actual:
        raise HTTPException(status_code=503, detail='Required model profile is not loaded')

async def _identity_problem(base, node, declared_identity):
    """Ask the front what it serves and compare (IP-02, T4.1). Silent when nothing is declared or reported."""
    import httpx
    if declared_identity is None:
        return None
    try:
        # a passive status read: it never loads a model and never counts as use
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), trust_env=False) as client:
            response = await client.get(base + '/ondemand/status',
                                        headers={'Authorization': 'Bearer ' + os.environ[node + '_API_KEY']})
    except Exception:
        return None  # unreachable front: the served-id request below reports the worker outage, not this
    if response.status_code == 404:
        return None  # no front in front of this backend (an always-on start), or a front older than T4.1
    if response.status_code != 200:
        return f'status {response.status_code}'  # a front that answered and would not say is not a pass
    try:
        body = response.json()
        reported = body['identity'] if isinstance(body, dict) and 'identity' in body else None
    except ValueError:
        return 'status body'
    return identity_policy.identity_problem(declared_identity, reported)


def _ondemand_refusal(response):
    if response.status_code != 503:
        return None
    try:
        error = response.json().get('error') or {}
    except ValueError:
        return None
    return error.get('message', '')[:300] if error.get('type') == 'ondemand_unavailable' else None
