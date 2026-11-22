"""Status service: the same one-line worker state and gateway health the CLI prints.

The read layer takes its transport as an argument so the API can use the same endpoint and
on-demand calls without inventing a second network path. A missing key is a declaration
gap, never a fabricated state.
"""
import json
import urllib.error
import urllib.request
from pathlib import Path

from agentiac import backend_names, declared, paths
from agentiac.secrets import read_key

ROOT = paths.root()


def backend_name(backend_id):
    """The backend file a front's reported manifest id belongs to (a manifest keeps its id when its file is renamed
    under the naming rule), or the id itself when no declared manifest carries it."""
    try:
        from agentiac import backend_names
        return backend_names.resolve_in(ROOT, backend_id)
    except (OSError, ValueError):
        return backend_id


def _tensorfold_backend(name):
    """The TensorFold manifest a `spark_tensorfold` host serves (backend_names), or None when it cannot be told."""
    try:
        return backend_names.tensorfold_backend(backend_names.manifests(ROOT), name)
    except ValueError:
        return None


def host_state(name, host, transport, timeout=10):
    try:
        port, key = transport.endpoint(name, host)
    except OSError:
        return 'no key in secrets/nodes (never claimed)'
    code_, status = transport.ondemand_call(name, host, 'GET', 'status', timeout=timeout)
    if code_ == 200:
        observed = status.get('identity') or {}
        identity_text = (f"; front backend {backend_name(observed['backend_id'])}, "
                         f"context {observed.get('context', 'unknown')}" if observed.get('backend_id') else '')
        if status['state'] != 'ready':  # ondemand-proxy.py: unloaded, loading, ready, unloading
            text = status['state'] + (' (loads on the next request)' if status['state'] == 'unloaded' else '')
            return text + (f"; last load failed: {status['last_error']}" if status.get('last_error') else '') + identity_text
        left = status.get('unload_in_s')
        return 'loaded' + (f', unloads in {round(left / 60)} min' if left is not None else ', kept loaded') + identity_text
    try:  # not behind a front: always-on serving (ops start) or nothing of ours
        request = urllib.request.Request(f'http://127.0.0.1:{port}/health',
                                     headers={'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return f'always on, health {response.status}'
    except urllib.error.HTTPError as error:
        return f'health {error.code}'
    except OSError:
        if host.get('borrowed'):
            default = (_tensorfold_backend(name) if host.get('spark_tensorfold') else
                       host.get('strata_default_backend') or host.get('default_profile'))
            return 'not claimed' + (f'; default backend {default}' if default else '')
        return 'not reachable (tunnel or worker down)'


def gateway_state(root=ROOT, gateway=None, timeout=10):
    gateway = gateway or declared.gateway_url(root, '/v1')
    request = urllib.request.Request(gateway + '/models',
                                 headers={'Authorization': 'Bearer ' + read_key(Path(root) / 'secrets/gateway.env')})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, f"ok, {len(json.load(response)['data'])} aliases"
    except (OSError, ValueError) as error:
        return False, f'down ({type(error).__name__}); systemctl status {declared.resource("gateway", root)}'
