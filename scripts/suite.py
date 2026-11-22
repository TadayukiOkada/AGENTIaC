#!/usr/bin/env python3
"""Phase 2 task suite: one runner for frontend x alias x task x trial.

  derive  build a task manifest from a fix commit (acceptance = tests that fail
          on the parent and pass on the commit; regression = tests passing on both)
  validate  controls: the reference commit must pass and the unchanged base must fail
  run     run pi on every registered (alias, task, trial) case not yet finished;
          aliases run in parallel, cases of one alias one at a time
  report  summarize the SQLite state (pass rate with Wilson 95% interval, wall time)

Tasks live in the site's benchmarks/suite/tasks/*.json; the regression id lists they hash are
cached in artifacts/suite/derived/. Sources are read-only bare clones under
artifacts/suite/sources/. Settings come from the alias (agent.model_entry); pi
uses its default system prompt, as in daily use. Traces stay in artifacts/suite/runs/.
"""
import argparse
import collections
import concurrent.futures
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
sys.path.insert(0, str(CODE / 'scripts'))
from agentiac import adapters, declared, failures, hardware, identities, pathwatch, suite  # noqa: E402
from agentiac.errors import explain, headline  # noqa: E402
from agentiac import route_aliases
from agentiac.secrets import read_key, redact  # noqa: E402
from agentiac.state import Store, stable_id  # noqa: E402
import agent  # noqa: E402
import route  # noqa: E402

TASKS = paths.locate('benchmarks/suite/tasks', ROOT)   # the site's suite (D6.7 item 3, revised)
DATA = ROOT / 'artifacts/suite'
TIMEOUT = 1800
PROMPT = adapters.PiAdapter.PROMPT  # the baseline prompt; kept here so the case text stays visible in this file


def source_path(task):
    return DATA / 'sources' / (task['repo'] + '.git')


def regression_ids(task):
    """The cached regression id list, re-derived if missing; verified against the manifest hash."""
    cached = DATA / 'derived' / (task['id'] + '.json')
    if not cached.exists():
        derived = suite.derive(source_path(task), task['reference'], task['base'])
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(derived, indent=1) + '\n')
    ids = json.loads(cached.read_text())['regression']
    if hashlib.sha256('\n'.join(ids).encode()).hexdigest() != task['regression_sha256']:
        raise ValueError(f"{task['id']}: derived regression ids differ from the manifest")
    return ids


def load_task(name):
    task = json.loads((TASKS / (name + '.json')).read_text())
    task['regression'] = regression_ids(task)
    return task


def derive(args):
    source = DATA / 'sources' / (args.repo + '.git')
    derived = suite.derive(source, args.commit)
    if not derived['acceptance'] or derived['broken_by_reference']:
        raise SystemExit(json.dumps({k: derived[k] for k in ('acceptance', 'broken_by_reference', 'base_failures')}, indent=1))
    (DATA / 'derived').mkdir(parents=True, exist_ok=True)
    (DATA / 'derived' / (args.id + '.json')).write_text(json.dumps(derived, indent=1) + '\n')
    task = {'id': args.id, 'repo': args.repo, 'base': derived['base'], 'reference': derived['reference'],
            'issue': Path(args.issue).read_text().strip(), 'acceptance': derived['acceptance'],
            'regression_count': len(derived['regression']),
            'regression_sha256': hashlib.sha256('\n'.join(derived['regression']).encode()).hexdigest(),
            'reference_changed_paths': derived['changed_paths']}
    TASKS.mkdir(parents=True, exist_ok=True)
    (TASKS / (args.id + '.json')).write_text(json.dumps(task, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'task': args.id, 'acceptance': len(task['acceptance']), 'regression': task['regression_count'],
                      'base_failures': derived['base_failures']}, indent=1, ensure_ascii=False))


def validate(args):
    import tempfile
    results = {}
    for name in args.task:
        task = load_task(name)
        controls = {}
        for label, rev in (('reference', task['reference']), ('base', task['base'])):
            with tempfile.TemporaryDirectory() as scratch:
                suite.export(source_path(task), rev, Path(scratch) / 'tree')
                controls[label] = suite.grade(Path(scratch) / 'tree', source_path(task), task)[0]['passed']
        results[name] = {**controls, 'ok': controls == {'reference': True, 'base': False}}
        print(json.dumps({name: results[name]}), flush=True)
    (DATA / 'validation.json').write_text(json.dumps(results, indent=2) + '\n')
    raise SystemExit(0 if all(r['ok'] for r in results.values()) else 1)


def pi_events(text):
    """Tool counts, tool errors, assistant turns, final stop reason and summed usage from pi --mode json.

    The reading belongs to the pi adapter (T12.1); this name stays because the report and the tests
    read events through it.
    """
    return adapters.adapter('pi').parse(text, '')


GUARD = CODE / 'agents/pi/extensions/loop-guard.ts'


def guard_spec(enabled):
    """Case-spec entry of the optional loop guard; None keeps suite v1 case ids unchanged."""
    return {'extension': GUARD.name, 'sha256': hashlib.sha256(GUARD.read_bytes()).hexdigest()} if enabled else None


def label(spec):
    """Report row of a case: its alias, marked when the loop guard or thinking ran."""
    return (spec['alias'] + (f" [{spec['agent']}]" if spec.get('agent') else '')
            + (' +loop-guard' if spec.get('loop_guard') else '')
            + (f" +thinking-{spec['thinking']}" if spec.get('thinking') else ''))


def provider_config(spec):
    """Per-run provider override, without mutating gateway or daily-use settings (pi adapter)."""
    return adapters.adapter('pi').provider_config(spec)


def evaluation_model(alias, thinking, provider=None, agent_id=None):
    return adapters.adapter(agent_id).model_for(alias, thinking=thinking, provider=provider)


def revision(root=ROOT):
    """The commit the run was made from, and whether the tree was dirty when it started.

    A dirty tree is recorded rather than refused: the operator may be measuring a candidate
    fix, and a result that cannot say which is a result that cannot be repeated.
    """
    try:
        commit = subprocess.run(['git', '-C', str(root), 'rev-parse', '--short', 'HEAD'],
                                capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = bool(subprocess.run(['git', '-C', str(root), 'status', '--porcelain'],
                                    capture_output=True, text=True, timeout=10).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None, None
    return commit or None, dirty


def target_of(alias, graph, live=None):
    """Which declared target serves this alias, or None when the alias is not in the declared graph.

    `route.py render()` publishes every target twice: once as `local-coder-<target>`, and once
    as the bare route alias pointing at the target the route is live on. Anything else — an
    alias of another gateway config, a typo — is not in the graph, and its provenance stays
    unknown instead of quietly borrowing the live target's.
    """
    selected = route.current() if alias == graph.route['alias'] and live is None else (live or graph.route['default'])
    return route_aliases.target_for_api(graph.route, alias, selected)


def provenance(alias, agent_version, root=ROOT, now=None, agent=adapters.BASELINE):
    """What a run of this alias was served by, on what hardware, from what code (T10.3).

    Every value is read from the declared graph, the inventory record and git; none of it is
    passed in by the caller, so a run cannot claim a configuration it was not served by. Each
    part degrades on its own: a host nobody inventoried costs the hardware identity and
    nothing else, a missing graph costs the whole block. It never raises and never stops a
    measurement that is already costing GPU time.

    It goes in the result, never in the case spec: `suite: v1` case ids are the hash of the
    spec, so adding a field there would orphan every stored result (X18).
    """
    prov = {'agent': {'name': agent, 'version': agent_version},
            'recorded_at': datetime.datetime.now().isoformat(timespec='seconds'), 'unknowns': {}}
    try:
        graph = declared.load(root)
        target = target_of(alias, graph)
        if target is None:
            prov['unknowns']['target'] = f'alias {alias!r} is not a target of the declared route'
        else:
            identity = identities.compute_target(graph, target).as_dict()
            prov.update({'target': target, 'node': identity['node'],
                         'class_id': identity['class_id'], 'config_id': identity['config_id']})
            if identity['unknowns']:  # an id computed over missing inputs says which ones were missing
                prov['unknowns']['identity'] = identity['unknowns']
            reading = hardware.fingerprint(hardware.directory(root), identity['node'], now=now)
            prov['hw_fp'] = reading.pop('hw_fp')
            prov['hardware'] = reading  # when it was read, how old it is, and why it may be unknown
            if 'reason' in reading:
                prov['unknowns']['hw_fp'] = reading['reason']
    except Exception as error:  # a provenance gap is reported, never a crash in the middle of a paid run
        prov['unknowns']['provenance'] = f'{type(error).__name__}: {error}'
    prov['revision'], prov['revision_dirty'] = revision(root)
    if prov['revision'] is None:
        prov['unknowns']['revision'] = 'git did not answer'
    return prov


def run_case(case, spec, task, key, folder, chosen=None):
    """One attempt on a fresh export of the task base, graded on a copy.

    The Agent is whatever the case spec names (`adapters.adapter(spec)`), so pi and Claude Code run
    through the same Executor: same export, same sandbox, same grading, same trace handling. The
    adapter only supplies argv, env, mounts and the reading of the output (PA §12.1).
    """
    chosen = chosen or adapters.adapter(spec)
    work = folder / 'workspace'
    folder.mkdir(parents=True, exist_ok=True)
    chosen.prepare(spec, folder / 'agent-state')
    suite.export(source_path(task), task['base'], work)
    for step in (['init', '-q'], ['add', '-A'], ['-c', 'user.name=suite', '-c', 'user.email=suite@localhost',
                                                  'commit', '-q', '-m', 'base ' + task['base']]):
        subprocess.run(['git', '-C', str(work)] + step, check=True, capture_output=True)
    invocation = chosen.invocation(adapters.RunRequest(case, spec, task, folder, key))
    started, timed_out = time.monotonic(), False
    try:
        result = subprocess.run(list(invocation.argv), env=dict(os.environ, **invocation.env),
                                capture_output=True, text=True, timeout=invocation.timeout_s)
        stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as error:
        subprocess.run(['docker', 'stop', '-t', '5', invocation.name], capture_output=True, timeout=30)
        decode = lambda b: b.decode(errors='replace') if isinstance(b, bytes) else (b or '')
        stdout, stderr, returncode, timed_out = decode(error.stdout), decode(error.stderr), None, True
    wall = time.monotonic() - started
    (folder / 'trace.jsonl').write_text(redact(stdout, [key]))
    (folder / 'stderr.txt').write_text(redact(stderr, [key]))
    graded, output = suite.grade(work, source_path(task), task, spec['image'])
    (folder / 'grading-output.txt').write_text(output)
    subprocess.run(['git', '-C', str(work), 'add', '-A'], capture_output=True)
    diff = subprocess.run(['git', '-C', str(work), 'diff', '--cached', '--stat', 'HEAD'], capture_output=True, text=True).stdout
    return {**graded, 'frontend_returncode': returncode, 'timed_out': timed_out, 'wall_s': round(wall, 1),
            **chosen.parse(stdout, stderr), 'diff_stat': diff}


def run(args):
    if os.geteuid() == 0:
        # Launched as root (a bare `sudo systemd-run`, say), git refuses the task mirror as a dubious
        # ownership, tar hands back an empty archive and every attempt lands in the database as a 0-second
        # infrastructure_error. Launch the unit with --uid=<the service account> instead.
        account = declared.local_account(declared.load(ROOT).nodes, ROOT)
        raise SystemExit(explain(f'the suite cannot run as root: run it as the owner (--uid={account} with systemd-run)',
                                 ['it was started with sudo, or by systemd-run without --uid'],
                                 '`id -u`, or for a unit `systemctl show --property=User UNIT` (empty means root)',
                                 f'start it again with `sudo systemd-run --uid={account} ...`'))
    os.umask(0o077)
    store = Store(DATA / 'state.sqlite')
    image = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', suite.IMAGE], text=True).strip()
    chosen = adapters.adapter(args.agent)
    if args.loop_guard and not chosen.baseline:
        # The loop guard is a pi extension; putting its hash in another Agent's spec would claim that Agent
        # ran with something it never loaded.
        raise SystemExit(f'--loop-guard is a pi extension and {chosen.id} cannot run it')
    try:
        agent_version = chosen.version()
    except adapters.AdapterError as error:  # the operator's answer, not a traceback
        raise SystemExit(str(error))
    tasks = {name: load_task(name) for name in args.task}
    provider = json.loads(args.provider_config.read_text()) if args.provider_config else None
    key = 'evaluation-only' if provider is not None else read_key(ROOT / 'secrets/gateway.env', 'LITELLM_TASK_KEY')
    queues = collections.defaultdict(list)
    for alias in args.alias:
        model = evaluation_model(alias, args.thinking != 'off', provider, agent_id=args.agent)
        if args.thinking != 'off' and not model.get('reasoning'):
            raise SystemExit(f'{alias} has no thinking switch; run it with --thinking off')
        for trial in args.trials:
            for name, task in tasks.items():
                spec = {'suite': 'v1', 'frontend': f'{chosen.id} {agent_version}', 'alias': alias, 'model': model,
                        'image': image, 'task': name,
                        'task_sha256': hashlib.sha256((TASKS / (name + '.json')).read_bytes()).hexdigest(),
                        'trial': trial, 'timeout_s': TIMEOUT}
                spec.update(chosen.spec_entry() or {})  # nothing for the baseline: pi case ids do not move (X18)
                if provider is not None:
                    spec['provider'] = provider
                if guard_spec(args.loop_guard):
                    spec['loop_guard'] = guard_spec(True)
                if args.thinking != 'off':  # off keeps suite v1 case ids unchanged
                    spec['thinking'] = args.thinking
                queues[alias].append((store.register(spec), spec, task))
        # said before the first attempt, so a run that dies early still left its identity in the journal
        print(json.dumps({'alias': alias, 'provenance': provenance(alias, agent_version, root=ROOT, agent=chosen.id)}),
              flush=True)

    def drain(alias):
        gate_said = False   # say once per alias what the gate read; an attempt row is not where to explain it
        for case, spec, task in queues[alias]:
            if args.deadline_epoch is not None and time.time() + TIMEOUT + 900 > args.deadline_epoch:
                print(json.dumps({'alias': alias, 'deferred': True, 'reason': 'insufficient task/grading budget'}), flush=True)
                return
            # Ask the declared front whether the path is up before taking a lease so attempts do not run
            # against a dead tunnel and get recorded as model failures. Waiting costs
            # the run minutes; claiming costs the evidence its honesty.
            waited, ready, checked = 0, True, None
            while True:
                ready, checked = pathwatch.path_ready(ROOT, alias)
                if ready or waited >= args.path_wait:
                    break
                print(json.dumps({'alias': alias, 'waiting_for_path': checked, 'waited_s': waited}), flush=True)
                time.sleep(args.path_poll)
                waited += args.path_poll
            if not ready:
                print(json.dumps({'alias': alias, 'deferred': True, 'reason': checked, 'waited_s': waited}), flush=True)
                return
            if not gate_said:
                gate_said = True
                print(json.dumps({'alias': alias, 'path_checked': checked}), flush=True)
            claimed = store.claim(case, retry_failed=args.retry_failed, lease_seconds=TIMEOUT + 900)
            if not claimed:
                continue
            owner, number = claimed
            folder = DATA / 'runs' / case / f'attempt-{number}'
            # Read per attempt: a bare `local-coder` run lasts hours and the route can move under it.
            identity = provenance(alias, agent_version, root=ROOT, agent=chosen.id)
            try:
                result = run_case(case, spec, task, key, folder)
            except Exception as error:  # infrastructure failure: recorded, not scored as a model result
                result = {'passed': False, 'infrastructure_error': f'{type(error).__name__}: {headline(error)}'}
            result['provenance'] = identity  # what served it, even when nothing ran (T10.3)
            void = pathwatch.void_attempt(result)
            if void:
                # An attempt that counted no tokens, called no tool and changed nothing measured no model, whatever
                # the reason was. `failures.classify` reads this field as the `infrastructure` class and `report`
                # keeps it out of the pass rate, so a retry can happen without the run quietly re-rolling a real
                # failure to make its numbers look better.
                result['infrastructure_error'] = void
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            store.finish(case, owner, number, result['passed'], result)
            if 'infrastructure_error' not in result:
                # The matrix is filled by what actually ran, not by what the documentation claims (T12.4).
                # After the result is safe: a matrix that cannot be written must not cost a graded attempt.
                try:
                    adapters.record_capability(chosen.id, agent_version, 'suite_grading', 'native',
                                               f"case {case[:12]} graded {result['passed']}", root=ROOT)
                except OSError as error:
                    print(json.dumps({'alias': alias, 'capability_not_recorded': str(error)}), flush=True)
            print(json.dumps({'time': datetime.datetime.now().isoformat(timespec='seconds'), 'alias': alias,
                              'task': spec['task'], 'trial': spec['trial'], 'passed': result['passed'],
                              'wall_s': result.get('wall_s'), 'error': result.get('infrastructure_error')}), flush=True)

    with concurrent.futures.ThreadPoolExecutor(len(queues)) as pool:
        for future in [pool.submit(drain, alias) for alias in queues]:
            future.result()


def wilson(passed, total, z=1.96):
    if not total:
        return (0.0, 0.0)
    p = passed / total
    centre, half = p + z * z / (2 * total), z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (round((centre - half) / (1 + z * z / total), 3), round((centre + half) / (1 + z * z / total), 3))


def latest_results(store):
    with store.connect() as db:
        rows = db.execute('''SELECT c.spec, a.status, a.result FROM cases c JOIN attempts a
                             ON a.case_id = c.id AND a.number = c.attempts WHERE a.status IN ('complete', 'failed')''')
        return [(json.loads(spec), status, json.loads(result or '{}')) for spec, status, result in rows]


def report(args):
    if getattr(args, 'failures', False):
        # T11.2: the classes come from the fields the runner stored, so no trace is opened (PA §10).
        if args.against:
            comparison = failures.new_regressions(ROOT, args.alias, args.against, args.task)
            print(json.dumps(comparison, indent=2) if args.json else failures.markdown_comparison(comparison))
        else:
            report_ = failures.summarise(ROOT, args.alias, args.task, args.agent)
            print(json.dumps(report_, indent=2) if args.json else failures.markdown(report_))
        return
    current = {p.stem: hashlib.sha256(p.read_bytes()).hexdigest() for p in TASKS.glob('*.json')}
    rows = [row for row in latest_results(Store(DATA / 'state.sqlite'))
            if current.get(row[0]['task']) == row[0]['task_sha256']]  # cases of superseded manifests are kept, not reported
    groups = collections.defaultdict(list)
    for spec, _, result in rows:
        if 'infrastructure_error' not in result:
            groups[label(spec)].append((spec, result))
    infra = sum('infrastructure_error' in result for _, _, result in rows)
    lines = [f'| alias | passed | 95% CI | median s | p90 s | regressions | timeouts | tool errors/run |',
             '|---|---:|---|---:|---:|---:|---:|---:|']
    tasks = sorted({spec['task'] for spec, _, _ in rows})
    per_task = ['| alias | ' + ' | '.join(tasks) + ' |', '|---|' + '---:|' * len(tasks)]
    for alias, items in sorted(groups.items()):
        passed = sum(r['passed'] for _, r in items)
        walls = sorted(r['wall_s'] for _, r in items)
        p90 = walls[min(len(walls) - 1, math.ceil(0.9 * len(walls)) - 1)]
        lines.append(f'| {alias} | {passed}/{len(items)} | {wilson(passed, len(items))} | {statistics.median(walls):.0f} | '
                     f'{p90:.0f} | {sum(bool(r["regression_failed"]) for _, r in items)} | '
                     f'{sum(r["timed_out"] for _, r in items)} | {statistics.mean(r["tool_errors"] for _, r in items):.1f} |')
        cells = []
        for task in tasks:
            mine = [r for s, r in items if s['task'] == task]
            cells.append(f"{sum(r['passed'] for r in mine)}/{len(mine)}" if mine else '-')
        per_task.append(f'| {alias} | ' + ' | '.join(cells) + ' |')
    print('\n'.join(lines + ['', *per_task, '', f'infrastructure errors (not scored): {infra}']))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    d = sub.add_parser('derive')
    d.add_argument('--repo', default='open-genie-server')
    d.add_argument('--commit', required=True)
    d.add_argument('--id', required=True)
    d.add_argument('--issue', required=True, help='file with the issue text given to the agent')
    r = sub.add_parser('run')
    r.add_argument('--alias', action='append', required=True)
    r.add_argument('--agent', choices=sorted(adapters.ADAPTERS), default=None,
                   help='which Agent runs the case (default: the baseline, pi). One Agent per run: results of '
                        'different Agents are never pooled (PA §12.5)')
    r.add_argument('--task', action='append', help='task id (default: every manifest)')
    trials = r.add_mutually_exclusive_group()
    trials.add_argument('--k', type=int, default=3, help='run trials 0 through k-1 (default: 3)')
    trials.add_argument('--trial', type=int, action='append', metavar='N',
                        help='run only this non-negative trial number; repeat to select several')
    r.add_argument('--provider-config', type=Path, help='evaluation-only local-infra provider JSON; included in case provenance')
    r.add_argument('--deadline-epoch', type=float, help='stop starting tasks unless timeout plus grading budget remains')
    r.add_argument('--retry-failed', action='store_true')
    r.add_argument('--path-wait', type=int, default=900,
                   help='seconds to keep waiting for a declared path that is not answering, before deferring that alias')
    r.add_argument('--path-poll', type=int, default=30, help='seconds between those checks')
    r.add_argument('--loop-guard', action='store_true', help='load the pi loop guard (separate cases and report rows)')
    r.add_argument('--thinking', choices=agent.THINKING, default='off',
                   help='pi thinking level; other than off needs a thinking switch (separate cases and report rows)')
    v = sub.add_parser('validate')
    v.add_argument('--task', action='append')
    f = sub.add_parser('report')
    f.add_argument('--failures', action='store_true',
                   help='classify the failures from the fields the runner stored, instead of the pass-rate table '
                        '(PA §10, T11.2): no trace is read')
    f.add_argument('--alias', help='with --failures: one alias only')
    f.add_argument('--task', help='with --failures: one task only')
    f.add_argument('--agent', help='with --failures: one Agent only (the baseline is pi)')
    f.add_argument('--against', metavar='ALIAS',
                   help='with --failures: which test ids fail for --alias and not for this one (the new '
                        'regression class PA §9.3 could not decide from aggregates)')
    f.add_argument('--json', action='store_true')
    args = parser.parse_args()
    if args.command == 'report' and args.against and not args.alias:
        parser.error('--against needs --alias: it compares one alias with another, and no alias means all of them')
    if args.command == 'run':
        if args.trial is not None:
            if any(trial < 0 for trial in args.trial):
                parser.error('--trial must be non-negative')
            args.trials = list(dict.fromkeys(args.trial))
        else:
            if args.k < 1:
                parser.error('--k must be positive')
            args.trials = range(args.k)
    if args.command in ('run', 'validate') and not TASKS.is_dir():
        raise SystemExit(explain(f'no task suite: {TASKS} does not exist',
                                 ['the suite belongs to the site; this site has not defined one'],
                                 f'ls {TASKS.parent}', 'add task manifests with `derive`, or run the suite in a site that has one'))
    if args.command in ('run', 'validate') and not args.task:
        args.task = sorted(p.stem for p in TASKS.glob('*.json'))
    {'derive': derive, 'validate': validate, 'run': run, 'report': report}[args.command](args)


if __name__ == '__main__':
    main()
