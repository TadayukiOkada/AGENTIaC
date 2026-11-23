#!/usr/bin/env python3
"""Claim, warm, unload or release a borrowed host (inventory `borrowed: true`).

Dedicated workers with `ondemand_persistent: true` (the Orins) use claim, warm,
unload and status too: claim installs the front as an enabled unit that survives
a reboot. They are never released; `agentiac ops start` returns them to always-on serving.

claim   installs the host's tested default profile (or its prepared Strata
        backend, ADR 0003) behind the on-demand front (ADR 0004), starts the
        tunnel and, unless --no-warm, loads the model now. The model is loaded
        on the first request and unloaded after --idle minutes without traffic
        (inventory idle_unload_minutes; 0 keeps it loaded). With --route,
        points local-coder at the host. A Strata host serves its legacy Q2_0
        root, or with --backend <id> [--context N] a manifest prepared in the root
        its engine version has (inventory strata_trial_roots; strata-128k-plan.md).
warm    loads the model now and waits until it answers (default host: the
        current local-coder target); the idle period starts again.
unload  unloads the model now; the next request loads it again.
status  on-demand state of the claimed hosts.
reclaim claims the host of the current local-coder target again with that target's backend
        and context, after a reboot or a release of the claim. It never switches the route,
        so the gateway keeps running; a claim that still stands is left as it is.
release moves local-coder away from the host first, then stops the on-demand
        front, every agentiac unit and project-labelled container there and
        the control tunnel, and reports anything left. Other users' workloads
        are never touched.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import yaml

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
sys.path.insert(0, str(CODE / 'scripts'))
from agentiac.errors import explain  # noqa: E402
from agentiac import backend_names, claims, declared, identities, mutation, retire, upgrade  # noqa: E402
import route  # noqa: E402
from agentiac.errors import command as cli_name  # noqa: E402

PREFIX = declared.setting('resource_prefix', ROOT)   # every deployed name is <prefix>-<role> (D6.8)

# On a borrowed worker everything named <prefix>* is ours. On the control host
# the gateway, tunnels and evaluations share that prefix, so only inference stops.
RELEASE = r'''
sudo -n systemctl stop %(prefix)s-ondemand 2>/dev/null || true  # first: it would load the model again
units() { systemctl $1 list-units --plain --no-legend --state=active,activating,reloading '%(units)s' | awk '$1 !~ /^%(prefix)s-spark-(archive|cleanup)-/ {print $1}' | xargs; }
sudo -n systemctl stop %(prefix)s-inference 2>/dev/null || true
for u in $(units --system); do sudo -n systemctl stop "$u"; done
for u in $(units --user); do systemctl --user stop "$u"; done
for c in $(docker ps -q --filter %(containers)s); do docker stop -t 60 "$c" >/dev/null; done
echo "units=$(units --system) $(systemctl is-active --quiet %(prefix)s-ondemand && echo %(prefix)s-ondemand)"
echo "user_units=$(units --user)"
echo "containers=$(docker ps -q --filter %(containers)s | xargs)"
echo "gpu_apps=$(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader 2>/dev/null | xargs)"
'''
WORKER_SCOPE = {'units': f'{PREFIX}*', 'containers': 'label=io.agent-infra.owner=agent-infra', 'prefix': PREFIX}
CONTROL_SCOPE = {'units': f'{PREFIX}-inference*', 'containers': f'name=^{PREFIX}-inference$', 'prefix': PREFIX}
PROXY = f'{PREFIX}-ondemand'
PROXY_DIRECTORY = 'ondemand'  # the directory a root's front lives in, and what `running_front_directory` reads back
LOAD_FAILURES = (('admission refused', 'the host refused the start: low memory, a busy host or GPU, or a build or '
                                       'profile that does not match'),
                 ('start command exited', 'the start command failed on the host'),
                 ('stopped while loading', 'the backend unit stopped while loading'),
                 ('backend not ready', 'the backend did not answer before the start timeout'))
WATCH = f'{PREFIX}-strata-watch'  # trial backends only; stopped by release like every unit of ours


def on_demand(host):
    return host.get('borrowed') or host.get('ondemand_persistent')


def serves_strata(host):
    """A Strata host (`backend: strata`), or a dedicated worker whose front serves a Strata manifest instead of its llama.cpp
    default_profile (inventory strata_backend: an Orin). The second keeps its own port, tunnel and key.

    A third form: a host that declares versioned Strata trial roots and makes Strata nothing by default.
    `strata_trial_roots` is the weaker fact - it names the directory a `--backend` claim of that engine
    version prepares into - while `strata_backend` and `strata_default_backend` are what a host serves when
    nothing is asked for, so a host declaring neither keeps its pinned TensorFold service as the default claim.
    """
    return host.get('backend') == 'strata' or 'strata_backend' in host or bool(host.get('strata_trial_roots'))


def other_serving_units(host, unit, name=None):
    """The backend units of this host that the front being installed does NOT start.

    A host has one API port, so whichever backend it is not serving must not keep holding that port or the memory behind it
    (docs/operations.md). The Orins switch between their llama.cpp default (`agentiac-inference`) and a Strata manifest
    (`agentiac-strata-inference`); the Strata hosts have only the Strata one, named by `inference_unit`; a
    `spark_tensorfold` host switches between its pinned TensorFold service - which IS its `inference_unit` -
    and a root of its `strata_trial_roots`. With NAME given, a manifest that declares how it is served adds its own
    unit to the set, because the point of this list is "every unit that could be holding the port", not "the two names
    this file knows".
    """
    units = {host.get('inference_unit'), f'{PREFIX}-strata-inference'}
    if host.get('default_profile'):
        units.add(f'{PREFIX}-inference')
    if name:
        units.update(declared_units(name))
    return sorted(other for other in units if other and other != unit)


def service_spec(manifest, name):
    """How MANIFEST says its engine is served on host NAME, or None when it says nothing.

    A Strata manifest is served by the rules in this file (a trial root, `<prefix>-strata-inference`, the inventory's
    on-demand backend port). An engine that serves from an image or a container has no such rules, so its manifest
    names the service instead: `workers.<host>.service` with the `unit` the project starts, the internal `port` the
    front reaches, the `root` the launch files live in, and - for a container - the `container` name whose PIDs and
    cgroup belong to it. Declaring it in the manifest is what keeps `claim`, `ops prepare`, `ops drift` and the trial
    watch agreeing about one engine, instead of each carrying its own engine name. `status_path` and `start_timeout_s`
    are only for a service that differs from the defaults the Strata trials use: the watch answers its own status
    route and the front its own readiness wait when they are not declared.

    Only what the serving path needs is read here: the image, the artifact and the engine flags stay in the manifest
    for the host-side worker and the identity checks to read.
    """
    worker = (manifest.get('workers') or {}).get(name) or {}
    if 'service' not in worker or worker['service'] is None:
        return None
    declared_service = worker['service']
    if not isinstance(declared_service, dict):
        raise SystemExit(explain(f'the service this manifest declares for {name} is not usable',
                                 ['`service` must be a mapping with `unit`, `port` and `root`'],
                                 'grep -n -A 12 "workers" backends/<manifest>.json',
                                 'fix the `service:` block, or drop it and let the Strata rules serve this manifest'))
    unit, port, root = declared_service.get('unit'), declared_service.get('port'), declared_service.get('root')
    container, status_path = declared_service.get('container'), declared_service.get('status_path')
    start_timeout_s = declared_service.get('start_timeout_s')
    problems = []
    if (not isinstance(unit, str) or not unit.startswith(f'{PREFIX}-')
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', unit.removeprefix(f'{PREFIX}-'))):
        problems.append(('unit', f'must be a name this site deploys: <resource_prefix>-<role>, so `{PREFIX}-...`'))
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        problems.append(('port', 'must be the internal port the front reaches, an int in 1024..65535'))
    if (not isinstance(root, str) or not root.startswith('/') or not root.rstrip('/')
            or '..' in Path(root).parts or not re.fullmatch(r'/[A-Za-z0-9._/-]+', root)):
        problems.append(('root', 'must be an absolute worker directory'))
    if container is not None and (not isinstance(container, str) or not container or any(c.isspace() for c in container)):
        problems.append(('container', 'when declared, must be the container name'))
    if status_path is not None and (not isinstance(status_path, str) or not status_path.startswith('/')):
        problems.append(('status_path', 'when declared, must be a route like /health'))
    if start_timeout_s is not None and (not isinstance(start_timeout_s, int) or isinstance(start_timeout_s, bool)
                                        or start_timeout_s < 1):
        problems.append(('start_timeout_s', 'when declared, is how long the front waits for this engine to be ready'))
    if problems:
        raise SystemExit(explain(f'the service this manifest declares for {name} is not usable',
                                 [f'`{key}` {problem}' for key, problem in problems],
                                 f'grep -n -A 12 "workers" backends/<manifest>.json',
                                 'fix the `service:` block, or drop it and let the Strata rules serve this manifest'))
    return {'unit': unit, 'port': port, 'root': root.rstrip('/'), 'start_timeout_s': start_timeout_s,
            'container': container, 'status_path': status_path}


def declared_units(name):
    """Every unit that any manifest declares for host NAME: the units a claim of another engine could leave running."""
    units = set()
    for path in (ROOT / 'backends').glob('*.json'):
        try:
            manifest = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):  # a file that is not a manifest declares no service (backends/ holds both)
            continue
        try:
            spec = service_spec(manifest, name)
        except SystemExit:
            continue  # a malformed declaration is reported by the command that reads it, not from a unit list
        if spec:
            units.add(spec['unit'])
    return units


def undeclared_backend(name, host, backend):
    """The refusal for a `--backend` this host has no way to serve, naming the two ways a manifest is served."""
    return explain(f'{name} cannot serve backends/{backend}.json: this host declares neither way of serving it',
                   ['the Strata way: `backend: strata`, or a `strata_backend`/`strata_default_backend` to serve by '
                    'default, or `strata_trial_roots` naming the root that manifest prepares into',
                   'the declared way: `workers` names this host and its `service` block gives the unit, the internal '
                    'port, the root, and for a container the container name'],
                   f'grep -n -A 12 "{name}" infra/inventory.yml; grep -n -A 20 "workers" backends/{backend}.json',
                   f'declare the trial root (Strata) or the `service` block (an image or container engine), then '
                   f'{cli_name()} ops prepare {name} --backend {backend} --check')


def api_key(name, host):
    """The key the control host holds for this worker's API: a Strata host's own, else the worker's."""
    stem = f'strata-{name}' if host.get('backend') == 'strata' else name
    return (ROOT / f'secrets/nodes/{stem}.key').read_text().strip()


def run_playbook(playbook, name, *argv):
    subprocess.run([str(CODE / '.venv/bin/ansible-playbook'), '-i', 'infra/inventory.yml', str(CODE / f'infra/{playbook}.yml'),
                    '--limit', name, *argv],
                   cwd=ROOT, env={**os.environ, 'ANSIBLE_CONFIG': str(CODE / 'infra/ansible.cfg')}, check=True)


def hosts():
    inventory = yaml.safe_load((ROOT / 'infra/inventory.yml').read_text())['all']
    return {name: {**inventory['vars'], **host} for name, host in inventory['children']['workers']['hosts'].items()}


def login(host):
    """`account@address` for ssh and scp to HOST: the service account it declares (D8.2) at its address."""
    return f"{declared.account(host, ROOT)}@{host['ansible_host']}"


def on_host(host, argv, **kwargs):
    if host.get('ansible_connection') == 'local':
        argv = ['bash', '-c', ' '.join(argv)]  # same word splitting as the remote shell
    else:
        argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', login(host)] + argv
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def on_host_text(host, command):
    """The command as someone would type it to run it on HOST."""
    return command if host.get('ansible_connection') == 'local' else f"ssh {login(host)} {command}"


def put(host, source, dest):
    """Copy a file control -> HOST. The only direction `put` goes; `get` is the other one."""
    if host.get('ansible_connection') == 'local':
        shutil.copyfile(source, dest)
        return 0
    return subprocess.run(['scp', '-q', str(source), f"{login(host)}:{dest}"]).returncode


def get(host, source, dest):
    """Bring a file HOST -> control.

    `source` is a path on the host and `dest` a path here. Named apart from `put` because on a node with
    `ansible_connection: local` both directions are the same `shutil.copyfile`, so a test on such a node cannot tell
    a fetch from a push: what it cannot see is the `account@host:` prefix, and which side of `scp` it belongs to.
    """
    if host.get('ansible_connection') == 'local':
        shutil.copyfile(source, dest)
        return 0
    return subprocess.run(['scp', '-q', f"{login(host)}:{source}", str(dest)]).returncode


def parse_report(text):
    report = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
    return {key: report.get(key, '').split() if key != 'gpu_apps' else report.get(key, '')
            for key in ('units', 'user_units', 'containers', 'gpu_apps')}


def targets_on(node):
    routes = json.loads(route.ROUTES.read_text())
    return [name for name, spec in routes['targets'].items() if spec['node'] == node]


def claimed_target(node, backend=None, context=None):
    """The local-coder target that matches what claim serves: the manifest backend's target whose window
    equals the served context. A target with no `backend` cannot be matched, which is how a claim is refused
    before anything starts rather than served by whatever target happens to name the host."""
    routes = json.loads(route.ROUTES.read_text())['targets']
    if backend is not None:
        context = context or json.loads((ROOT / f'backends/{backend}.json').read_text())['context']
    matches = [name for name in targets_on(node) if routes[name].get('backend') == backend
               and (backend is None or routes[name]['context_window'] == context)]
    if len(matches) != 1:
        cause = (f'{len(matches)} targets match, so the claim is ambiguous' if matches
                 else 'no target in gateway/local-coder.json declares this node, backend and context')
        raise SystemExit(explain(f'no single local-coder target for {node} {backend or ""} {context or ""}'.rstrip()
                                 + f' in gateway/local-coder.json (found {matches})', [cause],
                                 f'{cli_name()} model list',
                                 'give --backend ID and --context N of a listed target, or declare the target in '
                                 'gateway/local-coder.json'))
    return matches[0]


def endpoint(name, host):
    """Control-side API port and key of a host."""
    if host.get('backend') == 'strata':
        return host['strata_tunnel_port'], api_key(name, host)
    return host.get('tunnel_port', host['backend_port']), api_key(name, host)


def strata_root(host, backend):
    """Worker root of a Strata backend: the trial root prepared for that manifest's engine version
    (strata_trial_roots). One root per engine version keeps a 0.2.8 trial from ever reading the root an
    older engine serves from.

    A `strata_trial_roots` key may instead name a manifest ID, which is how one engine version holds more
    than one weight set on one host: each set is prepared in its own root, and each manifest still resolves
    to exactly one of them (the RTX 3090 host's Unsloth Dynamic v3.1 and Gyro-M builds of the same 0.2.11 engine).
    A key that names a manifest wins over that manifest's version key, and there is no fallback: a manifest
    with neither is refused.

    There is no unversioned root fallback: `strata_root` is retired and a manifest with no declared root on
    the host is refused by name, not served from a guess.
    """
    if backend is None:
        raise SystemExit('a Strata claim needs a backend: name --backend ID, or give the host '
                         'strata_default_backend in infra/inventory.yml')
    manifest = json.loads((ROOT / f'backends/{backend}.json').read_text())
    if str(manifest.get('status', '')).startswith('retired'):
        # a manifest kept as the record of a retired root resolves to nothing, even where another manifest of
        # the same engine version has a root: the record must not become a way to start a root that does not
        # hold its weights
        raise SystemExit(f'{backend} is retired: {manifest["status"]}')
    version = manifest.get('source_version')
    roots = host.get('strata_trial_roots', {})
    if backend in roots:
        return roots[backend]
    if version in roots:
        return roots[version]
    raise SystemExit(f'this host has no root for {backend} engine {version}: '
                     f'add one to strata_trial_roots, or prepare it with {cli_name()} ops prepare')


def tensorfold_backend(name):
    """The TensorFold manifest a `spark_tensorfold` host serves as its own backend (backends/, D6.4: not a constant)."""
    try:
        found = backend_names.tensorfold_backend(backend_names.manifests(ROOT), name)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if found is None:
        raise SystemExit(explain(f'{name} declares spark_tensorfold but no TensorFold manifest names it',
                                 ['no backends/*.json with the TensorFold engine lists this host under workers'],
                                 f'grep -l \'"{name}"\' backends/*.json', 'add the host to its manifest\'s workers'))
    return found


def serves_tensorfold(name, host, backend):
    """Whether the claim is the host's own TensorFold service: the inventory flag, and no --backend naming another."""
    return bool(host.get('spark_tensorfold')) and backend in (None, tensorfold_backend(name))


def manifest_service(name, backend):
    """The service block `backends/<backend>.json` declares for host NAME, or None where it declares none.

    A file that is not there declares nothing: the command that needs the manifest itself reads it and reports its
    own absence (`strata_root`, the claim's `manifest_file`), so this answer only decides how to serve.
    """
    if backend is None:
        return None
    try:
        manifest = json.loads((ROOT / f'backends/{backend}.json').read_text())
    except (OSError, ValueError):
        return None
    return service_spec(manifest, name) if isinstance(manifest, dict) else None


def tensorfold_root(host):
    return host['project_root'] + '/evaluations/spark-flash-next/service-tensorfold'


def ondemand_config(name, host, idle_minutes, backend=None, context=None, engine_options=None):
    """Worker directory and on-demand front configuration for the host's backend."""
    if serves_tensorfold(name, host, backend):
        # An explicit Strata --backend takes the other branch: a `spark_tensorfold` host answers a Strata manifest
        # from the trial root its inventory names, on the same listen port, with the TensorFold unit stopped.
        root = tensorfold_root(host)
        unit = f'{PREFIX}-spark-tensorfold'
        return root, {'listen_port': host['backend_port'], 'backend_port': 18885,
                      'key_file': host['project_root'] + '/api.key', 'unit': unit,
                      'start': ['sudo', '-n', 'systemctl', 'start', unit],
                      'stop': ['sudo', '-n', 'systemctl', 'stop', unit],
                      'idle_s': int(idle_minutes * 60), 'start_timeout_s': 1300,
                      # the front reports what it serves from this manifest (IP-02, T4.1)
                      'manifest_file': f'{root}/backend-manifest.json'}
    internal = host['ondemand_backend_port']
    service = manifest_service(name, backend)
    if service:
        # An engine that serves from an image or a container has no Strata trial root and no `<prefix>-strata-inference`
        # unit, so its manifest says how it is served and this resolves the same three facts a claim needs: the root
        # the launch files live in, the unit that holds the internal port, and that port. The start and stop stay
        # "the unit the project owns", which is what releases the GPU. A manifest that declares a service is served
        # that way even on a host that also runs Strata trials: the declaration is the more specific statement.
        root, unit = service['root'], service['unit']
        listen = host['strata_api_port'] if host.get('backend') == 'strata' else host['backend_port']
        return root, {'listen_port': listen, 'backend_port': service['port'],
                      'key_file': f'{root}/api.key', 'unit': unit,
                      'container': service['container'], 'status_path': service['status_path'],
                      'start': ['sudo', '-n', 'systemctl', 'start', unit],
                      'stop': ['sudo', '-n', 'systemctl', 'stop', unit],
                      'idle_s': int(idle_minutes * 60),
                      'start_timeout_s': service['start_timeout_s'] or 1300,
                      # shown by the front's status, and what the gateway's identity comparison expects (IP-02, T4.1)
                      'backend': backend, 'context': context,
                      'manifest_file': f'{root}/backend-manifest.json'}
    if serves_strata(host):
        root, unit = strata_root(host, backend), f'{PREFIX}-strata-inference'
        start = [f'{root}/.venv/bin/python', f'{root}/start-worker.py', '--node', name, '--root', root,
                 '--attempt', 'claim', '--port', str(internal), '--unit', unit]
        if backend is not None:
            start += ['--backend', f'{root}/backends/{backend}.json'] + (['--context', str(context)] if context else [])
        for option in engine_options or []:
            # every on-demand load re-applies it: the front runs this command each time it loads, so an option named
            # at claim time is how the host was claimed, not a one-off warm
            start += ['--engine-option', option]
        # a dedicated worker keeps the API port its tunnel and gateway alias already use
        listen = host['strata_api_port'] if host.get('backend') == 'strata' else host['backend_port']
        key_file = f'{root}/api-key-claim.env'
    else:
        root, unit = host['project_root'], f'{PREFIX}-inference'
        start = ['sudo', '-n', 'systemctl', 'start', unit]
        listen, key_file = host['backend_port'], f'{root}/api.key'
    config = {'listen_port': listen, 'backend_port': internal, 'key_file': key_file, 'unit': unit,
              'start': start, 'stop': ['sudo', '-n', 'systemctl', 'stop', unit],
              'idle_s': int(idle_minutes * 60), 'start_timeout_s': 1300}
    if not serves_strata(host):  # the Strata start script runs its own admission
        config['check'] = ['/usr/bin/python3', f'{root}/scripts/serve-profile.py', '--check', root]
    elif backend is not None:
        # shown by the front's status, and what the gateway's identity comparison expects (IP-02, T4.1)
        config['backend'], config['context'] = backend, context
        config['manifest_file'] = f'{root}/backends/{backend}.json'
    return root, config


def install_front(name, host, directory):
    """Run the front: a transient unit on a borrowed host (gone after release or reboot),
    an enabled unit on a dedicated one."""
    on_host(host, ['sudo', '-n', 'systemctl', 'stop', PROXY])  # a loaded backend is adopted by the new front
    on_host(host, ['sudo', '-n', 'systemctl', 'reset-failed', PROXY])
    if host.get('ondemand_persistent'):
        run_playbook('unit', name, '-e', 'unit=ondemand', '-e', f'directory={directory}')
        return
    started = on_host(host, ['sudo', '-n', 'systemd-run', f'--unit={PROXY}', f'--uid={declared.account(host, ROOT)}', '--property=Restart=on-failure',
                             '--property=RestartSec=5', '/usr/bin/python3', f'{directory}/proxy.py', f'{directory}/config.json'])
    if started.returncode:
        # systemd-run returns before proxy.py binds its port, so a port conflict cannot end up here.
        raise SystemExit(explain(f'on-demand front failed to start on {name}: {started.stderr.strip()}',
                                 ['ssh to the host failed', 'sudo -n was refused on the host',
                                  f'a unit named {PROXY} is still loaded there'],
                                 on_host_text(host, f'systemctl status {PROXY} --no-pager'),
                                 f'{cli_name()} ops preflight {name}, fix what it reports, then repeat the claim'))


def installed_unit_roles():
    """The roles `infra/unit.yml` can install, read from its own `units:` map (that file is what makes a unit's text)."""
    play = yaml.safe_load((CODE / 'infra' / 'unit.yml').read_text())
    return set(((play[0].get('vars') or {}).get('units') or {}))


def missing_unit(role):
    """The refusal for a service whose unit this site has no text for yet, naming the two files that would give it one."""
    return explain(f'this site cannot install `{PREFIX}-{role}`: `infra/unit.yml` has no role `{role}`',
                   ['a claimed engine is served by a unit this project installs and stops, because stopping it is what '
                    'releases the GPU (docs/operations.md)',
                    'the role name is the unit name without the site\'s resource prefix'],
                   'grep -n -A 6 "units:" infra/unit.yml; ls infra/templates',
                   f'add `{role}: {{file: \'{{{{ resource_prefix }}}}-{role}.service\', run: true}}` to that map and write '
                   f'`infra/templates/{role}.service.j2` (the recipe that starts the engine), then repeat the claim')


def prepare_backend(name, host, config, backend=None):
    """Install what the front's start command needs; never start the model."""
    if serves_tensorfold(name, host, backend):
        root = tensorfold_root(host)
        made = on_host(host, [f'umask 077 && mkdir -p {root}/runtime-cache'])
        backend = tensorfold_backend(name)
        if made.returncode or put(host, CODE / 'scripts/spark-tensorfold-worker.py', f'{root}/worker.py') or put(host, ROOT / f'backends/{backend}.launch.json', f'{root}/launch-args.json'):
            raise SystemExit('could not prepare Spark TensorFold worker')
        manifest = json.dumps({'container': f'{PREFIX}-spark-tensorfold', 'port': 18885}) + '\n'
        written = on_host(host, [f'umask 077 && cat > {root}/manifest.json'], input=manifest)
        if written.returncode or put(host, ROOT / f'backends/{backend}.json', f'{root}/backend-manifest.json'):
            raise SystemExit('could not prepare Spark TensorFold worker')
        run_playbook('unit', name, '-e', 'unit=spark-tensorfold', '-e', f'directory={root}')
        return
    service = manifest_service(name, backend)
    if service:
        role = service['unit'].removeprefix(f'{PREFIX}-')
        if role not in installed_unit_roles():
            raise SystemExit(missing_unit(role))
        root = service['root']
        made = on_host(host, [f'umask 077 && mkdir -p {root}/watch'])
        key = api_key(name, host)
        written = on_host(host, [f'umask 077 && cat > {root}/api.key'], input=f'{key}\n')
        failed = made.returncode or written.returncode
        if not failed:
            failed = put(host, ROOT / f'backends/{backend}.json', f'{root}/backend-manifest.json')
        launch = ROOT / f'backends/{backend}.launch.json'
        if not failed and launch.exists():  # the engine flags this site claims the manifest with (decision 13)
            failed = put(host, launch, f'{root}/launch-args.json')
        if failed:
            raise SystemExit(f'could not prepare the {backend} service on {name}')
        run_playbook('unit', name, '-e', f'unit={role}', '-e', f'directory={root}')
        return
    if serves_strata(host):
        root, key = strata_root(host, backend), api_key(name, host)
        on_host(host, ['mkdir', '-p', f'{root}/backends', f'{root}/watch'])
        pushed = on_host(host, [f'umask 077 && cat > {root}/api-key-claim.env'], input=f'LITELLM_MASTER_KEY={key}\n')
        failed = pushed.returncode or put(host, CODE / 'scripts/strata-start-worker.py', f'{root}/start-worker.py')
        if backend is not None and not failed:
            failed = put(host, ROOT / f'backends/{backend}.json', f'{root}/backends/{backend}.json')
        if failed:
            raise SystemExit(f'could not prepare the Strata start on {name}')
    elif 'default_profile' in host:
        run_playbook('start', name, '-e', f"model={host['default_profile']}", '-e', f"serve_port={config['backend_port']}",
                     '-e', 'ondemand=true')
    else:
        raise SystemExit(f'{name} has no tested default profile; nothing to claim')


def start_watch(name, host, root, config):
    """(Re)start the trial resource log and stop rules (strata-trial-watch.py) for a claimed manifest backend.

    The unit it watches and stops is the one this claim resolved, not a name hard-coded here, and a containerized
    engine names its container so the watch reads that container's PIDs and cgroup (see the watch's own docstring).
    """
    label = f"{config['backend']}-{(config['context'] or 0) // 1024 or 'default'}k"
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    on_host(host, ['sudo', '-n', 'systemctl', 'stop', WATCH])
    on_host(host, ['sudo', '-n', 'systemctl', 'reset-failed', WATCH])
    if put(host, CODE / 'scripts/strata-trial-watch.py', f'{root}/trial-watch.py'):
        raise SystemExit(f'could not install the trial watch on {name}')
    watch = ['sudo', '-n', 'systemd-run', f'--unit={WATCH}', f'--uid={declared.account(host, ROOT)}', '/usr/bin/python3',
             f'{root}/trial-watch.py', '--log', f'{root}/watch/{label}-{stamp}.jsonl',
             '--port', str(config['backend_port']), '--key-file', config['key_file'],
             '--unit', config['unit'], '--front', PROXY]
    if config.get('container'):
        watch += ['--container', config['container']]
    if config.get('status_path'):
        watch += ['--status-path', config['status_path']]
    started = on_host(host, watch)
    if started.returncode:
        raise SystemExit(f'trial watch failed to start on {name}: {started.stderr.strip()}')


def running_front_directory(host):
    """Which root's on-demand front the host actually runs (ADR 0004), read from the unit's own ExecStart.

    A host can hold several prepared roots and each carries its own `ondemand/config.json`, and two of those files
    can agree in every field a claim sets while the unit points at the other root. Comparing only the file in the
    root about to be used lets a claim of a different backend report `unchanged`, skip `install_front`, and leave
    the unit serving the previous root's front."""
    shown = on_host(host, ['systemctl', 'cat', PROXY, '--no-pager'])
    for line in shown.stdout.splitlines():
        if line.startswith('ExecStart=') and f'/{PROXY_DIRECTORY}/proxy.py' in line:
            return line.split(f'/{PROXY_DIRECTORY}/proxy.py')[0].split()[-1] + f'/{PROXY_DIRECTORY}'
    return None


def enable_ondemand(name, host, idle_minutes, backend=None, context=None, engine_options=None):
    """Start (or reconfigure) the on-demand front; an always-on backend of ours is stopped first."""
    root, config = ondemand_config(name, host, idle_minutes, backend, context, engine_options)
    if on_host(host, ['systemctl', 'is-active', '--quiet', PROXY]).returncode:
        on_host(host, ['sudo', '-n', 'systemctl', 'stop', config['unit']])  # it holds the API port
    prepare_backend(name, host, config, backend)
    directory = f'{root}/{PROXY_DIRECTORY}'
    wanted = json.dumps(config, indent=2) + '\n'
    current = on_host(host, [f'cat {directory}/config.json; sha256sum < {directory}/proxy.py'])
    script_hash = hashlib.sha256((CODE / 'scripts/ondemand-proxy.py').read_bytes()).hexdigest()
    unchanged = current.stdout == wanted + script_hash + '  -\n' and running_front_directory(host) == directory
    if not unchanged:
        on_host(host, ['sudo', '-n', 'systemctl', 'stop', config['unit']])  # never adopt a model started with other settings
        for other in other_serving_units(host, config['unit'], name):  # the backend this front replaces may still hold the RAM and the internal port
            on_host(host, ['sudo', '-n', 'systemctl', 'stop', other])
    # A host's own TensorFold service is not a Strata trial even when the host declares strata_trial_roots: its claim
    # carries the TensorFold manifest as `backend` (T4.1), but that config has no `backend` field and the watch's stop
    # rules name the Strata unit, so a bare TensorFold claim must not start the trial watch. A manifest that declares
    # its own service is a trial of the same kind: it is claimed, loaded on demand, and has to be watched.
    has_service = manifest_service(name, backend) is not None
    trial = (serves_strata(host) or has_service) and not serves_tensorfold(name, host, backend)
    if trial and backend is not None and (not unchanged or on_host(host, ['systemctl', 'is-active', '--quiet', WATCH]).returncode):
        start_watch(name, host, root, config)  # the trial watch is a claimed-backend thing; Spark keeps its own resource log
    elif (serves_strata(host) or has_service or declared_units(name)) and (backend is None or not trial):
        on_host(host, ['sudo', '-n', 'systemctl', 'stop', WATCH])  # a trial's watch does not outlive its backend
    if unchanged and not on_host(host, ['systemctl', 'is-active', '--quiet', PROXY]).returncode:
        return
    written = on_host(host, [f'umask 077 && mkdir -p {directory} && cat > {directory}/config.json'], input=wanted)
    if written.returncode or put(host, CODE / 'scripts/ondemand-proxy.py', f'{directory}/proxy.py'):
        raise SystemExit(f'could not install the on-demand front on {name}')
    install_front(name, host, directory)


def start_tunnel(name, host):
    unit = f'{PREFIX}-tunnel-{name}'
    if host.get('backend') == 'strata':
        if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode:
            subprocess.run(['sudo', '-n', 'systemctl', 'reset-failed', unit], capture_output=True)
            subprocess.run(['sudo', '-n', 'systemd-run', f'--unit={unit}', f'--uid={declared.local_account(hosts(), ROOT)}',
                            '--property=Restart=on-failure',
                            '--property=RestartSec=10', '/usr/bin/ssh', '-x', '-NT', '-o', 'BatchMode=yes',
                            '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=20', '-o', 'ServerAliveCountMax=3',
                            '-L', f"127.0.0.1:{host['strata_tunnel_port']}:127.0.0.1:{host['strata_api_port']}",
                            login(host)], check=True, capture_output=True)
    elif 'tunnel_port' in host:
        subprocess.run(['sudo', '-n', 'systemctl', 'start', unit], check=True)


def ondemand_call(name, host, method, path, timeout=30, connect_wait=0):
    """Call the host's on-demand front through the tunnel; return (HTTP status, body)."""
    port, key = endpoint(name, host)
    request = urllib.request.Request(f'http://127.0.0.1:{port}/ondemand/{path}', method=method,
                                     headers={'Authorization': 'Bearer ' + key})
    deadline = time.monotonic() + connect_wait  # a tunnel that was just started needs a moment
    while True:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.load(error)
            except ValueError:
                return error.code, {}
        except OSError as error:
            if time.monotonic() > deadline:
                return None, {'error': type(error).__name__}
            time.sleep(2)


def load_causes(code, last_error):
    """Why a warm failed, from ondemand-proxy.py's own LoadError sentences; nothing it did not say."""
    if code is None:
        return ['the control tunnel or the front on the host is down']
    for fragment, cause in LOAD_FAILURES:
        if fragment in (last_error or ''):
            return [cause]
    return []


def warm(name, host):
    code, status = ondemand_call(name, host, 'POST', 'warm', timeout=1400, connect_wait=30)
    print(json.dumps({'warm': name, **status}))
    if code != 200:
        raise SystemExit(explain(f'{name} did not load: {status.get("last_error") or status}',
                                 load_causes(code, status.get('last_error')), f'{cli_name()} status',
                                 f'{cli_name()} ops preflight {name}, {cli_name()} ops storage {name}, '
                                 f'then {cli_name()} warm {name}'))


# One `stat` per pinned weight file, on the host. F6's measured half: presence and size, never a hash — a claim
# that has to hash 85 GiB before it can serve is a claim nobody runs.
WEIGHTS_PRESENT = r'''set -u
while [ "$#" -gt 1 ]; do
  path=$1; size=$2; shift 2
  if [ -f "$path" ] && [ "$(stat -c %s -- "$path" 2>/dev/null)" = "$size" ]; then
    echo "present $path"
  else
    echo "missing $path"
  fi
done
'''


def weight_guard(name, host, backend):
    """F6 (T5.3): refuse to serve from a root that does not hold the weights its manifest pins.

    Two reads, in the cheap order. The ledger first: `ops thin` records a thin there as it makes one, and a thin
    N-1 is exactly the root that cannot be started (F5 reads a rollback aloud, never assumes it). Then the host,
    because a ledger line is a declaration and a weight can be absent for a reason the ledger does not know about.
    A host that cannot be answered is not a refusal here — `claim` is about to work with it at length, and its own
    connection failure is the clearer message.

    Where it looks is declared, not guessed, because the two manifest shapes put their weights in two places: a
    manifest that pins them flat (files) is served from a trial root the inventory names and holds them under
    <root>/data/models; a manifest that declares its service carries its model as files under the model.directory
    the manifest itself names, relative to the host's project_root - the reading ops storage already uses, so the
    guard and the storage ledger cannot disagree about where 100 GB is supposed to be.
    """
    if backend is None or serves_tensorfold(name, host, backend):
        return
    service = manifest_service(name, backend)
    if not serves_strata(host) and service is None:
        return
    manifest = json.loads((ROOT / f'backends/{backend}.json').read_text())
    if not upgrade.strata_shaped(manifest) and service is None:
        # The shape Spark's own TensorFold service is written in, on the host that serves it as its backend: its
        # weights sit in the checkpoint directory that manifest names, and today's claim of that service has never
        # been checked against them. Making the incumbent's claim start checking 32 pinned files changes a live
        # host's behaviour, which is a batch of its own with a window of its own, not a side effect of this reading.
        return
    pinned = upgrade.weight_entries(manifest)  # flat `files` or nested `model.files`: the same manifest, two shapes
    if not pinned:
        return  # a manifest that pins no weight file gives F6 nothing to check
    refusal = retire.thin_ledger(declared.load(ROOT).retention, name, backend)
    if refusal:
        raise SystemExit(refusal)
    if upgrade.strata_shaped(manifest):
        if not serves_strata(host):
            raise SystemExit(f'{backend}: a manifest that pins its weights flat is served from a root '
                             f'infra/inventory.yml declares (`strata_trial_roots` or `strata_root`); {name} declares '
                             'none, so there is no root to check and none to serve from')
        where = strata_root(host, backend) + '/data/models'
    else:
        directory = identities.model_directory(manifest)
        if directory is None:
            raise SystemExit(explain(
                f'{backend}: its manifest pins weight files and names no model directory, so this site cannot say '
                f'where they are on {name}',
                ['a manifest that declares its service carries its model as files under `model.directory`, relative '
                 "to the host's `project_root` - what `ops storage` lists as the weight directory it holds",
                 'a manifest that keeps its weights inside its engine root pins them flat (`files`), which is read '
                 'from <root>/data/models'],
                f'grep -n -A 4 "model" backends/{backend}.json',
                f'declare the directory (or pin flat), then {cli_name()} ops prepare {name} --backend {backend} '
                '--check'))
        where = host['project_root'].rstrip('/') + '/' + directory
    named = {f"{where}/{entry['path']}": entry['path'] for entry in pinned}
    argv = ['bash', '-s', name] + [value for entry in pinned
                                   for value in (f"{where}/{entry['path']}", str(entry['size']))]
    done = on_host(host, argv, input=WEIGHTS_PRESENT, timeout=60)
    if done.returncode:
        return
    answers = {line.removeprefix('present ').removeprefix('missing '): line.startswith('present ')
               for line in done.stdout.splitlines() if line.startswith(('present ', 'missing '))}
    missing = retire.weights_missing(manifest, {relative: answers.get(path, True) for path, relative in named.items()})
    if missing:
        listed = ', '.join(f"{entry['path']} ({entry['size']} B)" for entry in missing)
        raise SystemExit(f'{backend}: {len(missing)} pinned weight file(s) are not in {where} as this manifest pins '
                         f'them: {listed}. A root without its weights does not start (F6). Put them back with '
                         f'{cli_name()} ops restore {name} --backend {backend}, or prepare the root again with '
                         f'{cli_name()} ops prepare {name} --backend {backend}')


def claim(name, host, switch, idle_minutes, warm_now, backend=None, context=None, engine_options=None, by='claim'):
    if backend is not None and not serves_strata(host) and manifest_service(name, backend) is None:
        raise SystemExit(undeclared_backend(name, host, backend))
    if serves_tensorfold(name, host, backend):
        # A `spark_tensorfold` host's TensorFold manifest is its own backend, not a claim knob; the route target
        # declares it, so the claim and the gateway's identity comparison say the same thing (T4.1). An explicit
        # --backend naming a Strata manifest takes the other branch so the host can serve a Strata trial.
        backend = tensorfold_backend(name)
        context = context or json.loads((ROOT / f'backends/{backend}.json').read_text())['context']
    if backend is None and 'strata_backend' in host:
        # a dedicated worker's default: its inventory strata_backend at that manifest's prepared context
        backend = host['strata_backend']
        context = context or json.loads((ROOT / f'backends/{backend}.json').read_text())['context']
    if backend is None and serves_strata(host):
        # a borrowed Strata worker's default: with no host-wide legacy root to fall back to, a bare
        # `claim <host>` resolves to the manifest `strata_default_backend` names, at that manifest's own context
        backend = host['strata_default_backend']
        context = context or json.loads((ROOT / f'backends/{backend}.json').read_text())['context']
    if backend is not None:
        # F6: before a front, a tunnel or a load (T5.3). Which claims have a declared place on this host to check
        # - and which shapes are not checked yet - is the guard's own answer, not a condition written here.
        weight_guard(name, host, backend)
    if switch:
        claimed_target(name, backend, context)  # refuse before anything starts
    enable_ondemand(name, host, idle_minutes, backend, context, engine_options=engine_options)
    start_tunnel(name, host)
    # a claim that installed a front is a claim even when the first load fails, so the record precedes the warm (D2.2)
    recorded = backend if backend is not None else host.get('default_profile')
    claims.write(ROOT, name, recorded, context, idle_minutes, by)
    own_tensorfold = host.get('spark_tensorfold') and backend == tensorfold_backend(name)
    profile = (json.loads((ROOT / f'backends/{backend}.json').read_text())['id'] if own_tensorfold else
               backend if backend is not None else host['default_profile'])
    print(json.dumps({'claimed': name, 'profile': profile, 'context': context, 'idle_unload_minutes': idle_minutes}))
    if warm_now:
        warm(name, host)
    if switch:
        subprocess.run([sys.executable, str(CODE / 'scripts/route.py'), 'set', claimed_target(name, backend, context)], check=True)


def reclaim(inventory):
    """Serve the current local-coder target again; its route entry says what it serves."""
    spec = json.loads(route.ROUTES.read_text())['targets'][route.current()]
    host = inventory[spec['node']]
    if not on_demand(host):
        raise SystemExit(f"{spec['node']} is not an on-demand host; use {cli_name()} ops start")
    backend = spec.get('backend')  # what the target declares is what the claim must serve (IP-02, T4.1)
    claim(spec['node'], host, False, host['idle_unload_minutes'], True, backend, spec['context_window'] if backend else None,
          by='reclaim')


def release(name, host):
    if route.current() in targets_on(name):
        default = json.loads(route.ROUTES.read_text())['default']
        if default in targets_on(name):
            raise SystemExit('the default local-coder target is on this host; change gateway/local-coder.json first')
        subprocess.run([sys.executable, str(CODE / 'scripts/route.py'), 'set', default], check=True)
    script = RELEASE % (CONTROL_SCOPE if host.get('ansible_connection') == 'local' else WORKER_SCOPE)
    remote = on_host(host, ['bash', '-s'], input=script, timeout=300)
    if remote.returncode:
        raise SystemExit(f'remote release failed on {name}: {remote.stderr.strip()}')
    unit = f'{PREFIX}-tunnel-{name}'
    if subprocess.run(['systemctl', 'cat', unit], capture_output=True).returncode == 0:
        subprocess.run(['sudo', '-n', 'systemctl', 'stop', unit], check=True)
    claims.clear(ROOT, name)
    report = parse_report(remote.stdout)
    left = report['units'] + report['user_units'] + report['containers']
    print(json.dumps({'released': name, 'left': left, 'gpu_apps_not_ours_to_stop': report['gpu_apps']}))
    if left:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('claim', 'warm', 'unload', 'status', 'reclaim', 'release'))
    parser.add_argument('node', nargs='?', help='warm: default the current local-coder target; status: default all')
    parser.add_argument('--route', action='store_true', help='claim: point local-coder at this host')
    parser.add_argument('--idle', type=float, help='claim: idle minutes before unload (0 = never); default inventory idle_unload_minutes')
    parser.add_argument('--no-warm', action='store_true', help='claim: do not load the model now')
    parser.add_argument('--backend',
                        choices=sorted(stem for stem in backend_names.manifests(ROOT) if not stem.startswith('tf-')),
                        type=lambda name: backend_names.resolve_in(ROOT, name),  # a retired name still answers
                        help='claim: serve this manifest where it can be served - a Strata host\'s trial root, or the '
                             'service its own manifest declares')
    parser.add_argument('--context', type=int, help="claim with --backend: served context (default: the manifest's)")
    parser.add_argument('--engine-option', action='append', metavar='NAME=VALUE',
                        help='claim with --backend: an engine option to claim this host with, NAME as the engine spells '
                             'it without the dashes (experts=6). Only names the manifest lists in engine_flags; every '
                             'on-demand load of this claim re-applies it, and the start state records it')
    args = parser.parse_args()
    inventory = hosts()
    if args.action == 'status':
        for name in [args.node] if args.node else [n for n, h in inventory.items() if on_demand(h)]:
            code, status = ondemand_call(name, inventory[name], 'GET', 'status', timeout=10)
            print(json.dumps({'host': name, **(status if code == 200 else {'state': 'not on demand', 'http': code})}))
        return
    if args.action == 'reclaim':
        with mutation.hold_or_exit(ROOT, 'reclaim'):
            reclaim(inventory)
        return
    if args.action == 'warm' and not args.node:
        args.node = json.loads(route.ROUTES.read_text())['targets'][route.current()]['node']
    host = inventory.get(args.node)
    if not host or not (host.get('borrowed') or args.action != 'release' and host.get('ondemand_persistent')):
        parser.error(f'{args.node} is not a borrowed host; dedicated workers use {cli_name()} ops start/stop')
    if args.action == 'claim':
        if args.context and not args.backend:
            parser.error('--context needs --backend')
        if args.engine_option and not args.backend:
            parser.error('--engine-option needs --backend: an engine option is checked against that manifest')
        with mutation.hold_or_exit(ROOT, f'claim {args.node}'):
            claim(args.node, host, args.route, host['idle_unload_minutes'] if args.idle is None else args.idle,
                  not args.no_warm, args.backend, args.context, args.engine_option)
    elif args.action == 'warm':
        warm(args.node, host)
    elif args.action == 'unload':
        code, status = ondemand_call(args.node, host, 'POST', 'unload', timeout=300)
        print(json.dumps({'unload': args.node, **status}))
        if code != 200:
            raise SystemExit(1)
    else:
        with mutation.hold_or_exit(ROOT, f'release {args.node}'):
            release(args.node, host)


if __name__ == '__main__':
    main()
