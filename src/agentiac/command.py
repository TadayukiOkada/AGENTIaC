"""agentiac: daily use and operation of the local inference cluster.

One command instead of make targets (ADR 0006). Daily commands work from any
directory; operations and evaluation run the existing scripts and playbooks from
the project root, so their relative paths (artifacts/..., profiles/...) are
relative to the project as before.
"""
import argparse
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import time
import urllib.error
import urllib.request

from agentiac import adapters, backup, catalog, declared, doctor as doctor_module, evidence as evidence_module, hygiene, identities, model_names, mutation, route_aliases, observe as observe_module, sessions as sessions_module, tasklog, upgrade, services
from agentiac import paths
from agentiac.errors import explain
from agentiac.errors import command as cli_name

ROOT = paths.root()
CODE = paths.CODE
PYTHON = CODE / '.venv/bin/python'
CLOSURES = ROOT / 'artifacts/closures'
GATEWAY_PYTHON = CODE / '.venv-gateway/bin/python'  # LiteLLM, for the ChatGPT sign-in
WORKTREES = Path.home() / 'agent-worktrees'  # same as scripts/agent.py

def inventory():
    return services.inventory.nodes(ROOT)


def routes():
    return json.loads((ROOT / 'gateway/local-coder.json').read_text())


def pi_models():
    return json.loads((ROOT / 'agents/pi/models.json').read_text())['providers']['local-infra']['models']


def gateway_aliases():
    import yaml
    base = yaml.safe_load((ROOT / 'gateway/config.yaml').read_text())['model_list']
    local = routes()
    return ([m['model_name'] for m in base] + list(route_aliases.legacy_sources(local))
            + list(route_aliases.api_targets(local, local['default'])))


def code_aliases():
    """What scripts/agent.py accepts: pi models, local-coder and its variants, the router."""
    local = routes()
    names = [name for name in route_aliases.api_targets(local, local['default']) if name != local['alias']]
    return [local['alias'], 'router'] + [m['id'] for m in pi_models()] + names


def legacy_names(local=None):
    """Names still accepted but not offered by completion: a target key and its `local-coder-<target>`
    API name, when that target's declaration has a node-first short alias. A target whose declaration
    has none stays offered, so nothing becomes unreachable from the completion list."""
    from agentiac import declared, model_names
    local = local or routes()
    if route_aliases.version(local) >= 2:
        return route_aliases.retired_names(local)
    graph = declared.load(ROOT)
    declaration = {target: model_names.describe(graph, target)['declaration_id'] for target in local['targets']}
    covered = {declaration[target] for target in local.get('short_aliases', {}).values()}
    return {name for target in local['targets'] if declaration[target] in covered
            for name in (target, f"{local['alias']}-{target}")}


def offer_node_first(action):
    """Mark an alias/selector argument: its completion omits legacy_names(); validation is unchanged."""
    action.offer_node_first = True
    return action


def script(name, *argv, python=PYTHON):
    return [str(python), str(CODE / 'scripts' / name), *argv]


def playbook(name, *argv):
    return [str(CODE / '.venv/bin/ansible-playbook'), '-i', 'infra/inventory.yml', str(CODE / f'infra/{name}.yml'), *argv]


def profile_for(host, profile):
    if profile:
        return profile
    default = inventory()[host].get('default_profile')
    if not default:
        raise SystemExit(explain(f'{host} has no default_profile in infra/inventory.yml; give --model PROFILE',
                                 [f'infra/inventory.yml sets no default_profile for {host}'], 'ls profiles/',
                                 f'add --model with a name from profiles/, or `{cli_name()} claim {host}` if it '
                                 'serves a prepared backend'))
    return default


# Each pass-through command returns the argv to run from the project root.

def code(args):
    argv = script('agent.py', '--repo', str(Path(args.repo).resolve()), '--alias', args.model)
    argv += ['--allow-cloud'] * args.allow_cloud + ['--in-place'] * args.in_place + ['--loop-guard'] * args.loop_guard
    argv += ['--thinking', args.thinking] if args.thinking else []
    argv += ['--resume'] if args.resume else []
    pi = (['-p', args.prompt] if args.prompt else []) + args.extra
    return argv + (['--', *pi] if pi else [])


def prepare_weights(host, backend_id, reuse_root, reuse_declared=False, download=False, graph=None):
    """Say where a preparation's weights come from, before it starts (T5.8 (S §9 F12), src/agentiac/upgrade.py).

    A host that already holds the pinned bytes is asked once, not downloaded from: the IQ3_XXS pair is pinned at the
    same SHA-256 by the 0.2.8, 0.2.10 and 0.2.12 manifests (70.63 GiB on an Orin) and the Gyro-M file by the
    0.2.8 and 0.2.11 manifests (85.65 GiB on the RTX 4080 host). `--reuse-root` is checked against the ledger and the manifest;
    a second way to say the same thing is `--reuse-declared`; `--download` is the way to mean the download.
    """
    _manifest, _key, _root, answer = upgrade.prepare_gate(graph or declared.load(ROOT), host, backend_id,
                                                          reuse_root, reuse_declared, download)
    if answer[0] == 'reuse':
        print(f'weights: copied from {answer[1]} ({answer[2]} pins the same (path, size, sha256) set)',
              file=sys.stderr)
        return answer[1]
    if answer[0] == 'download':
        print(f'weights: nothing on {host} pins these bytes — a {round(answer[1] / upgrade.GIB, 1)} GiB download, '
              f'declared', file=sys.stderr)
    return reuse_root


def check_core_pin(root=None):
    """When the site checkout has a `core/` submodule (D6.3, T9.8), refuse when `core/` is dirty or not at the commit
    the site pins (`git ls-tree HEAD core`). Without `core/`, do nothing."""
    root = Path(root) if root is not None else ROOT
    core = root / paths.CORE_DIR
    if not core.is_dir():
        return
    tree = git(root, 'ls-tree', 'HEAD', paths.CORE_DIR).stdout.strip().split()
    pinned = tree[2] if len(tree) >= 3 and tree[1] == 'commit' else None
    head = git(core, 'rev-parse', 'HEAD').stdout.strip()
    dirty = git(core, 'status', '--porcelain').stdout.strip()
    if dirty:
        raise SystemExit(explain(f'{paths.CORE_DIR}/ has uncommitted changes',
                                 [f'the pinned core at {core} is dirty'],
                                 f'git -C {core} status',
                                 f'clean {paths.CORE_DIR}/ or pass --allow-dirty to {cli_name()} ops deploy'))
    if not pinned or head != pinned:
        shown_pin = pinned[:8] if pinned else 'untracked'
        raise SystemExit(explain(f'{paths.CORE_DIR}/ is at {head[:8] or "unknown"}, not the pinned {shown_pin}',
                                 [f'{root} pins {paths.CORE_DIR}/ at {shown_pin}'],
                                 f'git -C {root} submodule status {paths.CORE_DIR}',
                                 f'run `git -C {root} submodule update --init`, or pass --allow-dirty'))


def record_deploy(root=None):
    """Record the deployed site and core revisions in artifacts/deployed.json (D6.3, T9.8)."""
    root = Path(root) if root is not None else ROOT
    site = git(root, 'rev-parse', 'HEAD').stdout.strip()
    dirty = bool(git(root, 'status', '--porcelain').stdout.strip())
    core_dir = root / paths.CORE_DIR
    if core_dir.is_dir():
        core = git(core_dir, 'rev-parse', 'HEAD').stdout.strip()
        dirty = dirty or bool(git(core_dir, 'status', '--porcelain').stdout.strip())
    else:
        core = site
    (root / 'artifacts/deployed.json').write_text(json.dumps({
        'revision': site, 'site': site, 'core': core, 'dirty': dirty,
        'time': datetime.datetime.now(datetime.timezone.utc).isoformat()}) + '\n')


def ops(args):
    action = args.action
    if action in ('build', 'runtime', 'stop'):
        return playbook(action, '--limit', args.host)
    if action == 'node-config':
        return playbook('node-config', '--limit', args.host, *['--check', '--diff'] * args.check)
    if action in ('download', 'start'):
        return playbook(action, '--limit', args.host, '-e', f'model={profile_for(args.host, args.model)}')
    if action == 'deploy':
        if not args.check and not args.allow_dirty:
            check_core_pin(ROOT)
        return playbook('control', *['--check', '--diff'] * args.check, *(['--tags', args.only] if args.only else []),
                        *['-e', 'allow_dirty=true'] * args.allow_dirty)
    if action == 'netwatch':
        return playbook('netwatch', '--limit', ','.join(args.hosts))
    if action == 'netwatch-report':
        host = inventory()[args.host]
        return ['ssh', '-o', 'BatchMode=yes', f"{declared.account(host, ROOT)}@{host['ansible_host']}",
                'python3', 'agentiac/scripts/netwatch.py', 'report']
    if action == 'backend-probe':
        return playbook('backend-probe', '--limit', args.host, '-e', f'backend_probe={args.backend}')
    if action == 'logs':
        unit = declared.resource('chatgpt' if args.chatgpt else 'gateway', ROOT)
        return ['journalctl', '-u', unit, '-n', str(args.lines), '--no-pager']
    if action == 'prepare':
        reuse = args.reuse_root
        if args.backend and not args.check:
            reuse = prepare_weights(args.host, args.backend, reuse, reuse_declared=args.reuse_declared,
                                    download=args.download)
        return playbook('prepare', '--limit', args.host, *['--check', '--diff'] * args.check,
                        *(['-e', f'backend_id={args.backend}'] if args.backend else []),
                        *(['-e', f'reuse_root={reuse}'] if reuse else []), *['-e', 'isolated=true'] * args.isolated)
    if action == 'drift':
        return script('drift.py', *['--json'] * args.json)
    if action == 'update':
        return script('update.py', args.update_action, *([args.id] if args.update_action == 'plan' else []),
                      *['--json'] * args.json, *['--notify'] * getattr(args, 'notify', False))
    if action == 'backup':
        return script('backup-control.py', args.backup_action, '--destination', args.destination,
                      '--keep', str(args.keep), *['--dry-run'] * args.dry_run)
    if action == 'recover':
        argv = [args.recover_action]
        if args.recover_action == 'pause':
            argv += [f'{args.minutes:g}'] + (['--reason', args.reason] if args.reason else [])
        argv += (['--check'] * getattr(args, 'check', False)) + (['--json'] * getattr(args, 'json', False))
        return script('recover.py', *argv)
    if action == 'restore' and not args.host:
        if not args.into:
            raise SystemExit(explain('ops restore needs one of its two forms',
                                     ['`ops restore HOST --backend ID` puts a thin N-1 root back to full',
                                      '`ops restore --into PATH` restores control from the backup archive'],
                                     f'{cli_name()} ops restore --help',
                                     'name the HOST and the --backend ID, or the --into PATH; never both'))
        if args.backend:
            raise SystemExit(explain('ops restore --into PATH is control, not a root',
                                     ['`--backend ID` belongs to `ops restore HOST --backend ID`',
                                      'the control restore takes the archive and the checkout to write into'],
                                     f'{cli_name()} ops restore --help',
                                     'drop --backend, or name the HOST instead'))
        argv = ['--into', args.into, *['--dry-run'] * args.dry_run]
        argv += ['--destination', args.destination] if args.destination else []
        argv += ['--archive', args.archive] if args.archive else []
        argv += ['--sha256', args.sha256] if args.sha256 else []
        return script('restore-control.py', *argv)
    if action == 'eval-image':
        return script('evalimage.py', *([args.what] if args.what else []),
                      *(['--tag', args.tag] if args.tag else []))
    if action == 'storage':
        return script('storage.py', *args.hosts, *['--json'] * args.json, *['--candidates'] * args.candidates)
    if action == 'retire':
        stage = 'quarantine' if args.quarantine else 'purge' if args.purge else 'check'
        named = (['--backend', args.backend] if args.backend else
                 ['--image', args.image] if args.image else ['--name', args.name])
        return script('retire.py', stage, args.host, *named, *['--json'] * args.json,
                      *( ['--shorten-wait', args.shorten_wait] if args.shorten_wait else []))
    if action in ('thin', 'restore') and getattr(args, 'host', None):
        if not args.backend:
            raise SystemExit(explain(f'ops {action} {args.host} needs --backend ID',
                                     ['the ledger line names the backend whose weights are being moved',
                                      f'{cli_name()} model list shows the declared backends of that host'],
                                     'infra/retention.yml',
                                     f'{cli_name()} ops {action} {args.host} --backend <a backend the ledger holds '
                                     f'on {args.host}>'))
        stage = 'thin' if action == 'thin' else 'restore'
        return script('retire.py', stage, args.host, '--backend', args.backend,
                      *['--dry-run'] * args.check, *['--json'] * args.json)
    if action == 'upgrade':
        return script('upgrade.py', args.upgrade_action, args.host, '--backend', args.backend,
                      *['--json'] * args.json)
    if action == 'preflight':
        return script('preflight.py', *args.hosts, *['--json'] * args.json)
    if action == 'hosts':
        return script('status.py')
    if action == 'inventory':
        remote = [f"{declared.account(h, ROOT)}@{h['ansible_host']}" for h in inventory().values()
                  if h.get('ansible_connection') != 'local']
        return script('inventory.py', '--hosts', 'local', *remote)
    if action == 'metrics':
        return script('sample-node.py', args.host, *args.rest)
    if action == 'memory':
        return script('host-memory.py', args.host, '--floor', str(args.floor), '--max-age', str(args.max_age))
    if action == 'pipeline':
        return ['sudo', '-n', 'systemd-run', f'--unit={declared.resource("pipeline-" + args.host, ROOT)}', '--collect',
                f'--uid={declared.local_account(inventory(), ROOT)}',
                f'--working-directory={ROOT}', '--property=RuntimeMaxSec=12h', *script('pipeline.py', args.host)]
    raise AssertionError(action)


def evaluate(args):
    if args.action == 'watch':
        return [str(PYTHON), '-m', 'agentiac.evaluation_watch', *args.rest]
    if args.action == 'suite':
        return script('suite.py', *args.rest)
    if args.action == 'ladder':
        return script('ladder.py', *args.rest)
    if args.action == 'bench':
        return [str(PYTHON), '-m', 'agentiac.cli', *args.rest]
    if args.action == 'report' and args.failures:
        # The spelling PA §10 and §14 use. The classes are the suite's, so the work is suite.py's.
        return script('suite.py', 'report', '--failures',
                      *(['--alias', args.alias] if args.alias else []),
                      *(['--task', args.task] if args.task else []),
                      *(['--agent', args.agent] if args.agent else []),
                      *(['--against', args.against] if args.against else []),
                      *['--json'] * args.json)
    if args.action == 'report' and not args.database:
        raise SystemExit('eval report needs a benchmark state.sqlite, or --failures for the task suite')
    return script('aggregate-results.py', args.database, '--format', 'markdown', *args.rest)


PASS_THROUGH = {
    'code': code,
    'warm': lambda a: script('borrow.py', 'warm', *([a.host] if a.host else [])),
    'unload': lambda a: script('borrow.py', 'unload', a.host),
    'claim': lambda a: script('borrow.py', 'claim', a.host, *['--route'] * a.route,
                              *(['--idle', str(a.idle)] if a.idle is not None else []), *['--no-warm'] * a.no_warm,
                              *(['--backend', a.backend] if a.backend else []), *(['--context', str(a.context)] if a.context else []),
                              *[x for option in (a.engine_option or []) for x in ('--engine-option', option)]),
    'reclaim': lambda a: script('borrow.py', 'reclaim'),
    'release': lambda a: script('borrow.py', 'release', a.host),
    'route': lambda a: script('route.py', *(['set', a.target] if a.target else ['show'])),
    'chatgpt': lambda a: script('chatgpt-login.py', *{'login': [], 'status': ['--status'], 'logout': ['--logout']}[a.action],
                                python=GATEWAY_PYTHON),
    'check': lambda a: script('smoke.py', '--alias', a.model, *['--no-pi'] * a.no_pi),
    'ops': ops,
    'eval': evaluate,
}


def missing_program(program):
    """run() execs the venv's tools and system ones (ssh, journalctl, sudo); they fail for different reasons."""
    what = f'{program} not found; see Bootstrap in README.md'
    if '.venv' in program:
        return explain(what, ['this checkout has not been bootstrapped', 'this is a worktree, which has no .venv'],
                       f'ls {Path(program).parent}', 'follow Bootstrap in README.md, or run from ~/src/agentiac')
    return explain(what, [f'{program} is not installed on this host, or not on PATH'], f'command -v {program}',
                   f'install {program}, then run this command again')


def run(argv, extra_env=None):
    """Replace this process (terminal, signals and exit status go straight to the command)."""
    os.chdir(ROOT)
    env = dict(os.environ, ANSIBLE_CONFIG=str(CODE / 'infra/ansible.cfg'))
    env.update(extra_env or {})
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(CODE / 'src'), env.get('PYTHONPATH')]))
    try:
        os.execvpe(argv[0], argv, env)
    except FileNotFoundError:
        raise SystemExit(missing_program(argv[0]))


# Native commands

def host_state(name, host):
    """One line for a worker: on-demand state, always-on health, or not claimed."""
    sys.path.insert(0, str(CODE / 'scripts'))
    import borrow
    return services.status.host_state(name, host, SimpleNamespace(endpoint=borrow.endpoint,
                                              ondemand_call=borrow.ondemand_call))


def gateway_state():
    return services.status.gateway_state(ROOT, declared.gateway_url(ROOT, '/v1'))


def chatgpt_state():
    try:
        result = subprocess.run(script('chatgpt-login.py', '--status', python=GATEWAY_PYTHON), cwd=ROOT,
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f'unknown ({type(error).__name__})'
    lines = (result.stdout.strip() or result.stderr.strip()).splitlines()
    return lines[-1] if lines else f'unknown (exit {result.returncode})'


def deploy_state(route):
    """Which revision was deployed and whether a gateway still has to load a change; local files only."""
    path = ROOT / 'artifacts/deployed.json'
    if not path.exists():
        return 'no deploy recorded'
    deployed = json.loads(path.read_text())
    site = deployed.get('site') or deployed['revision']
    core = deployed.get('core') or site
    head = git(ROOT, 'rev-parse', 'HEAD').stdout.strip()
    core_dir = ROOT / paths.CORE_DIR
    core_head = git(core_dir, 'rev-parse', 'HEAD').stdout.strip() if core_dir.is_dir() else head
    pending = [kind for kind in route.SOURCES if route.pending(kind)]
    shown = site[:8] if core == site else f'{site[:8]} (core {core[:8]})'
    behind = ([] if site == head else [f'HEAD {head[:8]}']) + ([] if core == core_head else [f'core HEAD {core_head[:8]}'])
    return (shown + (f", {', '.join(behind)}" if behind else '')
            + (', deployed with uncommitted changes' if deployed.get('dirty') else '')
            + (f", {' and '.join(pending)} pending" if pending else ''))


def status(args):
    sys.path.insert(0, str(CODE / 'scripts'))
    import route
    hosts = inventory()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(hosts) + 2) as pool:
        gateway = pool.submit(gateway_state)
        states = {name: pool.submit(host_state, name, host) for name, host in hosts.items()}
        chatgpt = pool.submit(chatgpt_state)
        healthy, line = gateway.result()
        target = route.current()
        spec = routes()['targets'][target]
        graph = declared.load(ROOT)
        full_name = model_names.describe(graph, target)['full_name']
        shown = model_names.label(model_names.display_names(graph)[target], target)
        rows = [('gateway', line), ('local-coder', f"{shown} -> {full_name} ({spec['source_alias']})")]
        rows += [(name, f"{states[name].result()}{' [borrowed]' if hosts[name].get('borrowed') else ''}") for name in hosts]
        rows.append(('chatgpt', chatgpt.result()))
        rows.append(('deployed', deploy_state(route)))
    if getattr(args, 'json', False):
        print(json.dumps({'schema_version': 1, 'healthy': healthy, 'route': target,
                          'route_name': model_names.display_names(graph)[target],
                          'rows': [{'name': name, 'state': text} for name, text in rows]}))
    else:
        for name, text in rows:
            print(f'{name:<12} {text}')
    return 0 if healthy else 1


def save_snapshot(report):
    """One closure record per file, never overwritten: what a session left is evidence (T8.5)."""
    CLOSURES.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = CLOSURES / f'{stamp}.json'
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as file:
        file.write(json.dumps(report, sort_keys=True) + '\n')
    return path


def doctor(args):
    """agentiac doctor: everything a session start asks, read at once. Loads nothing (PA §10), and writes
    nothing unless --snapshot asks for the one closure file."""
    sys.path.insert(0, str(CODE / 'scripts'))
    import borrow
    report = doctor_module.summary(ROOT, borrow.hosts(), gateway_state)
    print(json.dumps(report, sort_keys=True) if args.json else doctor_module.screen(report))
    code = doctor_module.exit_code(report)
    if args.snapshot:
        try:
            print(save_snapshot(report), file=sys.stderr)
        except OSError as error:
            print(explain(f'no closure snapshot was written: {error}',
                          ['artifacts/closures is not writable', 'a snapshot from this same second already exists'],
                          f'ls -ld {CLOSURES}', f'fix that, then run {cli_name()} doctor --snapshot again'),
                  file=sys.stderr)
            return code or 3  # a closing session must not read a healthy exit as "state recorded"
    return code


def models(args):
    sys.path.insert(0, str(CODE / 'scripts'))
    import route
    local, target = routes(), route.current()
    router = json.loads((ROOT / 'agents/pi/router.json').read_text())
    spec = local['targets'][target]
    graph = declared.load(ROOT)
    full_name = model_names.describe(graph, target)['full_name']
    shown = model_names.label(model_names.display_names(graph)[target], target)
    rows = [(local['alias'], f"-> {shown} -> {full_name} ({spec['source_alias']})", 'local'),
            ('router', f"{router['low']}; hard tasks -> {router['high']} in {', '.join(router['cloud_repos'])}",
             'local, cloud after confirmation')]
    for alias, selected in local.get('short_aliases', {}).items():
        rows.append((alias, f"node-first configuration alias (route key {selected})", 'local'))
    if route_aliases.version(local) >= 2:
        for name in local['targets']:
            retired = [old for old, new in route_aliases.legacy(local).items() if new == name]
            rows.append((name, 'route target' + (f" (retired names {', '.join(retired)})" if retired else ''), 'local'))
    for model in pi_models():
        cloud = 'leaves this host' in model['name']
        rows.append((model['id'], f"{model['name'].replace(' (leaves this host)', '')}, context {model['contextWindow']}",
                     'sent to OpenAI' if cloud else 'local'))
    if route_aliases.version(local) >= 2:
        rows.append((f"{local['alias']}-<retired>", 'retired API names, still accepted until naming stage 4; '
                     f'{cli_name()} model names lists them', 'local'))
    else:
        rows.append((f"{local['alias']}-<target>", f"target aliases; {cli_name()} model names lists their full declarations", 'local'))
    for alias, what, where in rows:
        print(f'{alias:<24} {where:<34} {what}')
    return 0


def model(args):
    """agentiac model: the ids of PA §6, the one screen of T10.5, and the recommendation of T10.6.

    `model` lists descriptive configuration names with aliases and the T10.2 ids; `model TARGET`
    retains the original identity view. `list`, `show`,
    `names`, `compatibility` and `recommend` are additions (PA §13: additions only). Every value is
    read from the declared files, the inventory records and the derived Evidence DB. The
    generated documents are written only by `names --write` and `compatibility --write`.
    """
    verb = getattr(args, 'verb', None)
    if verb in catalog.VERBS:
        return {'list': model_list, 'show': model_show, 'names': model_names_list, 'compatibility': model_compatibility,
                'recommend': model_recommend}[verb](args)
    from agentiac import declared
    graph = declared.load()
    target = args.target or verb        # `model TARGET` is the T10.2 spelling of `model show TARGET`
    if target is None:
        for binding in model_names.bindings(graph):
            identity = identities.compute_target(graph, binding['target_aliases'][0])
            note = '' if not identity.unknowns else '  (' + ', '.join(sorted(identity.unknowns)) + ')'
            print(binding['full_name'])
            if binding['short_aliases']:
                print(f"  short aliases: {', '.join(binding['short_aliases'])}")
            if binding.get('legacy_names'):
                print(f"  retired names: {', '.join(binding['legacy_names'])}")
            print(f"  aliases: {', '.join(binding['target_aliases'])}\n"
                  f"  {identity.node:<7} class {identity.class_id}  config {identity.config_id}{note}")
        return 0
    target = route_aliases.resolve_target(graph.route, target)
    identity = identities.compute_target(graph, target).as_dict()
    print(json.dumps(identity, indent=2, sort_keys=True) if args.json else
          f"{target}: class {identity['class_id']}\nconfig {identity['config_id']}\n"
          + '\n'.join(f'  {k}: {v}' for k, v in sorted(identity['class_inputs'].items()))
          + '\n' + '\n'.join(f'  {k}: {v}' for k, v in sorted(identity['config_inputs'].items()))
          + ('\nunknown: ' + ', '.join(sorted(identity['unknowns'])) if identity['unknowns'] else ''))
    return 0


def _route_now():
    """Which target local-coder points at now; scripts/route.py owns that, including its fallback."""
    sys.path.insert(0, str(CODE / 'scripts'))
    import route
    return route.current()


def model_names_list(args):
    """Explicit legacy-target/API-alias bindings to descriptive declared configuration names."""
    graph = declared.load(ROOT)
    rows = model_names.bindings(graph)
    if getattr(args, 'write', False):
        path = ROOT / 'docs/model-names.md'
        text = model_names.documentation(graph)
        if not path.exists() or path.read_text() != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        print(path)
        return 0
    if args.json:
        print(json.dumps({'schema_version': 1, 'scope': 'declared-configuration',
                          'bindings': rows}, indent=2, sort_keys=True))
    else:
        for row in rows:
            print(f"{', '.join(row['short_aliases'] + row['target_aliases'])} -> {row['full_name']}")
            print(f"  API aliases: {', '.join(row['api_aliases'])}")
        print('Short API aliases require deployment of the rendered gateway config; full names remain descriptive labels.')
        print(f'{cli_name()} model show TARGET shows pins, state and evidence; --json includes naming details.')
    return 0


def model_list(args):
    """One row per declared target: what it is, what was read off its node, what has been measured."""
    rows = services.catalog.targets(ROOT, current=_route_now())
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    names = model_names.catalog_names(rows)
    print(f"{'':<2}{'name':<46} {'node':<6} {'ctx':>7} {'quantization':<13} {'engine':<9} "
          f"{'class':<9} {'config':<9} evidence")
    for row in rows:
        spec, identity, measured = row['declared'], row['identity'], row['evidence']
        shown = measured.get('why') or (f"{measured['usable']} usable of {measured['rows']}"
                                        if measured['rows'] else 'no measurement')
        print(f"{'* ' if row['current'] else '  '}{names[row['target']]:<46} {row['node']:<6} "
              f"{str(spec['context_window'] or '?'):>7} {spec['naming']['quantization']:<13} "
              f"{spec['engine']['version']:<9} {identity['class_id'][:8]:<9} {identity['config_id'][:8]:<9} {shown}")
        print(f"    -> {spec['naming']['full_name']}")
        print(f"       route key {row['target']}")
    ranked = sum(1 for row in rows if row['evidence']['usable'])
    print(f"\n* what local-coder serves now. {ranked}/{len(rows)} targets have a measurement that may rank; "
          f'the rest are declared only.')
    print(f'{cli_name()} model show TARGET for the inputs behind an id; {cli_name()} evidence show for the rows.')
    return 0


def model_show(args):
    """One target, three blocks: declared, state, evidence. They are printed apart and never merged."""
    if args.target is None:
        print(f'{cli_name()} model show needs a TARGET; {cli_name()} model list names them', file=sys.stderr)
        return 2
    row = catalog.show(ROOT, args.target, current=_route_now())
    if args.json:
        print(json.dumps(row, indent=2, sort_keys=True))
        return 0
    spec, identity, state, measured = row['declared'], row['identity'], row['state'], row['evidence']
    engine, weights = spec['engine'], spec['weights']
    print(f"{row['target']} — local-coder target on {row['node']} ({state['route']})")
    print(f"  name       {spec['naming']['full_name']} (declared configuration)")
    if spec['naming']['short_aliases']:
        print(f"  aliases    {', '.join(spec['naming']['short_aliases'])}")
    if spec['naming'].get('legacy_names'):
        print(f"  retired    {', '.join(spec['naming']['legacy_names'])}")
    print(f"  declared   backend       {spec['backend']}  (source alias {spec['source_alias']})")
    print(f"             context       {spec['context_window']}   max tokens {spec['max_tokens']}   "
          f"thinking {spec['thinking'] or 'off'}" + (f", start {spec['thinking_start']}" if spec['thinking'] else ''))
    print(f"             quantization  {spec['naming']['quantization']} ({spec['naming']['quantization_source']})")
    print(f"             engine        {engine['repository'].rsplit('/', 1)[-1]} {engine['version']} "
          f"@ {engine['revision'][:12]}")
    print(f"             weights       {weights['repository']} @ {weights['revision'][:12]}, "
          f"{weights['files']} files, {weights['sha256']} pinned by sha256, {round(weights['bytes'] / 2 ** 30, 1)} GiB")
    print(f"             class_id      {identity['class_id']}")
    print(f"             config_id     {identity['config_id']}")
    for field, why in sorted(identity['unknowns'].items()):
        print(f'             not declared  {field}: {why}')
    print(f"  state      hardware      {state['gpu']}, driver {state['driver']}, "
          f"{state['memory_total_gib']} GiB, hw_fp {state['hw_fp']}")
    print(f"             read          {state['collected_at'] or 'never'}"
          + (f" ({round(state['age_s'] / 3600)} h ago)" if state['age_s'] is not None else ''))
    print(f"             observed      {state['observed_versions_why']}")
    if measured.get('why'):
        print(f"  evidence     {measured['why']}")
    elif not measured['rows']:
        print(f'  evidence     no measurement of this configuration; {cli_name()} eval suite run --alias '
              f"local-coder-{row['target']}")
    else:
        print(f"  evidence     {measured['usable']} of {measured['rows']} rows may decide a ranking")
        for name, metric in measured['metrics'].items():
            interval = '-' if metric['low'] is None else f"{metric['low']}-{metric['high']}"
            print(f"             {name:<20} {metric['value']:>10}  n={metric['n']:<6} {interval:<18} "
                  f"{metric['measured_at']}")
        for reason in measured['excluded']:
            print(f'             excluded: {reason}')
    return 0


def model_compatibility(args):
    """docs/compatibility.md, generated from the declared files. Print it, or write it with --write."""
    text = catalog.compatibility(ROOT)
    if not args.write:
        print(text)
        return 0
    target = ROOT / 'docs/compatibility.md'
    changed = not target.exists() or target.read_text() != text
    target.write_text(text)
    print(f"{'wrote' if changed else 'unchanged'} {target.relative_to(ROOT)}")
    return 0


def _storage_readings():
    """Free space per node from `ops storage` (read-only, over SSH). No answer is a gap, not a failure."""
    sys.path.insert(0, str(CODE / 'scripts'))
    try:
        import storage
        return {node['host']: node for node in storage.report()}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'ops storage did not answer ({error}); the disk gate reports unknown instead of passing',
              file=sys.stderr)
        return {}


def model_recommend(args):
    """PA §9.3: filter and rank, print every reason, and stop. Nothing is downloaded or re-routed."""
    try:
        report = catalog.recommend(ROOT, node=args.node, task=args.task, context=args.context,
                                   storage=None if args.no_storage else _storage_readings(),
                                   current=_route_now())
    except ValueError as error:
        raise SystemExit(str(error))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    incumbent = report['incumbent']
    print(f"local-coder is served by {incumbent['target']} on {incumbent['node']} "
          f"(config {incumbent['config_id'][:8]}, {incumbent['context_window']} context)")
    print(f"task {report['task']}; a candidate must serve at least {report['required_context']} context")
    print(f'rule (Roadmap Phase 5): {report["rule"]}\n')
    for entry in report['candidates']:
        mark = '*' if entry['incumbent'] else ('-' if entry['excluded'] else ' ')
        verdict = 'the incumbent' if entry['incumbent'] else ('excluded' if entry['excluded'] else 'compared')
        print(f"{mark} {entry['target']:<36} {entry['node']:<6} {verdict}")
        for check in entry['filters']:
            if check['status'] != 'ok' or args.explain:
                print(f"      {check['status']:<9} {check['check']:<9} {check['reason'] or ''}")
        comparison = entry['comparison']
        if comparison and (args.explain or comparison['passes']):
            for name, condition in comparison['conditions'].items():
                print(f"      {'met' if condition['ok'] else 'not met':<9} {name:<23} "
                      f"candidate {condition['candidate']}  incumbent {condition['incumbent']}"
                      + (f"  bound {condition['bound']}" if 'bound' in condition else ''))
            if comparison['state'] == 'compared':
                print(f"      compared on {len(comparison['shared_tasks'])} tasks both sides ran; "
                      f"not run by the candidate: {', '.join(comparison['incumbent_only']) or 'none'}")
            print(f'      {comparison["note"]}')
    meaning = {'incumbent': 'keep the current route: nothing that survived the filter met the promotion rule',
               'candidate': 'a candidate met the promotion rule; switching the route is still an operator '
                            f'action ({cli_name()} route TARGET, after a tested rollback)',
               'insufficient-evidence': 'the candidates that survived the filter cannot be compared on the '
                                        f'records; measure them with {cli_name()} eval suite run'}[report['conclusion']]
    print(f"\nconclusion: {report['conclusion']} — {meaning}")
    return 0


def git(path, *argv):
    return subprocess.run(['git', '-C', str(path), *argv], capture_output=True, text=True)


def worktree_info(path):
    path = Path(path)
    branch = git(path, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
    main = Path(git(path, 'worktree', 'list', '--porcelain').stdout.splitlines()[0].split(' ', 1)[1])
    base = git(main, 'rev-parse', 'HEAD').stdout.strip()
    ahead = int(git(path, 'rev-list', '--count', f'{base}..HEAD').stdout.strip() or 0)
    dirty = len(git(path, 'status', '--porcelain').stdout.splitlines())
    hand_made = not path.resolve().is_relative_to(WORKTREES.resolve())
    return {'name': path.name, 'path': path, 'branch': branch, 'repo': main, 'commits': ahead,
            'uncommitted': dirty, 'hand_made': hand_made}


def merged_state(info, asked):
    """Merged as `worktrees prune` judges it: the tip is in origin/main, or is the head of a merged pull request
    (a squash merge). ASKED caches origin/main and GitHub's answer per repository."""
    repo = info['repo']
    if repo not in asked:
        base = git(repo, 'rev-parse', '--verify', '--quiet', 'origin/main').stdout.strip()
        asked[repo] = (base, hygiene.merged_heads(repo) if base else None)
    base, heads = asked[repo]
    if not base:
        return 'merge state unknown (no origin/main)'
    tip = git(info['path'], 'rev-parse', 'HEAD').stdout.strip()
    if hygiene.merged_how(repo, tip, base, heads):
        return 'merged'
    return 'not merged' if heads is not None else 'not in origin/main (GitHub not asked)'


def worktrees_prune(args):
    """Merged agent/ branches and their worktrees: listed by default, removed with --apply."""
    report = hygiene.survey(ROOT, hygiene.merged_heads(ROOT))
    print(hygiene.screen(report))
    if not args.apply:
        if report['remove']:
            print(f'nothing was removed; to remove the above: {cli_name()} worktrees prune --apply')
        return 0
    try:
        for line in hygiene.apply(ROOT, report):
            print(line)
    except RuntimeError as error:
        raise SystemExit(f'stopped: {error}')
    return 0


def worktrees(args):
    if args.action == 'prune':
        return worktrees_prune(args)
    found = sorted(p for p in WORKTREES.iterdir() if (p / '.git').exists()) if WORKTREES.is_dir() else []
    if args.action == 'list':
        # Session worktrees of any repository (agent --repo), then this repository's other agent/ worktrees.
        listed = {path.resolve(): path for path in found}
        for tree in hygiene.worktrees(ROOT)[1:]:
            if (tree['branch'] or '').startswith(hygiene.PREFIX) and not tree['prunable'] and tree['path'].exists():
                listed.setdefault(tree['path'].resolve(), tree['path'])
        if not listed:
            print(f'no agent worktrees in {WORKTREES} or {ROOT}')
        asked = {}
        for info in sorted(map(worktree_info, listed.values()), key=lambda i: (i['hand_made'], str(i['path']))):
            state = merged_state(info, asked)
            kind = ', made by hand' if info['hand_made'] else ''
            print(f"{str(info['path']):<36} {info['branch']}  {info['commits']} commits, "
                  f"{info['uncommitted']} uncommitted files, {state}{kind}  "
                  f"(git -C {info['repo']} diff HEAD...{info['branch']})")
        return 0
    if args.action in ('review', 'apply') and args.name is None:
        sys.path.insert(0, str(CODE / 'scripts'))
        import agent
        _, path = agent.latest_session(agent.STATE)
    else:
        path = WORKTREES / args.name
    if path not in found:
        raise SystemExit(f'{path} is not an agent worktree in {WORKTREES}; see {cli_name()} worktrees')
    info = worktree_info(path)
    if args.action in ('review', 'apply'):
        if not info['branch'].startswith('agent/'):
            raise SystemExit('worktree must be on an agent/ branch')
        main = info['repo']
        current = git(main, 'symbolic-ref', '--quiet', '--short', 'HEAD')
        if current.returncode:
            raise SystemExit(f'{main} is not on a branch')
        print(f"{path} ({info['branch']}) -> {main} ({current.stdout.strip()})")
        if args.action == 'review':
            for cmd in (('log', '--oneline', f"HEAD..{info['branch']}"),
                        ('diff', *([] if args.diff else ['--stat']), f"HEAD...{info['branch']}")):
                result = git(main, *cmd)
                if result.returncode:
                    raise SystemExit(result.stderr.strip())
                print(result.stdout, end='')
            if info['uncommitted']:
                print(f"worktree has {info['uncommitted']} uncommitted files; commit or resolve them before applying")
            return 0
        if info['uncommitted']:
            raise SystemExit('worktree has uncommitted files; commit or resolve them before applying')
        target_status = git(main, 'status', '--porcelain', '--untracked-files=no')
        if target_status.returncode or target_status.stdout.strip():
            raise SystemExit('destination has tracked changes; commit or resolve them before applying')
        ancestry = git(main, 'merge-base', '--is-ancestor', 'HEAD', info['branch'])
        if ancestry.returncode:
            raise SystemExit('destination and worktree have diverged; review and integrate manually')
        result = git(main, 'merge', '--ff-only', info['branch'])
        if result.returncode:
            raise SystemExit(result.stderr.strip() or result.stdout.strip())
        print(result.stdout, end='')
        return 0
    if (info['commits'] or info['uncommitted']) and not args.force:
        raise SystemExit(f"{args.name} has {info['commits']} commits not in {info['repo'].name}'s HEAD and "
                         f"{info['uncommitted']} uncommitted files; merge them or add --force")
    for argv in (['worktree', 'remove', *['--force'] * args.force, str(path)], ['branch', '-D', info['branch']]):
        result = git(info['repo'], *argv)
        if result.returncode:
            raise SystemExit(result.stderr.strip())
    print(f"removed {path} and branch {info['branch']}")
    return 0


def completion(args):
    """Bash completion generated from the parser, so it never falls behind the commands."""
    words, values, files = {}, {}, set()
    hidden = legacy_names()

    def offered(action):
        return [c for c in action.choices if not (getattr(action, 'offer_node_first', False) and c in hidden)]

    def walk(parser, key):
        entries = []
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    entries.append(name)
                    walk(child, f'{key} {name}')
            elif isinstance(action, argparse._HelpAction):
                continue
            elif action.option_strings:
                entries += [o for o in action.option_strings if o.startswith('--') or o == '-m']
                if action.choices:
                    values.update({f'{key} {o}': ' '.join(offered(action)) for o in action.option_strings})
            elif action.choices:
                entries += offered(action)
            elif action.nargs != argparse.REMAINDER:
                files.add(key)
        words[key] = ' '.join(dict.fromkeys(entries))  # positionals can share choices

    walk(build_parser(former=False), 'agentiac')  # bash arrays take no empty key
    table = ''.join(f'  [{json.dumps(k)}]={json.dumps(v)}\n' for k, v in sorted({**words, **values}.items()))
    print(f'''# agentiac bash completion (agentiac completion)
declare -gA _agentiac_words=(
{table})
_agentiac_files=" {' '.join(sorted(k.replace(' ', '_') for k in files))} "
_agentiac() {{
  local cur=${{COMP_WORDS[COMP_CWORD]}} prev=${{COMP_WORDS[COMP_CWORD-1]}} key=agentiac w
  for w in "${{COMP_WORDS[@]:1:COMP_CWORD-1}}"; do
    [[ -n ${{_agentiac_words["$key $w"]+x}} && $w != -* ]] && key="$key $w"
  done
  if [[ -n ${{_agentiac_words["$key $prev"]+x}} && $prev == -* ]]; then
    COMPREPLY=($(compgen -W "${{_agentiac_words["$key $prev"]}}" -- "$cur")); return
  fi
  if [[ $cur != -* && $_agentiac_files == *" ${{key// /_}} "* ]]; then COMPREPLY=(); return; fi
  COMPREPLY=($(compgen -W "${{_agentiac_words[$key]}}" -- "$cur"))
}}
complete -o default -F _agentiac agentiac''')
    return 0


def agent(args):
    """agentiac agent: what is pinned, and what has actually been shown (PA §12.2-12.3, T12.4).

    The version and the matrix come from the machine, not from this file: an Agent that is not pinned
    says `not pinned`, and a capability nobody probed at this version says `unverified`.
    """
    command = getattr(args, 'agent_command', None) or 'list'
    if command == 'probe':
        return agent_probe(args)
    show = command == 'capabilities' or getattr(args, 'capabilities', False)
    ids = ([args.agent_id] if getattr(args, 'agent_id', None) else sorted(adapters.ADAPTERS))
    rows = services.agents.rows(ROOT, ids=ids, show=show)
    if getattr(args, 'json', False):
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    for row in rows:
        mark = 'pinned  ' if row['version'] else 'NOT     '
        print(f"{row['id']:<16} {mark}{row['version'] or row['note']:<28} "
              f"{'gateway-only' if row['gateway_only'] else 'ANY HOST   '} {','.join(row['allowed_paths'])}"
              + ('  (baseline)' if row['baseline'] else ''))
        print(f"{'':<16} {row['install']}")
        for feature in adapters.CAPABILITIES:
            if feature not in row.get('capabilities', {}):
                continue
            entry = row['capabilities'][feature]
            print(f"{'':<19} {feature:<28} {entry['value']:<11} {entry['reason']}")
    if not any(row['version'] for row in rows):
        print('nothing is pinned; a capability claim without a version is not evidence (U4)')
    return 0


def agent_probe(args):
    """Run one probe in the suite sandbox and record what it showed. Nothing is claimed without this."""
    sys.path.insert(0, str(CODE / 'scripts'))
    from agentiac import suite as suite_settings
    from agentiac.secrets import read_key, redact
    chosen = adapters.adapter(args.agent_id)
    spec = {'alias': args.alias, 'model': chosen.model_for(args.alias)}
    folder = ROOT / 'artifacts/agents/probes' / f'{args.agent_id}-{args.feature}-{time.strftime("%Y%m%d-%H%M%S")}'
    (folder / 'workspace').mkdir(parents=True)
    (folder / 'workspace/probe.txt').write_text('probe token: green\n')
    chosen.prepare(spec, folder / 'agent-state')
    image = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', suite_settings.IMAGE],
                                    text=True).strip()
    request = adapters.RunRequest(args.feature, {'alias': args.alias, 'model': spec['model'], 'image': image,
                                                 'timeout_s': 600},
                                  {'issue': ''}, folder, read_key(ROOT / 'secrets/gateway.env', 'LITELLM_TASK_KEY'))

    def run(argv, env, timeout_s):
        completed = subprocess.run(list(argv), env=dict(os.environ, **env), capture_output=True, text=True,
                                   timeout=timeout_s)
        # A verdict is one line and the trace is what explains it, so the trace stays with the evidence.
        (folder / 'trace.jsonl').write_text(redact(completed.stdout, list(env.values())))
        (folder / 'stderr.txt').write_text(redact(completed.stderr, list(env.values())))
        return completed.stdout, completed.stderr, completed.returncode

    try:
        outcome = adapters.probe(chosen, args.feature, request, run)
    except (adapters.AdapterError, subprocess.SubprocessError) as error:
        raise SystemExit(f'{args.agent_id} {args.feature}: {error}')
    (folder / 'probe.json').write_text(json.dumps(outcome, indent=1) + '\n')
    print(json.dumps(outcome))
    print(f'evidence kept in {folder.relative_to(ROOT)}', file=sys.stderr)
    return 0


def evidence(args):
    """agentiac evidence: the derived DB of PA §9.2 (T10.4).

    `import` rebuilds it from the primary records (the suite DB, the ladder runs, the
    transcribed values); `show` reads it. Nothing here is authored: the only hand-written
    inputs are `evidence/alias-history.yml` and `evidence/reports.yml`, and both are refused
    before a row is written when they would bind a measurement to an id that does not name
    its weights.
    """
    action = getattr(args, 'action', None) or 'show'
    if action == 'import':
        summary, rows = evidence_module.import_evidence(ROOT, write=not args.dry_run)
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0
        where = ' (nothing written)' if args.dry_run else f' {evidence_module.database(ROOT).relative_to(ROOT)}'
        print(f"{summary['rows']} rows{where}: {summary['usable']} may rank, "
              f"{summary['unknown']} identity unknown, {summary['stale']} stale")
        for source in summary['sources']:
            skipped = ', '.join(f'{count} {why}' for why, count in sorted(source['skipped'].items())) or 'none'
            print(f"  {source['source']:<7} {source['ref']:<34} {source['cases']} measurements, "
                  f"not measurements: {skipped}")
        for metric, count in sorted(summary['by_metric'].items()):
            print(f'  {metric:<22} {count}')
        return 0
    try:
        rows = evidence_module.query(ROOT, node=args.node, target=args.target, agent=args.agent,
                                     metric=args.metric, workload=args.workload, confidence=args.confidence,
                                     usable_only=args.usable, stale=True if args.stale else None)
    except FileNotFoundError as error:
        raise SystemExit(f'{error}')
    if getattr(args, 'json', False):
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    if not rows:
        print(f'no rows; {cli_name()} evidence import, or widen the filters')
        return 0
    print(f"{'workload':<26} {'metric':<21} {'config':<9} {'node':<6} {'agent':<5} "
          f"{'value':>10} {'n':>5} {'95% / range':<15} {'measured':<20} why not usable")
    for row in rows:
        interval = '-' if row['ci_low'] is None else f"{row['ci_low']}-{row['ci_high']}"
        print(f"{row['workload']:<26} {row['metric']:<21} {(row['config_id'] or '')[:8]:<9} "
              f"{(row['node'] or '-'):<6} {row['agent']:<5} {(row['value'] if row['value'] is not None else float('nan')):>10} "
              f"{str(row['n'] or ''):>5} {interval:<15} {row['measured_at']:<20} "
              f'{evidence_module.unusable_reason(row) or ""}')
    usable = sum(1 for row in rows if evidence_module.usable(row))
    print(f'\n{usable}/{len(rows)} of these rows may decide a ranking (PA §9.2: no keys, no rank; '
          f'published never ranks; stale waits for a decision to re-measure)')
    return 0


def log_task(args):
    """agentiac log-task: one JSONL line of real usage (Roadmap Phase 3, T8.7).

    The line is the whole product — repository, type, outcome, minutes, failure category — and it holds
    no prompt, no diff and no path. PA L8's complaint about the manual equivalent is that it drifted
    into prose, so the fields are declared and the rest is refused. Counting the lines is T13.2's job,
    where this log is read together with the suite, the gateway metadata and the router logs.
    """
    fields = dict(repository=args.repo, type=args.type, outcome=args.outcome, minutes=args.minutes,
                  failure=args.failure, agent=args.agent, target=args.target)
    try:
        line = tasklog.build(ROOT, **fields) if args.dry_run else tasklog.record(ROOT, **fields)
    except ValueError as problem:
        print(f'log-task: {problem}', file=sys.stderr)
        return 2                                        # PA §13: 2 is a misuse of the command
    if args.json:
        print(json.dumps(line, sort_keys=True, ensure_ascii=False))
        return 0
    where = ' (nothing written)' if args.dry_run else f'  {tasklog.PATH}'
    print(f'{line["time"]}  {line["repository"]}  {line["type"]}  {line["outcome"]}'
          f'{" (" + line["failure"] + ")" if line["failure"] else ""}  {line["minutes"]} min  '
          f'{line["agent"]} on {line["target"] or "unknown"}{where}')
    return 0


def observe(args):
    """agentiac observe: the observation side of PA §14 in one screen (T13.2).

    Five sources read together, because the question that starts a proposal is always cross-source.
    Each contributes only its declared keys — no prompt, no response, no diff — and each says whether
    it could be read at all, what keeps it, and how old its oldest line is.
    """
    sources = observe_module.SOURCES if not args.source else \
        tuple(source for source in observe_module.SOURCES if source.source in args.source)
    quiet = (lambda unit, since: (None, 0, [], 0, {})) if args.no_gateway else observe_module.read_journal
    since = None
    if args.days is not None:
        since = (datetime.date.today() - datetime.timedelta(days=args.days)).isoformat()
    report = observe_module.summary(ROOT, sources=sources, journal=quiet, since=since)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else observe_module.markdown(report))
    return 0


def sessions(args):
    """agentiac sessions: the inventory of routine work, counted out of pi's transcripts (T11.1).

    The transcripts hold every prompt and reply; this prints counts. The only strings it can show are
    tool names, program names and subcommands, each checked against a list declared in `sessions.py`.
    """
    if args.days is not None and args.days < 1:
        print('sessions: --days wants a positive number of days', file=sys.stderr)
        return 2                                        # PA §13: 2 is a misuse of the command
    if args.top < 1:
        print('sessions: --top wants a positive number of rows', file=sys.stderr)
        return 2
    report = sessions_module.summary(ROOT, days=args.days, top=args.top)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else sessions_module.markdown(report))
    return 0


NATIVE = {'status': status, 'doctor': doctor, 'models': models, 'model': model, 'worktrees': worktrees, 'completion': completion,
          'agent': agent, 'evidence': evidence, 'log-task': log_task, 'observe': observe, 'sessions': sessions}


def build_parser(former=True):
    workers = inventory()
    # a former name (inventory former_names) is accepted wherever a host is, and main() names the current host;
    # completion is built with former=False, so it suggests only current names
    hosts = list(workers) + (list(declared.former_names(workers)) if former else [])
    parser = argparse.ArgumentParser(prog=cli_name(), description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', metavar='COMMAND')

    p = sub.add_parser('code', help='start pi in a new worktree of REPO',
                       description='Start pi on a gateway alias in a new git worktree of REPO '
                                   '(~/agent-worktrees/<repo>-<time>, branch agent/<time>); '
                                   'pi arguments go after --.')
    p.add_argument('repo', nargs='?', default='.', help='git repository (default: this directory)')
    offer_node_first(p.add_argument('-m', '--model', default='local-coder', choices=code_aliases(), metavar='ALIAS',
                   help=f'alias (default local-coder; router = rule-based escalation; see {cli_name()} models)'))
    p.add_argument('-p', '--prompt', help='run this one prompt and exit (pi print mode)')
    p.add_argument('--allow-cloud', action='store_true', help='router: escalate without asking (allowed repositories only)')
    p.add_argument('--in-place', action='store_true', help='edit REPO directly instead of a new worktree')
    p.add_argument('--resume', action='store_true', help='resume the most recently modified pi session in its saved worktree')
    p.add_argument('--loop-guard', action='store_true', help='stop pi after repeated near-identical bash commands')
    p.add_argument('--thinking', choices=['off', 'minimal', 'low', 'medium', 'high'],
                   help="starting thinking level (default: the target's thinking_start, Spark and Gyro-M medium, else off; "
                        'only targets with a thinking switch)')

    p = sub.add_parser('worktrees', help='list, review, apply, drop or prune agent worktrees')
    w = p.add_subparsers(dest='action')
    w.add_parser('list', help='worktrees with their commits and uncommitted files (default)')
    for action in ('review', 'apply'):
        q = w.add_parser(action, help=('summarize changes before applying' if action == 'review' else
                                       'fast-forward the original repository to an agent worktree'))
        q.add_argument('name', nargs='?', help='worktree name (default: latest pi session worktree)')
        if action == 'review':
            q.add_argument('--diff', action='store_true', help='show the complete committed diff')
    d = w.add_parser('drop', help='remove a worktree and its agent/ branch')
    d.add_argument('name')
    d.add_argument('--force', action='store_true', help='also when it has commits or changes not merged')
    q = w.add_parser('prune', help='every merged agent/ branch and its worktree, squash merges included '
                                   '(asks GitHub); lists them unless --apply')
    q.add_argument('--apply', action='store_true', help='remove what the listing shows')

    sub.add_parser('models', help='aliases usable with code -m')
    p = sub.add_parser('model', help='the local-coder targets: their ids, one screen of declaration, state '
                                     'and evidence, and a recommendation (PA §6, §9.2, §9.3)')
    offer_node_first(p.add_argument('verb', nargs='?', choices=[*catalog.VERBS, *route_aliases.selectors(routes())],
                   metavar='{list,show,names,compatibility,recommend,TARGET}',
                   help='names resolves descriptive names and aliases; list or show reads the screen; compatibility renders docs/compatibility.md; '
                        'recommend filters and ranks; a TARGET name is the T10.2 id view'))
    offer_node_first(p.add_argument('target', nargs='?', choices=route_aliases.selectors(routes()), help='with show'))
    p.add_argument('--node', choices=hosts, help='recommend: only candidates served on this node')
    p.add_argument('--task', default='coding', choices=list(catalog.TASKS),
                   help='recommend: the task to rank on (only coding has measurements)')
    p.add_argument('--context', type=int, metavar='N',
                   help='recommend: the context a candidate must serve (default: what the route gives today)')
    p.add_argument('--explain', action='store_true', help='recommend: also print the checks that passed')
    p.add_argument('--no-storage', action='store_true',
                   help='recommend: do not read free space over SSH; the disk gate then reports unknown')
    p.add_argument('--write', action='store_true', help='names or compatibility: write the generated documentation')
    p.add_argument('--json', action='store_true', help='the full record instead of the screen')
    sub.add_parser('status', help='gateway, hosts, local-coder target and ChatGPT sign-in').add_argument(
        '--json', action='store_true', help='one JSON document instead of the table')
    p = sub.add_parser('doctor', help='read everything a session start needs at once; loads no model')
    p.add_argument('--json', action='store_true', help='one JSON document instead of the table')
    p.add_argument('--snapshot', action='store_true',
                   help='also write the --json document to artifacts/closures/ and print its path on stderr')

    p = sub.add_parser('warm', help='load a model now (default: the local-coder target)')
    p.add_argument('host', nargs='?', choices=hosts)
    p = sub.add_parser('unload', help='unload a model now; the next request loads it again')
    p.add_argument('host', choices=hosts)
    p = sub.add_parser('claim', help='serve a borrowed host on demand (or change a dedicated host\'s settings)')
    p.add_argument('host', choices=hosts)
    p.add_argument('--route', action='store_true', help='also point local-coder at it (restarts the gateway)')
    p.add_argument('--idle', type=float, metavar='MIN', help='idle minutes before unload (0 = never)')
    p.add_argument('--no-warm', action='store_true', help='load on the first request instead of now')
    p.add_argument('--backend', metavar='ID', help='Strata host: serve backends/ID.json from the root its engine version has there')
    p.add_argument('--context', type=int, metavar='N', help="with --backend: served context (default: the manifest's)")
    p.add_argument('--engine-option', action='append', metavar='NAME=VALUE',
                   help='with --backend: claim the host with this engine option added (experts=6); only names the '
                        'manifest lists in engine_flags, and the start state records what was added')
    sub.add_parser('reclaim', help='after a reboot: claim the current local-coder host again, keeping the route')
    p = sub.add_parser('release', help='give a borrowed host back: local-coder to the default declared in '
                                      'gateway/local-coder.json, stop everything of ours')
    p.add_argument('host', choices=[h for h in hosts if workers[declared.current_host(h, workers)].get('borrowed')])
    p = sub.add_parser('route', help='show, or switch, the local-coder target (switch restarts the gateway)')
    offer_node_first(p.add_argument('target', nargs='?', choices=route_aliases.selectors(routes())))
    p = sub.add_parser('chatgpt', help='ChatGPT Plus sign-in for cloud-gpt-sol')
    p.add_argument('action', choices=('login', 'status', 'logout'))
    p = sub.add_parser('check', help='protocol checks and a short pi fix against an alias')
    offer_node_first(p.add_argument('-m', '--model', default='local-coder', choices=gateway_aliases(), metavar='ALIAS'))
    p.add_argument('--no-pi', action='store_true', help='protocol checks only')

    p = sub.add_parser('ops', help='operations: build, download, start, stop, deploy, logs, ...')
    o = p.add_subparsers(dest='action', required=True, metavar='ACTION')
    for name, text in (('build', 'build the pinned backend'), ('runtime', 'build the runtime image'),
                       ('stop', 'stop project inference and the on-demand front')):
        o.add_parser(name, help=text).add_argument('host', choices=hosts)
    q = o.add_parser('node-config', help='make node.json say what infra/inventory.yml declares, on a host that '
                                         'serves no profile (T5.11). Starts nothing; corrects the file, never '
                                         'creates one')
    q.add_argument('host', choices=hosts)
    q.add_argument('--check', action='store_true', help='show what would change; change nothing')
    for name, text in (('download', 'download verified weights onto the worker'),
                       ('start', 'serve a profile always on (replaces the on-demand front)')):
        q = o.add_parser(name, help=text)
        q.add_argument('host', choices=hosts)
        q.add_argument('--model', metavar='PROFILE', help="profiles/<PROFILE>.json (default: the host's default_profile)")
    q = o.add_parser('deploy', help='install the control gateway, tunnels and watchdog')
    q.add_argument('--check', action='store_true', help='show what would change; change nothing')
    q.add_argument('--only', choices=('tunnels', 'gateway', 'chatgpt', 'watch'), help='deploy one part')
    q.add_argument('--allow-dirty', action='store_true', help='deploy with uncommitted changes in this checkout')
    q = o.add_parser('prepare', help="prepare a pinned backend: Spark's TensorFold, or a Strata --backend ID")
    q.add_argument('host', choices=hosts)
    q.add_argument('--backend', metavar='ID', help='Strata: backends/ID.json')
    q.add_argument('--reuse-root', metavar='PATH', help='Strata: prepared root whose identical files may be copied')
    q.add_argument('--reuse-declared', action='store_true',
                   help='Strata: take the reuse root this checkout declares (infra/retention.yml, form: full, '
                        'identical pinned bytes); refuses when there is more than one or none')
    q.add_argument('--download', action='store_true',
                   help='Strata: mean the download, even when this host already holds the pinned bytes')
    q.add_argument('--isolated', action='store_true', help='Strata: new isolated root')
    q.add_argument('--check', action='store_true', help='show what would change; change nothing')
    q = o.add_parser('upgrade', help='read an engine upgrade before declaring it: what moves, and which prepared '
                                     'root the weights can be copied from (T5.8, S §9 F12)')
    u = q.add_subparsers(dest='upgrade_action', required=True)
    for name, text in (('plan', 'the ordered reading of an upgrade on HOST: declarations, blockers, steps'),
                       ('reuse', 'which full root on HOST already pins the bytes this backend pins')):
        p = u.add_parser(name, help=text)
        p.add_argument('host', choices=hosts)
        p.add_argument('--backend', required=True, metavar='ID', help='backends/ID.json')
        p.add_argument('--json', action='store_true', help='one JSON document instead of the lines')
    o.add_parser('drift', help='report where the live system differs from this checkout').add_argument(
        '--json', action='store_true', help='one JSON document instead of the lines')
    q = o.add_parser('eval-image', help='the benchmark and suite images against what they are built from (IP-13)')
    q.add_argument('what', nargs='?', choices=('status', 'hash', 'build', 'pin-suite'),
                   help='status: report what is known (default); hash: print the Dockerfile hash; build: rebuild, '
                        'labelled; pin-suite: lock the suite image and its build inputs')
    q.add_argument('--tag', help='image to check instead of $AGENTIAC_EVAL_IMAGE or <resource_prefix>-eval:baseline')
    q = o.add_parser('storage', help='free space, room for one more root, what is not registered, and what the '
                                     'ledger marks goable')
    q.add_argument('hosts', nargs='*', choices=hosts,
                   help='nodes to measure (default: every node with a declared root)')
    q.add_argument('--json', action='store_true', help='one JSON document instead of the lines')
    q.add_argument('--candidates', action='store_true',
                   help='list what infra/retention.yml marks `retirement: candidate`, with its size, its last read '
                        'and what still names it (PA §11). Reads only')
    q = o.add_parser('retire', help='the two stages of a retirement: isolate what the ledger marks goable, then '
                                    'delete what has been isolated (P7 and P8)')
    q.add_argument('host', choices=hosts)
    named = q.add_mutually_exclusive_group(required=True)
    named.add_argument('--backend', help='the ledger line naming a backend manifest (infra/retention.yml)')
    named.add_argument('--name', help='the ledger line naming a thing with no manifest: an isolated path')
    named.add_argument('--image', help='the ledger line naming an image (T5.9): read what points at the tag and '
                                       'report the `docker rmi`. --check is the only stage an image has (§3.3)')
    stage = q.add_mutually_exclusive_group()
    stage.add_argument('--check', action='store_true', help='every §4.3 gate and the inode-aware reclaim, '
                                                            'changing nothing (default)')
    stage.add_argument('--quarantine', action='store_true', help='save the metadata, then move the root into the '
                                                                 'staging directory the ledger names (§5.5). '
                                                                 'Reversible')
    stage.add_argument('--purge', action='store_true', help='delete a path a quarantine record says is already '
                                                            'isolated, after 24 h of that. **IRREVERSIBLE** (§5.6)')
    q.add_argument('--shorten-wait', metavar='REASON', help='with --purge: record that the 24 h isolation was '
                                                            'shortened, and why, instead of skipping it')
    q.add_argument('--json', action='store_true', help='one JSON document instead of the gate report')
    q = o.add_parser('thin', help='move the pinned weights out of a rollback root, keeping the engine, the packs '
                                  'and the MTP layer (Phase 5; needs §3.2 recorded in the ledger)')
    q.add_argument('host', choices=hosts)
    q.add_argument('--backend', required=True, help='the rollback generation to thin (infra/retention.yml)')
    q.add_argument('--check', action='store_true', help='every gate plus the worker\'s plan; changes nothing')
    q.add_argument('--json', action='store_true', help='one JSON document instead of the gate report')
    q = o.add_parser('backup', help='the control backup (IP-15): what it covers, run it, verify a restore')
    b = q.add_subparsers(dest='backup_action', required=True, metavar='ACTION')
    b.add_parser('plan', help='what the set covers today; writes nothing and reaches no host')
    b.add_parser('run', help='build the archive, upload it, prune to the retention rule, record it')
    b.add_parser('verify', help='download the last archive back, compare its hash, re-run the aggregation')
    b.add_parser('prune', help='apply the retention rule to the remote only')
    b.add_parser('status', help='what was last backed up, and how old that is')
    q.add_argument('--destination', default=backup.DESTINATION, help='rclone remote and path')
    q.add_argument('--keep', type=int, default=4, help='weekly archives kept, newest first')
    q.add_argument('--dry-run', action='store_true', help='build the archive and stop before uploading')
    q = o.add_parser('recover', help='re-claim the host the route points at, and never move the route '
                                     '(recovery-only)')
    r = q.add_subparsers(dest='recover_action', required=True, metavar='ACTION')
    p = r.add_parser('run', help='decide, and re-claim that host when every gate passes (what the timer runs)')
    p.add_argument('--check', action='store_true', help='report the decision; act nothing and write nothing')
    p.add_argument('--json', action='store_true', help='one JSON document instead of the lines')
    p = r.add_parser('status', help='what the last run decided, plus the pause and the failure counter')
    p.add_argument('--json', action='store_true', help='one JSON document instead of the lines')
    p = r.add_parser('pause', help='stand down for a while: before ops stop, a release or hands-on repair')
    p.add_argument('minutes', type=float, metavar='MIN', help='how long to stand down')
    p.add_argument('--reason', default='', help='what the timer is being kept off (an ops stop, a release, a repair)')
    p.add_argument('--json', action='store_true', help='one JSON document instead of the lines')
    p = r.add_parser('resume', help='end the pause now')
    p.add_argument('--json', action='store_true', help='one JSON document instead of the lines')
    p = r.add_parser('reset', help='clear the failure counter after a person has looked at the host')
    p.add_argument('--json', action='store_true', help='one JSON document instead of the lines')
    q = o.add_parser('restore', help='put a thin N-1 root back to full (HOST --backend ID), or restore control '
                                     'from the backup archive (--into PATH) and stop at the human steps')
    q.add_argument('host', nargs='?', choices=hosts,
                   help='the node whose rollback root is being filled again; needs --backend ID')
    q.add_argument('--backend', help='with HOST: the rollback generation (infra/retention.yml, `form: thin`)')
    q.add_argument('--check', action='store_true', help='with HOST: every gate plus the worker\'s plan; changes '
                                                        'nothing')
    q.add_argument('--json', action='store_true', help='with HOST: one JSON document instead of the gate report')
    q.add_argument('--into', metavar='PATH', help='an agentiac checkout with no records of its own '
                                                  '(the control restore)')
    q.add_argument('--archive', help='a remote archive the backup record does not name')
    q.add_argument('--sha256', help='its digest, required with --archive')
    q.add_argument('--destination', default=None, help='rclone remote and path (default: what the record says)')
    q.add_argument('--dry-run', action='store_true', help='print what would happen and change nothing')
    q = o.add_parser('update', help='what upstream offers for infra/components.yml; applies nothing')
    u = q.add_subparsers(dest='update_action', required=True, metavar='ACTION')
    q = u.add_parser('check', help='read upstream versions and audit the locks; keep the last answer if a read fails')
    q.add_argument('--json', action='store_true', help='one JSON document instead of the table')
    q.add_argument('--notify', action='store_true', help='tell the desktop about a new version of a `notify` component')
    u.add_parser('observe', help='read what the machines run; loads no model and writes nothing to a worker').add_argument(
        '--json', action='store_true', help='one JSON document instead of the table')
    q = u.add_parser('plan', help='print what updating one component would take; changes nothing')
    q.add_argument('id', metavar='ID', help='a component in infra/components.yml')
    q.add_argument('--json', action='store_true', help='one JSON document instead of the lines')
    q = o.add_parser('preflight', help='check what a host needs before anything runs there')
    q.add_argument('hosts', nargs='+', choices=hosts)
    q.add_argument('--json', action='store_true', help='one JSON document instead of one line per host')
    o.add_parser('netwatch', help='install the Wi-Fi watch').add_argument('hosts', nargs='+', choices=hosts)
    o.add_parser('netwatch-report', help='Wi-Fi outages and reconnects').add_argument('host', choices=hosts)
    q = o.add_parser('backend-probe', help='bounded probe of a reviewed backend manifest')
    q.add_argument('host', choices=hosts)
    q.add_argument('backend')
    q = o.add_parser('logs', help='latest gateway log lines (metadata only)')
    q.add_argument('--chatgpt', action='store_true', help='the ChatGPT gateway instead of the main one')
    q.add_argument('-n', '--lines', type=int, default=30)
    o.add_parser('hosts', help='build, download and service units on every worker')
    o.add_parser('inventory', help='collect hardware inventory into artifacts/inventory')
    q = o.add_parser('metrics', help='sample RAM, load and backend metrics (args go to sample-node.py)')
    q.add_argument('host', choices=hosts)
    q.add_argument('rest', nargs=argparse.REMAINDER)
    q = o.add_parser('memory', help="the host's newest stored MemAvailable; exit 1 below the floor, 3 if too old")
    q.add_argument('host', choices=hosts)
    q.add_argument('--floor', type=float, default=10, help='GiB (default: 10, the run abort limit)')
    q.add_argument('--max-age', type=int, default=300, help='seconds a reading stays usable (default: 300)')
    o.add_parser('pipeline', help='persistent download/build/start/benchmark job').add_argument('host', choices=hosts)

    p = sub.add_parser('eval', help='evaluation: task suite, benchmark, report')
    e = p.add_subparsers(dest='action', required=True, metavar='ACTION')
    e.add_parser('suite', help='scripts/suite.py derive|run|validate|report ...').add_argument('rest', nargs=argparse.REMAINDER)
    e.add_parser('bench', help='incremental benchmark: run|status|export ...').add_argument('rest', nargs=argparse.REMAINDER)
    e.add_parser('ladder', help='context ladder: scripts/ladder.py run|report ...').add_argument('rest', nargs=argparse.REMAINDER)
    e.add_parser('watch', help='evaluation watchdog: check|status|test-notification').add_argument('rest', nargs=argparse.REMAINDER)
    q = e.add_parser('report', help='markdown report from a benchmark state.sqlite, or the failure classes of the suite')
    q.add_argument('database', nargs='?', help='benchmark state.sqlite (omit it with --failures)')
    q.add_argument('--failures', action='store_true',
                   help='the task suite instead: failure classes from the fields the runner stored, no trace read '
                        '(PA §10, T11.2)')
    q.add_argument('--alias', help='with --failures: one alias only')
    q.add_argument('--task', help='with --failures: one task only')
    q.add_argument('--agent', help='with --failures: one Agent only')
    q.add_argument('--against', metavar='ALIAS',
                   help='with --failures: which test ids fail for --alias and not for this one')
    q.add_argument('--json', action='store_true')
    q.add_argument('rest', nargs=argparse.REMAINDER)

    sub.add_parser('completion', help='print the bash completion script')
    p = sub.add_parser('observe', help='the observation side of PA §14 in one screen: suite, tasks, router, guards, gateway')
    p.add_argument('--source', action='append', choices=[source.source for source in observe_module.SOURCES],
                   help='show only this source; repeat to choose several')
    p.add_argument('--no-gateway', action='store_true', help='skip journalctl (the gateway row says unavailable)')
    p.add_argument('--days', type=int, metavar='N', help='ask journald only for the last N days (the other sources are read whole)')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('sessions', help='count pi\'s transcripts: the routine work, with no body kept (T11.1)')
    p.add_argument('--days', type=int, metavar='N', help='count only sessions that started in the last N days')
    p.add_argument('--top', type=int, default=10, metavar='N', help='rows per top list (default 10, the completion condition of T11.1)')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('log-task', help='append one line of real-usage record (Roadmap Phase 3): no body, no prose')
    p.add_argument('--repo', required=True, metavar='NAME', help='the repository by name, not a path')
    p.add_argument('--type', required=True, choices=tasklog.TYPES, help='what kind of task it was')
    p.add_argument('--outcome', required=True, choices=tasklog.OUTCOMES,
                   help='accepted, edited, rejected or abandoned (Roadmap Phase 3)')
    p.add_argument('--minutes', required=True, type=float, help='how long the task took')
    p.add_argument('--failure', choices=tasklog.FAILURES,
                   help='required for a rejected or abandoned task; forbidden for an accepted one')
    p.add_argument('--agent', choices=sorted(adapters.ADAPTERS), default='pi')
    p.add_argument('--target', help='which local-coder target served it (default: what the route says now)')
    p.add_argument('--dry-run', action='store_true', help='print the line; write nothing')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('evidence', help='the derived evidence DB (PA §9.2): what has been measured, under which keys')
    v = p.add_subparsers(dest='action')
    q = v.add_parser('import', help='rebuild artifacts/evidence/evidence.sqlite from the primary records')
    q.add_argument('--dry-run', action='store_true', help='report what it would write; change nothing')
    q.add_argument('--json', action='store_true')
    p.set_defaults(node=None, target=None, agent=None, workload=None, metric=None, confidence=None,
                   usable=False, stale=False, json=False)     # bare `evidence` is `show`, with show's filters unset
    q = v.add_parser('show', help='the rows of the derived DB (default)')
    for flag, text in (('--node', 'filter by node'), ('--target', 'filter by the declared route target'),
                       ('--agent', 'filter by the Agent that ran (pi, claude-code, codex, none)'),
                       ('--workload', 'filter by task or ladder depth')):
        q.add_argument(flag, metavar='VALUE', help=text)
    q.add_argument('--metric', choices=list(evidence_module.METRICS), help='one measurement kind')
    q.add_argument('--confidence', choices=list(evidence_module.CONFIDENCE), help='how the value was obtained')
    q.add_argument('--usable', action='store_true', help='only the rows that may decide a ranking (PA §9.3)')
    q.add_argument('--stale', action='store_true', help='only rows whose keys no longer follow from the declaration')
    q.add_argument('--json', action='store_true')
    from agentiac.management.admin import configure as configure_dashboard
    configure_dashboard(sub.add_parser('dashboard', help='private dashboard maintenance and reviewed local services'))
    q = sub.add_parser('agent', help='declared Agents: pinned path, version and the capability matrix (PA §12.2-12.3)')
    a = q.add_subparsers(dest='agent_command')
    l = a.add_parser('list', help='what is pinned and which gateway paths it may use (default)')
    l.add_argument('--capabilities', action='store_true', help='also show the capability matrix with version and time')
    l.add_argument('--json', action='store_true')
    c = a.add_parser('capabilities', help='the capability matrix of one Agent, with the version it was probed at')
    c.add_argument('agent_id', nargs='?', choices=sorted(adapters.ADAPTERS))
    c.add_argument('--json', action='store_true')
    p = a.add_parser('probe', help='run one probe in the suite sandbox and record what it showed')
    p.add_argument('agent_id', choices=sorted(adapters.ADAPTERS))
    p.add_argument('feature', choices=list(adapters.CAPABILITIES))
    p.add_argument('--alias', default='local-coder', help='the gateway alias the probe asks for')
    return parser


def resolve_former_hosts(args):
    """A host argument given by a former name (inventory `former_names`) becomes the current host, said once (T8.10)."""
    former = declared.former_names(inventory())
    if not former:
        return
    for field in ('host', 'hosts', 'node'):
        value = getattr(args, field, None)
        names = value if isinstance(value, list) else [value]
        for name in names:
            if isinstance(name, str) and name in former:
                print(f'{name} is now {former[name]} (a former name in infra/inventory.yml)', file=sys.stderr)
        if isinstance(value, list):
            setattr(args, field, [former.get(name, name) for name in value])
        elif isinstance(value, str):
            setattr(args, field, former.get(value, value))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    extra = []
    if argv[:1] == ['code'] and '--' in argv:  # pi's own arguments, untouched
        argv, extra = argv[:argv.index('--')], argv[argv.index('--') + 1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    args.extra = extra
    resolve_former_hosts(args)
    if isinstance(getattr(args, 'backend', None), str):
        # a retired backend file name (backends/legacy-names.yml) or a manifest id names its current file
        from agentiac import backend_names
        args.backend = backend_names.resolve_in(ROOT, args.backend)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == 'worktrees' and not args.action:
        args.action = 'list'
    if args.command == 'dashboard':
        return args.func(args)
    if args.command in NATIVE:
        return NATIVE[args.command](args)
    extra = None
    if args.command == 'ops' and args.action == 'deploy' and not args.check:
        # ansible replaces this process, so the lock is handed to it and lasts for the whole deploy (F05, D2.1)
        try:
            extra = mutation.hold_for_exec(ROOT, 'ops deploy')
        except mutation.Busy as busy:
            raise SystemExit(explain(str(busy), ['another agentiac command is changing the system'],
                                     'cat artifacts/locks/mutation.lock', 'wait for it to finish, then deploy again'))
    if extra:
        run(PASS_THROUGH[args.command](args), extra)
    else:
        run(PASS_THROUGH[args.command](args))


if __name__ == '__main__':
    sys.exit(main())
