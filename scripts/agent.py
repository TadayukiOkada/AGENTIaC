#!/usr/bin/env python3
"""Start pi against a local gateway alias in a fresh git worktree of REPO.

The worktree gets its own branch (agent/<timestamp>) so the user's checkout and
uncommitted changes are never touched; review and merge the branch yourself.
pi state (models.json, sessions) lives in artifacts/agents/pi-daily, separate
from any personal pi configuration.
"""
import argparse
import datetime
import json
import os
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


def single_model(alias, thinking):
    """(pi model entry, pi model arguments, banner) for one alias at the starting THINKING level (None: default)."""
    model = model_entry(alias, thinking=True)
    if thinking not in (None, 'off') and not model.get('reasoning'):
        raise SystemExit(f"{model['name']} has no thinking switch (local-coder targets: 'thinking' in "
                         'gateway/local-coder.json); omit --thinking or pick another model')
    # A local-coder target with a switch starts at its 'thinking_start' (default off); tracked models keep pi's default.
    start = thinking or (target(alias)[1].get('thinking_start', 'off') if 'compat' in model else None)
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
    parser.add_argument('--alias', default='local-coder', help="gateway alias, or 'router' for agents/pi/extensions/router.ts")
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
    env = {name: value for name, value in os.environ.items()
           if name not in ('LITELLM_MASTER_KEY', 'LITELLM_TASK_KEY', 'LITELLM_CODE_CLOUD_KEY', 'LITELLM_CODE_KEY')}
    env.update(PI_CODING_AGENT_DIR=str(STATE), PI_OFFLINE='1',
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
        model, model_args, banner = single_model(args.alias, args.thinking)
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
