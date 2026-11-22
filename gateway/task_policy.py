"""IP-01 (T4.3): the policy behind the low-privilege task key.

A suite or smoke container runs code the model wrote, with host networking, so the
credential it carries must open as little as possible (docs/improvement-proposals.md
IP-01). This module is that policy, kept free of litellm so `make check` can test it
from the control venv; gateway_callbacks.py wraps it in the litellm types the proxy
expects and gateway/config.yaml wires that in as `general_settings.custom_auth`.

The task key may only:
  * call the inference routes below — no /v1/models, no admin route, nothing else; and
  * only for the rendered local-coder aliases, i.e. the keys of the routes.json that
    scripts/route.py renders — exactly what the suite and the smoke address.

Everything else — the master key, public routes, unknown keys — is left to litellm's
own auth: custom_auth returns None for it and the request is authenticated exactly as
before this file existed. The cloud alias is not a rendered local-coder alias, so the
task key cannot reach it: that was the ADR 0005 `cloud_repos` bypass IP-01 measured.

The daily interactive agent carries one of two code keys instead of the master key (F01): both are limited to
the same inference routes, the code key to every local alias and the code cloud key to those plus the
subscription alias, which leaves the host and spends the owner's subscription.
"""
import secrets

TASK_USER = 'agent-task'  # the user_id custom_auth stamps on the task key
CODE_USER = 'agent-code'              # the daily agent's key: every local alias
CODE_CLOUD_USER = 'agent-code-cloud'  # the same plus the subscription alias
CODE_USERS = frozenset({CODE_USER, CODE_CLOUD_USER})
INFERENCE_ROUTES = frozenset({'/v1/chat/completions', '/v1/responses', '/v1/messages', '/v1/completions'})


def is_task_key(api_key, task_key):
    """True only for an exact match of the configured task key; an unset task key authenticates nothing."""
    if not task_key or not api_key:
        return False
    # bytes: compare_digest raises on a non-ASCII str, and every presented credential comes through here
    return secrets.compare_digest(str(api_key).encode(), str(task_key).encode())


def route_allowed(path):
    """The task key is an inference credential: only the LLM routes, chosen before the body is read."""
    return path in INFERENCE_ROUTES


def model_allowed(model, allowed):
    """The pinned alias list; checked in the pre-call hook, where the requested model is known."""
    return model in allowed


def key_user(api_key, keys):
    """The user of the first (user, configured key) pair that API_KEY matches exactly, or None."""
    for user, configured in keys:
        if is_task_key(api_key, configured):
            return user
    return None


def code_aliases(user, local, subscription):
    """What a code key may call: every local alias, and the subscription aliases for the cloud key only."""
    return set(local) | (set(subscription) if user == CODE_CLOUD_USER else set())
