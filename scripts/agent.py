#!/usr/bin/env python3
"""Start pi against a local gateway alias in a fresh git worktree of REPO.

The worktree gets its own branch (agent/<timestamp>) so the user's checkout and
uncommitted changes are never touched; review and merge the branch yourself.
pi state (models.json, sessions) lives in artifacts/agents/pi-daily, separate
from any personal pi configuration.
The local-any alias selects a worker once per launch, including on resume.
When resume picks a different worker, the old node's prefix cache is not reused.
"""
import argparse
import datetime
import json
import os
import re
from pathlib import Path
import subprocess
import sys

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
sys.path.insert(0, str(CODE / 'scripts'))
from agentiac import route_aliases
from agentiac.secrets import read_key  # noqa: E402
from agentiac.adapters import EXTENSIONS, NODE, PI, PI_TREE, gateway  # noqa: E402  the pinned install is one fact (U4)
import route  # noqa: E402
from agentiac.errors import command as cli_name  # noqa: E402

STATE = ROOT / 'artifacts/agents/pi-daily'
WORKTREES = Path.home() / 'agent-worktrees'
# local-any's default pool: the tracked pi models named <node>-worker (one fixed node each), in declaration order.
LOCAL_POOL_PATTERN = re.compile(r'[a-z0-9]+-worker')

THINKING = ['off', 'minimal', 'low', 'medium', 'high']
COMPACTION_GUARD = EXTENSIONS / 'compaction-guard.ts'
SKILLS = ROOT / 'agents/skills'

# pi's own default is 300000 (undici's bodyTimeout/headersTimeout on the global dispatcher), which
# terminates long cold-prefill turns (the strata worker sends its first chunk immediately and then
# nothing until prefill completes, exceeding 300 s on large inputs). 600000 leaves headroom above
# long prefills while staying below the gateway's own `timeout: 900` (gateway/config.yaml), so a truly
# stuck request is still stopped by the gateway rather than never. pi reads this key from the agent
# dir's settings.json at startup (globalSettings.httpIdleTimeoutMs); the running session keeps the
# value it started with.
HTTP_IDLE_TIMEOUT_MS = 600000


def compaction(models):
    """pi's compaction block for the MODELS a session can run.

    pi gives a summary `min(floor(0.8 * reserveTokens), maxTokens)` answer tokens, and a summary
    that hits that cap is a failure (see agents/pi/extensions/compaction-guard.ts). So
    reserveTokens is twice maxTokens: 0.8 of it is more than maxTokens, which pi then clamps to
    maxTokens, so the summary gets the model's whole answer budget. It is capped at a quarter of
    the window, because reserveTokens is also what makes pi compact early (at contextWindow -
    reserveTokens) and a model must keep room to work.

    Every model is sized, thinking switch or not: settings.json is one file shared by every
    session of the agent dir, so a model left out inherits the last session's block. A 16K
    window under Spark's 32K reserve has a trigger below zero and compacts at every check. With
    several models (the router runs a low and a high one) the tightest of each wins.
    """
    return {'enabled': True, 'reserveTokens': min(min(2 * m['maxTokens'], m['contextWindow'] // 4) for m in models),
            'keepRecentTokens': min(min(12000, m['contextWindow'] // 8) for m in models)}


def write_settings(state, block):
    """Put the compaction BLOCK and the HTTP idle timeout into the agent dir's settings.json, leaving every other key alone.

    artifacts/ is gitignored, so a re-claim or a fresh host would otherwise lose the settings
    that make pi compact before the context is full and survive a cold prefill over 300 s. An
    unreadable file is reported, never overwritten: pi would only report the same load error, and
    the user's own settings are not agentiac's to discard.
    """
    path = state / 'settings.json'
    settings = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text())
        except ValueError as error:
            raise SystemExit(f'{path} is not valid JSON ({error}); fix or remove it, agentiac will not overwrite it')
        if not isinstance(settings, dict):
            raise SystemExit(f'{path} must contain a JSON object; fix or remove it, agentiac will not overwrite it')
    changed = False
    if settings.get('compaction') != block:
        settings['compaction'] = block
        changed = True
    if settings.get('httpIdleTimeoutMs') != HTTP_IDLE_TIMEOUT_MS:
        settings['httpIdleTimeoutMs'] = HTTP_IDLE_TIMEOUT_MS
        changed = True
    if changed:
        path.write_text(json.dumps(settings, indent=2) + '\n')


def target(alias):
    """(target name, spec) in gateway/local-coder.json for a local-coder ALIAS, or None for other aliases."""
    routes = json.loads(route.ROUTES.read_text())
    selected = route_aliases.target_for_api(routes, alias, route.current())
    return (selected, routes['targets'][selected]) if selected is not None else None


def local_pool(tracked_models):
    """The ordered local-any pool: AGENTIAC_LOCAL_POOL (each a tracked model id), else the <node>-worker models."""
    override = os.environ.get('AGENTIAC_LOCAL_POOL')
    if override is None:
        pool = tuple(alias for alias in tracked_models if LOCAL_POOL_PATTERN.fullmatch(alias))
        if not pool:
            raise SystemExit('local-any: no tracked local-infra model is named <node>-worker; '
                             'declare one in agents/pi/models.json or set AGENTIAC_LOCAL_POOL')
        return pool
    pool = tuple(alias.strip() for alias in override.split(','))
    for alias in pool:
        if alias not in tracked_models:
            raise SystemExit(f'AGENTIAC_LOCAL_POOL: {alias!r} is not a tracked local-infra model id')
    return pool


def session_env(pid, raw):
    """The launch environment of PID when PID is a pi session itself, else None.

    pi rewrites its process title, so its command line cannot be read; its environment can. Children
    of a session (pi's bash tool) inherit the markers but not the PID agent.py recorded before execve.
    """
    env = dict(entry.split('=', 1) for entry in os.fsdecode(raw).split('\0') if '=' in entry)
    return env if env.get('AGENTIAC_PI_PID') == pid else None


def process_envs():
    """Yield the environments of running pi sessions (readable Linux processes), excluding this launcher."""
    own_pid = str(os.getpid())
    for environ in Path('/proc').glob('[0-9]*/environ'):
        pid = environ.parent.name
        if pid == own_pid:
            continue
        try:
            raw = environ.read_bytes()
        except OSError:
            continue
        env = session_env(pid, raw) if raw else None
        if env is not None:
            yield env


def count_local_sessions(envs, pool, local_coder=None):
    """Count daily pi sessions by pool alias from their launch environments."""
    counts = dict.fromkeys(pool, 0)
    routed_aliases = []
    if local_coder:
        node = local_coder.get('node')
        source = local_coder.get('source_alias')
        routed_aliases = [alias for alias in counts if alias == source or
                          (node and (alias == f'{node}-worker' or alias.startswith(f'{node}-worker-')))]

    for env in envs:
        alias = env.get('AGENTIAC_PI_MODEL')
        if env.get('PI_CODING_AGENT_DIR') != str(STATE) or not alias:
            continue
        if alias == 'local-coder':
            for worker in routed_aliases:
                counts[worker] += 1
        elif alias in counts:
            counts[alias] += 1
    return counts


def pick_local_alias(tracked_models):
    pool = local_pool(tracked_models)
    try:
        resolved = target('local-coder')
    except (OSError, ValueError, KeyError, TypeError):
        resolved = None
    counts = count_local_sessions(process_envs(), pool, resolved[1] if resolved else None)
    alias = min(pool, key=counts.__getitem__)
    sessions = ', '.join(f'{worker}={count}' for worker, count in counts.items())
    print(f'local-any: picked {alias} (sessions: {sessions})', file=sys.stderr)
    return alias


def model_entry(alias, thinking=False):
    """pi model metadata for ALIAS; local-coder follows the currently rendered target.

    local-coder runs without thinking. With THINKING, a target that names its verified
    switch in gateway/local-coder.json ('thinking': pi's compat.thinkingFormat) is
    described as a reasoning model, so pi can turn thinking on; other targets are unchanged.
    """
    found = target(alias)
    if found:
        name, spec = found
        entry = {'id': alias, 'name': f'local-coder ({name})', 'contextWindow': spec['context_window'],
                 'maxTokens': spec['max_tokens'], 'reasoning': False}
        if thinking and 'thinking' in spec:
            entry.update(reasoning=True, compat=spec.get('compat', {'thinkingFormat': spec['thinking']}))
        return entry
    tracked = json.loads((ROOT / 'agents/pi/models.json').read_text())['providers']['local-infra']['models']
    for model in tracked:
        if model['id'] == alias:
            return model
    raise SystemExit(f'unknown alias {alias!r}')


def single_model(alias, thinking, tracked_model=None):
    """(pi model entry, pi model arguments, banner) for one alias at the starting THINKING level (None: default)."""
    model = model_entry(alias, thinking=True) if tracked_model is None else tracked_model
    if thinking not in (None, 'off') and not model.get('reasoning'):
        raise SystemExit(f"{model['name']} has no thinking switch (local-coder targets: 'thinking' in "
                         'gateway/local-coder.json); omit --thinking or pick another model')
    # A local-coder target with a switch starts at its 'thinking_start' (default off); tracked models keep pi's default.
    start = thinking
    if start is None and tracked_model is None and 'compat' in model:
        start = target(alias)[1].get('thinking_start', 'off')
    model_args = ['--provider', 'local-infra', '--model', alias] + (['--thinking', start] if start else [])
    banner = f"{model['name']}, context {model['contextWindow']}, max {model['maxTokens']} tokens"
    if start and model.get('reasoning'):
        banner += f', thinking {start} (/thinking to change)'
    return model, model_args, banner


def toplevel(repo):
    top = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if top.returncode:
        raise SystemExit(f'{repo} is not a git repository')
    return Path(top.stdout.strip())


def router_config(repo_name, allow_cloud):
    """Configuration for the router extension (agents/pi/extensions/router.ts) in REPO_NAME."""
    tracked = json.loads((ROOT / 'agents/pi/router.json').read_text())
    low = model_entry(tracked['low'])
    return {'provider': 'local-infra', 'low': tracked['low'], 'high': tracked['high'], 'repo': repo_name,
            'cloudAllowed': repo_name in tracked['cloud_repos'], 'confirm': not allow_cloud,
            'threshold': tracked['threshold'], 'contextWindow': low['contextWindow'], 'maxTokens': low['maxTokens'],
            'log': str(STATE / 'router-log.jsonl')}


def worktree(top, name):
    if subprocess.run(['git', '-C', str(top), 'status', '--porcelain'], capture_output=True, text=True).stdout.strip():
        print(f'note: uncommitted changes in {top} are not part of the worktree', file=sys.stderr)
    path = WORKTREES / f'{top.name}-{name}'
    subprocess.run(['git', '-C', str(top), 'worktree', 'add', '-b', f'agent/{name}', str(path)], check=True)
    return path


def latest_session(state):
    """Return the latest saved session and its original worktree, without changing either."""
    sessions = list((state / 'sessions').rglob('*.jsonl'))
    if not sessions:
        raise SystemExit('no saved pi sessions found')
    session = max(sessions, key=lambda p: (p.stat().st_mtime_ns, str(p)))
    try:
        with session.open(encoding='utf-8') as f:
            header = json.loads(f.readline())
        if header.get('type') != 'session' or not isinstance(header.get('cwd'), str):
            raise ValueError('missing session cwd')
        cwd = Path(header['cwd'])
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError('saved worktree is missing')
        if toplevel(cwd) != cwd.resolve():
            raise ValueError('saved cwd is not a git worktree root')
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f'cannot resume {session}: {error}') from error
    return session, cwd.resolve()


def code_key_name(alias, cloud_allowed):
    """Which gateway key this session carries (gateway/task_policy.py): the cloud one only where cloud is reachable."""
    return 'LITELLM_CODE_CLOUD_KEY' if alias == 'cloud-gpt-sol' or (alias == 'router' and cloud_allowed) else 'LITELLM_CODE_KEY'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--alias', default='local-coder',
                        help='gateway alias (default local-coder), local-any for the least busy worker, '
                             'or router for rule-based escalation')
    parser.add_argument('--allow-cloud', action='store_true',
                        help='router: escalate to the cloud model without asking (repository must be in agents/pi/router.json)')
    parser.add_argument('--in-place', action='store_true', help='work in REPO directly instead of a new worktree')
    parser.add_argument('--resume', action='store_true', help='resume the most recent saved pi session in its worktree')
    parser.add_argument('--loop-guard', action='store_true',
                        help='load agents/pi/extensions/loop-guard.ts (stops repeated near-identical bash commands)')
    parser.add_argument('--thinking', choices=THINKING,
                        help="pi's starting thinking level (local-coder: the target's thinking_start, else off); "
                             '/thinking changes it when the model has a switch')
    parser.add_argument('pi_args', nargs=argparse.REMAINDER, help='extra pi arguments after --')
    args = parser.parse_args()
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    resumed = latest_session(STATE) if args.resume else None
    top = toplevel(resumed[1] if resumed else args.repo.resolve())
    config = json.loads((ROOT / 'agents/pi/models.json').read_text())
    tracked_model = None
    if args.alias == 'local-any':
        tracked_models = {model['id']: model for model in config['providers']['local-infra']['models']}
        args.alias = pick_local_alias(tracked_models)
        tracked_model = tracked_models[args.alias]
    env = {name: value for name, value in os.environ.items()
           if name not in ('LITELLM_MASTER_KEY', 'LITELLM_TASK_KEY', 'LITELLM_CODE_CLOUD_KEY', 'LITELLM_CODE_KEY')}
    env.update(PI_CODING_AGENT_DIR=str(STATE), PI_OFFLINE='1', AGENTIAC_PI_MODEL=args.alias, AGENTIAC_PI_PID=str(os.getpid()),
               PATH=str(NODE.parent) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'))
    cloud_allowed = False
    if args.alias == 'router':
        if args.thinking:
            raise SystemExit('the router picks the thinking level itself (agents/pi/extensions/router.ts); omit --thinking')
        router = router_config(top.name, args.allow_cloud)
        cloud_allowed = router['cloudAllowed']
        config['providers']['local-infra']['models'] = [model_entry(router['low']), model_entry(router['high'])]
        (STATE / 'router.json').write_text(json.dumps(router, indent=2) + '\n')
        env['AGENTIAC_ROUTER'] = str(STATE / 'router.json')
        model_args = ['-e', str(CODE / 'agents/pi/extensions/router.ts'), '--provider', 'router', '--model', 'auto']
        cloud = ('without asking' if args.allow_cloud else 'after confirmation') if router['cloudAllowed'] else 'not allowed here'
        banner = f"router: {router['low']}, hard tasks -> {router['high']} ({cloud})"
    else:
        model, model_args, banner = single_model(args.alias, args.thinking, tracked_model)
        config['providers']['local-infra']['models'] = [model]
    try:
        env['LITELLM_CODE_KEY'] = read_key(ROOT / 'secrets/gateway.env', code_key_name(args.alias, cloud_allowed))
    except ValueError:
        raise SystemExit(f'secrets/gateway.env has no code key yet; run {cli_name()} ops deploy once')
    config['providers']['local-infra']['apiKey'] = '${LITELLM_CODE_KEY}'  # literal: the copy pi reads, not the tracked file
    if args.loop_guard:
        env['AGENTIAC_LOOP_GUARD_LOG'] = str(STATE / 'loop-guard-log.jsonl')
        model_args = ['-e', str(CODE / 'agents/pi/extensions/loop-guard.ts')] + model_args
        banner += ', loop guard on'
    models = config['providers']['local-infra']['models']
    block = compaction(models)
    write_settings(STATE, block)
    # Always loaded, not a flag: a session that never compacts loses its whole context.
    env['AGENTIAC_COMPACTION_GUARD_LOG'] = str(STATE / 'compaction-guard-log.jsonl')
    model_args = ['-e', str(COMPACTION_GUARD)] + model_args
    model_args = ['--skill', str(SKILLS)] + model_args
    banner += f", compacts over {min(model['contextWindow'] for model in models) - block['reserveTokens']} tokens"
    (STATE / 'models.json').write_text(json.dumps(config, indent=2) + '\n')
    name = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    cwd = resumed[1] if resumed else args.repo.resolve() if args.in_place else worktree(top, name)
    print(f'pi -> {banner} via {gateway().removeprefix("http://")}; working in {cwd}', file=sys.stderr)
    extra = args.pi_args[1:] if args.pi_args[:1] == ['--'] else args.pi_args
    if resumed:
        if any(x in ('--session', '--session-id', '--resume', '-r', '--continue', '-c', '--no-session') or x.startswith('--session=') for x in extra):
            raise SystemExit('--resume cannot be combined with pi session selection flags')
        model_args += ['--session', str(resumed[0])]
    os.chdir(cwd)
    os.execve(str(NODE), [str(NODE), str(PI)] + model_args + extra, env)


if __name__ == '__main__':
    main()
