#!/usr/bin/env python3
"""Show, render or switch the explicit local-coder gateway target.

The tracked gateway/config.yaml stays the base. The deployed configuration is
rendered into artifacts/gateway/ with one extra alias copied from the selected
target, so a switch is an explicit, logged operator action, never a fallback.

The selected target is runtime state, not configuration (ADR 0007). What a running
gateway loaded is recorded as a signature, so agentiac ops deploy restarts it only
for a real change: both deploy and a switch record it, and only after a good restart.
"""
import argparse
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.parse
import urllib.request

import yaml

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'gateway'))  # identity_policy lives with the gateway that enforces it
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
import identity_policy  # noqa: E402
from agentiac import declared, model_names, mutation, retire, route_aliases  # noqa: E402
ROUTES = ROOT / 'gateway/local-coder.json'
OUT = ROOT / 'artifacts/gateway'
# Besides the rendered configuration, what each gateway process reads at start. The lock stands for the code in
# .venv-gateway: without it a new LiteLLM would be installed and never loaded.
_CALLBACKS = ('gateway/gateway_callbacks.py', 'gateway/task_policy.py', 'gateway/identity_policy.py',
              'gateway/chatgpt_auth.py', 'secrets/gateway.env', 'requirements-gateway.lock')
SOURCES = {'gateway': _CALLBACKS, 'chatgpt': ('gateway/chatgpt.yaml', *_CALLBACKS)}


def target_params(base, routes, target):
    """litellm_params of TARGET's source alias plus the target's extra_body."""
    target = route_aliases.resolve_target(routes, target)
    spec = routes['targets'][target]
    sources = [m for m in base['model_list'] if m['model_name'] == spec['source_alias']]
    if len(sources) != 1:
        raise ValueError(f"source alias {spec['source_alias']!r} must exist exactly once")
    params = copy.deepcopy(sources[0]['litellm_params'])
    if 'extra_body' in spec:
        params['extra_body'] = spec['extra_body']
    return params


def render(base, routes, target):
    """Return (config, callback routes) for the base config plus local-coder -> TARGET.

    Every target is also published under its own API name with the same settings
    (version 1: local-coder-<target>; version 2: the node-first target key, plus
    local-coder-<retired key> for each retired key), so evaluations can address
    several targets at once without switching the route. A manifested target also carries its expected identity
    (backend id, manifest hash, context) so the pre-call can refuse an alias
    that is served by anything else (IP-02, T4.1): the front reports what it
    actually serves and gateway/identity_policy.py compares the two.
    """
    base_names = [model['model_name'] for model in base['model_list']]
    route_aliases.require_valid(routes, base_names)
    if routes.get('short_aliases') or route_aliases.version(routes) >= 2:
        manifests = {}
        for spec in routes['targets'].values():
            backend = spec.get('backend')
            if backend and backend not in manifests:
                path = ROOT / 'backends' / f'{backend}.json'
                manifests[backend] = json.loads(path.read_text()) if path.exists() else {}
        graph = SimpleNamespace(route=routes, backends=manifests, nodes=inventory_hosts(),
                                gateway_config=base, gateway_models=base_names)
        problems = model_names.alias_problems(graph)
        if problems:
            raise ValueError('; '.join(problems))
    names = list(route_aliases.api_targets(routes, target).items())
    if any(m['model_name'] == name for m in base['model_list'] for name, _ in names):
        raise ValueError('the base config must not define a rendered alias')
    config, callback = copy.deepcopy(base), {}
    for name, selected in names:
        params = target_params(base, routes, selected)
        config['model_list'].append({'model_name': name, 'litellm_params': params})
        entry = [params['api_base'].removesuffix('/v1'), params['model'].removeprefix('openai/')]
        spec = routes['targets'][selected]
        if 'backend' in spec:  # a manifest declares what this alias must be served by
            entry.append(identity_policy.expected_identity(ROOT, spec['backend'], spec.get('context_window')))
        callback[name] = entry
    # Retired transport names (naming stage 3) answer as copies of their current transport until stage 4. They are
    # base aliases, not route names: no identity entry, and the task key cannot call them.
    for old, new in route_aliases.legacy_sources(routes).items():
        source = next(m for m in base['model_list'] if m['model_name'] == new)
        config['model_list'].append({'model_name': old, 'litellm_params': copy.deepcopy(source['litellm_params'])})
    return config, callback


def node_tables(base, inventory_hosts):
    """The callbacks' tables (ROUTES, NODE_KEY, HOST_BY_KEY, SUBSCRIPTION) derived from the declarations (D3.1).

    gateway_callbacks.py used to hold these as hand-written literals beside gateway/config.yaml and
    infra/inventory.yml, which already say the same things; since M8 (T8.6) this file is their only source. Only
    openai/ entries have a worker to verify; a subscription alias (litellm_proxy/, forwarded to the subscription
    gateway) has none and needs the sign-in instead. Retired transports are added by the callbacks, as before.
    """
    routes, node_key, subscription = {}, {}, []
    for entry in base['model_list']:
        alias, params = entry['model_name'], entry['litellm_params']
        if params.get('model', '').startswith('litellm_proxy/'):
            subscription.append(alias)
        if not params.get('model', '').startswith('openai/'):
            continue
        routes[alias] = [params['api_base'].removesuffix('/v1'), params['model'].removeprefix('openai/')]
        key = params.get('api_key', '')
        if not (key.startswith('os.environ/') and key.endswith('_API_KEY')):
            raise ValueError(f'{alias}: api_key must be os.environ/<NAME>_API_KEY')
        name = key.removeprefix('os.environ/').removesuffix('_API_KEY')
        port = ':' + str(urllib.parse.urlsplit(params['api_base']).port)
        if node_key.setdefault(port, name) != name:
            raise ValueError(f'{alias}: port {port} already uses {node_key[port]}_API_KEY, not {name}_API_KEY')
    host_by_key = {}
    for host, facts in inventory_hosts.items():
        if facts.get('backend') == 'strata':
            published = facts.get('strata_tunnel_port')
        else:
            published = facts.get('tunnel_port') or facts.get('backend_port')
        key = node_key.get(f':{published}')
        if key:
            host_by_key[key] = host
    return {'routes': routes, 'node_key': node_key, 'host_by_key': host_by_key, 'subscription': sorted(subscription)}


def inventory_hosts():
    """The worker hosts of infra/inventory.yml with the group vars merged in, as borrow.hosts() reads them."""
    inventory = yaml.safe_load((ROOT / 'infra/inventory.yml').read_text())['all']
    return {name: {**inventory['vars'], **host} for name, host in inventory['children']['workers']['hosts'].items()}


def current():
    """The current target key. A route saved under a retired key resolves to its new key (naming stage 2)."""
    routes = json.loads(ROUTES.read_text())
    try:
        saved = json.loads((OUT / 'route.json').read_text())['target']
    except FileNotFoundError:
        return routes['default']
    return route_aliases.resolve_target(routes, saved)


def rendered(target):
    """The files of artifacts/gateway/ for TARGET, in memory."""
    base = yaml.safe_load((ROOT / 'gateway/config.yaml').read_text())
    routes = json.loads(ROUTES.read_text())
    target = route_aliases.resolve_target(routes, target)
    config, callback = render(base, routes, target)
    return {'config.yaml': yaml.safe_dump(config, sort_keys=False),
            'routes.json': json.dumps(callback, indent=2) + '\n',
            'route.json': json.dumps({'target': target}) + '\n',
            'nodes.json': json.dumps(node_tables(base, inventory_hosts()), indent=2, sort_keys=True) + '\n'}


def signature(kind):
    """Identity of what KIND (gateway or chatgpt) would load now; holds no secret."""
    digest = hashlib.sha256()
    if kind == 'gateway':
        files = rendered(current())
        digest.update((files['config.yaml'] + files['routes.json'] + files['nodes.json']).encode())
    for path in SOURCES[kind]:
        digest.update(paths.locate(path, ROOT).read_bytes())
    return digest.hexdigest()


def deployed_path(kind):
    return ROOT / f'artifacts/{kind}-deployed.sha256'


def pending(kind):
    """True when the running process has not loaded the current configuration."""
    path = deployed_path(kind)
    return not path.exists() or path.read_text().strip() != signature(kind)


def record(kind):
    deployed_path(kind).write_text(signature(kind) + '\n')


def record_deploy():
    """Which revision of this checkout (and its pinned core/) the last agentiac ops deploy installed."""
    from agentiac import command
    command.record_deploy(ROOT)


def write(target):
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in rendered(target).items():
        tmp = OUT / (name + '.tmp')
        tmp.write_text(text)
        os.replace(tmp, OUT / name)


def gateway_models():
    key = next(line.split('=', 1)[1].strip() for line in (ROOT / 'secrets/gateway.env').read_text().splitlines()
               if line.startswith('LITELLM_MASTER_KEY='))
    request = urllib.request.Request(declared.gateway_url(ROOT, '/v1/models'), headers={'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(request, timeout=5) as response:
        return [m['id'] for m in json.load(response)['data']]


def restart():
    subprocess.run(['sudo', '-n', 'systemctl', 'restart', declared.resource('gateway', ROOT)], check=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if json.loads(ROUTES.read_text())['alias'] in gateway_models():
                return
        except OSError:
            pass
        time.sleep(2)
    raise RuntimeError('gateway did not come back with local-coder within 120 s')


def thin_target(target):
    """F6 (T5.3): the refusal, if any, for pointing local-coder at TARGET.

    A route switch is the decision that makes a root load-bearing, so it is read from the ledger before anything is
    written: a target naming a `form: thin` line would send every `local-coder` request to a root that does not hold
    its weights and cannot start. The refusal names the way out, and it stops before `write`, so the route table and
    the gateway are untouched.
    """
    spec = json.loads(ROUTES.read_text())['targets'][target]
    return retire.thin_ledger(declared.load(ROOT).retention, spec['node'], spec.get('backend'))


def switch_to(target):
    """Point local-coder at TARGET; a gateway that does not come back gets the previous route again."""
    target = route_aliases.resolve_target(json.loads(ROUTES.read_text()), target)
    previous = current()
    write(target)
    print(f'local-coder: {previous} -> {target}; restarting gateway (in-flight requests are cut)', file=sys.stderr)
    try:
        restart()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        write(previous)
        print(f'gateway did not come back on {target} ({error}); restoring {previous}', file=sys.stderr)
        try:
            restart()
        except (OSError, RuntimeError, subprocess.CalledProcessError) as second:
            raise SystemExit(f'gateway is down: route restored to {previous} but it did not restart ({second})')
        raise SystemExit(f'route unchanged: still {previous}')
    record('gateway')
    with (OUT / 'route-log.jsonl').open('a') as log:
        log.write(json.dumps({'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                              'from': previous, 'to': target}) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for name, text in (('pending', 'print pending or current: has the running process loaded this configuration?'),
                       ('record', 'record the configuration as loaded (after a good restart)')):
        sub.add_parser(name, help=text).add_argument('kind', choices=SOURCES)
    sub.add_parser('record-deploy', help='record the checkout revision of a finished deploy')
    sub.add_parser('show')
    sub.add_parser('render', help='write artifacts/gateway/ for the current target without restarting')
    switch = sub.add_parser('set', help='render for TARGET and restart the gateway')
    switch.add_argument('target')
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'show':
        spec = json.loads(ROUTES.read_text())['targets'][current()]
        name = model_names.display_names(declared.load(ROOT))[current()]
        print(json.dumps({'local-coder': current(), 'name': name, **spec}))
    elif args.command == 'render':
        write(current())
    elif args.command == 'pending':
        print('pending' if pending(args.kind) else 'current')
    elif args.command == 'record':
        record(args.kind)
    elif args.command == 'record-deploy':
        record_deploy()
    else:
        args.target = route_aliases.resolve_target(json.loads(ROUTES.read_text()), args.target)
        with mutation.hold_or_exit(ROOT, f'route {args.target}'):
            block = thin_target(args.target)
            if block:
                raise SystemExit(f'route unchanged: {block}')
            switch_to(args.target)
        print(json.dumps({'local-coder': args.target}))


if __name__ == '__main__':
    main()
