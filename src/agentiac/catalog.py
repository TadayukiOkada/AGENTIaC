"""One screen per target: what is declared, what has been observed, and what has been measured.

docs/platform-architecture.md §9.3 and execution plan T10.5/T10.6. `agentiac model list`,
`model show` and `model recommend` are the three readers of this module, and they all read the
same three blocks, which never speak for one another:

* **declared** — `declared.py`: the route target, its backend manifest, the inventory facts.
  This is what this checkout commits, so it is the only block that may say what something *is*.
* **state** — what a probe recorded, with the time it was read: the hardware record of the node
  and the target the gateway points at now. An unobserved value stays `unknown`; a declaration
  is never copied into it to make the screen look complete (T9.3 owns observed versions).
* **evidence** — the derived database of PA §9.2, read through its own ranking rule. A target
  with no rows says `no measurement`, which is a fact about the records and not an opinion
  about the model.

`model recommend` automates two of the proposal's seven steps and nothing else (PA §9.3):
**filter** on license, node, context, memory and disk, printing the reason for every candidate,
and **rank** the survivors against the current route with the Roadmap Phase 5 promotion rule.
It never downloads, never re-routes, and never builds a weighted score — the evidence exists
for coding only, and a weight over unmeasured uses would be a number with no reason behind it.
"""
import collections
import datetime
import json
import re
from pathlib import Path
import statistics

from agentiac import declared, evidence, hardware, identities, model_names, paths, route_aliases, updates
from agentiac.errors import command as cli_name

ROOT = paths.root()

#: the verbs of `agentiac model`; anything else is a target name, which is the T10.2 form
VERBS = ('list', 'show', 'names', 'compatibility', 'recommend')

#: PA §9.3: the only task with measurements today. Other uses get no recommendation, not a guess.
TASKS = ('coding',)

#: PA §9.3's three conclusions
CONCLUSIONS = ('incumbent', 'candidate', 'insufficient-evidence')

#: Roadmap Phase 5, quoted in the output so a screen cannot drift from the rule it applies
PROMOTION_RULE = ('pass rate not lower than the incumbent lower bound, no new regression class, '
                  'and 25% shorter median wall time or a capacity gain')

GIB = 2 ** 30
DEFAULT_SCRATCH_GIB = 15           # scripts/storage.py's default for a node with no declared worker
FLOOR_GIB = 40                     # what the owner keeps free at all times; scripts/storage.py and prepare both apply it
WALL_MARGIN = 0.75                 # "25% shorter", as the promotion rule words it

#: what the promotion rule reads; a task missing one of them cannot be set against anything
COMPARED = ('pass_rate', 'regression_pass_rate', 'wall_time_s')


def _moment(value):
    return datetime.datetime.fromisoformat(value) if value else None


def _age(iso, now):
    """How old a reading is, in seconds; None when there is no reading to age."""
    moment = _moment(iso)
    if moment is None:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return max(0, int((now - moment).total_seconds()))


def _weights(manifest):
    """The weights a manifest serves, from either manifest shape (Strata flat, TensorFold nested)."""
    model = manifest.get('model') if isinstance(manifest.get('model'), dict) else {}
    files = manifest.get('files') or model.get('files') or []
    return {'repository': manifest.get('model_repository') or model.get('repository') or identities.UNKNOWN,
            'revision': manifest.get('model_revision') or model.get('revision') or identities.UNKNOWN,
            'files': len(files), 'bytes': sum(f.get('size') or 0 for f in files),
            'sha256': sum(1 for f in files if f.get('sha256'))}


def _summarise(rows, why=None):
    """The evidence block: what may rank, what may not and why, and the middle of each measurement."""
    usable = [row for row in rows if evidence.usable(row)]
    metrics = {}
    for row in usable:
        metrics.setdefault(row['metric'], []).append(row)
    block = {'rows': len(rows), 'usable': len(usable), 'metrics': {},
             'excluded': sorted({evidence.unusable_reason(row) for row in rows if not evidence.usable(row)})}
    for name, group in sorted(metrics.items()):
        bounds = [row[key] for row in group for key in ('ci_low', 'ci_high') if row[key] is not None]
        block['metrics'][name] = {'value': round(statistics.median([row['value'] for row in group]), 4),
                                  'n': sum(row['n'] or 0 for row in group),
                                  'low': min(bounds) if bounds else None,
                                  'high': max(bounds) if bounds else None,
                                  'measured_at': max(row['measured_at'] for row in group)}
    if why:
        block['why'] = why
    return block


def _rows_for(root, config_id):
    """The rows measured under one configuration. A missing database is a reason, not an empty list."""
    try:
        return evidence.query(root, config_id=config_id), None
    except FileNotFoundError:
        return [], f'no evidence database; run {cli_name()} evidence import'


# --- the screen (T10.5) ---------------------------------------------------------------------------

def _observed(root):
    return updates.load_report(Path(root) / 'artifacts/components/observed.json')


def _observed_versions(report, node, backend_id):
    """State from `ops update observe` (T9.3), and never the declared value: without a reading it stays unknown."""
    versions = {'engine': updates.observed_engine(report, node, backend_id) or identities.UNKNOWN,
                'agent': identities.UNKNOWN}  # a target runs no single Agent; see `agent list`
    if report is None:
        return versions, f'no reading yet: run {cli_name()} ops update observe. A declared version is not repeated here'
    if versions['engine'] == identities.UNKNOWN:
        return versions, f"at {report['observed_at']} no front on {node} reported this target's manifest"
    return versions, f"from the front on {node} at {report['observed_at']}, its manifest matching this checkout"


def target_row(graph, target, *, identity=None, records=None, rows=(), why=None, observed=None, current=None, now=None):
    """One route target as the three blocks. `rows` are the measurements taken under its config_id."""
    spec = graph.route['targets'][target]
    identity = identity or identities.compute_target(graph, target)
    backend = spec.get('backend') or spec.get('source_alias')
    manifest = graph.backends.get(backend) or {}
    record = (records or {}).get(identity.node) or {}
    facts = record.get('facts', {})

    def fact(name):
        return (facts.get(name) or {}).get('value')

    total = fact('memory_total')
    versions, versions_why = _observed_versions(observed, identity.node, manifest.get('id'))
    return {
        'target': target, 'node': identity.node, 'current': target == current,
        'declared': {
            'source_alias': spec['source_alias'], 'backend': backend,
            'naming': model_names.describe(graph, target),
            'context_window': spec.get('context_window'), 'max_tokens': spec.get('max_tokens'),
            'thinking': spec.get('thinking'), 'thinking_start': spec.get('thinking_start'),
            'quantization': identity.class_inputs['quantization'],
            'engine': identity.class_inputs['engine'], 'weights': _weights(manifest),
            'workers': sorted((manifest.get('workers') or {}).keys()),
        },
        'identity': {'class_id': identity.class_id, 'config_id': identity.config_id,
                     'unknowns': identity.unknowns},
        'state': {
            'route': 'serving local-coder' if target == current else 'not serving',
            'hw_fp': record.get('hw_fp', identities.UNKNOWN),
            'gpu': fact('gpu') or identities.UNKNOWN, 'driver': fact('driver') or identities.UNKNOWN,
            'memory_total_gib': round(total / GIB, 1) if total else None,
            'collected_at': record.get('collected_at'), 'age_s': _age(record.get('collected_at'), now),
            'observed_versions': versions, 'observed_versions_why': versions_why,
        },
        'evidence': _summarise(rows, why),
    }


def _resolved(graph, current):
    """A saved or passed route target under a retired key names its new key (naming stage 2)."""
    try:
        return route_aliases.resolve_target(graph.route, current)
    except ValueError:
        return current


def targets(root=ROOT, *, current=None, now=None, graph=None):
    """Every declared route target as one row of the screen, in the order the route declares them."""
    graph = graph or declared.load(root)
    records = {host: hardware.latest(hardware.directory(root), host) for host in graph.nodes}
    current = _resolved(graph, current_target(root, graph) if current is None else current)
    observed = _observed(root)
    rows = []
    for target in graph.route['targets']:
        identity = identities.compute_target(graph, target)
        found, why = _rows_for(root, identity.config_id)
        rows.append(target_row(graph, target, identity=identity, records=records, rows=found, why=why,
                               observed=observed, current=current, now=now))
    return rows


def show(root=ROOT, target=None, *, current=None, now=None):
    """One target in full. `model show` prints this; `--json` is this, unchanged."""
    graph = declared.load(root)
    target = route_aliases.resolve_target(graph.route, target)
    current = _resolved(graph, current_target(root, graph) if current is None else current)
    identity = identities.compute_target(graph, target)
    found, why = _rows_for(root, identity.config_id)
    return target_row(graph, target, identity=identity,
                      records={identity.node: hardware.latest(hardware.directory(root), identity.node)},
                      rows=found, why=why, observed=_observed(root), current=current, now=now)


def current_target(root=ROOT, graph=None):
    """Which target local-coder points at now. scripts/route.py owns this; the caller passes it in."""
    graph = graph or declared.load(root)
    return graph.route['default']


# --- the generated document (T10.5, PA §5.1 L0) --------------------------------------------------

def compatibility(root=ROOT, *, graph=None):
    """`docs/compatibility.md`: the version ledger, generated.

    Deliberately declared-only. Observed versions and measurements are not copied in, because a
    copy of a measurement in a document is how a document starts lying (PA §10 says the same of
    handoff state: write the judgement, link the value). The screen reads those; this file records
    what this checkout commits, which is why the test can compare it byte for byte anywhere.
    """
    graph = graph or declared.load(root)
    identities_all = identities.compute_all(graph)
    lines = ['# Compatibility',
             '',
             'Generated by `agentiac model compatibility --write`. Do not edit by hand: every value',
             'below is read from the files named in the column, and `make check` fails when this file',
             'and those files disagree.',
             '',
             'Observed versions and measurements are **not** copied here on purpose. `agentiac model',
             'list` and `model show` read them with their age and their source; a version in a document',
             'is a claim about a machine, and this checkout has no probe that would earn it (T9.3).',
             '',
             '## What local-coder can be served',
             '',
             '| target | node | backend | quantization | context | max tokens | thinking | engine | weights '
             '| class_id | config_id |',
             '|---|---|---|---|---|---|---|---|---|---|---|']
    for target, identity in identities_all.items():
        spec = graph.route['targets'][target]
        declared_block = target_row(graph, target, identity=identity)['declared']
        engine = declared_block['engine']
        weights = declared_block['weights']
        lines.append('| {} | {} | {} | {} | {} | {} | {} | {} {} | {} {} | `{}` | `{}` |'.format(
            target, identity.node, declared_block['backend'], declared_block['quantization'],
            declared_block['context_window'], declared_block['max_tokens'],
            declared_block['thinking'] or 'off',
            engine['repository'].rsplit('/', 1)[-1], engine['version'],
            weights['repository'], weights['revision'][:12],
            identity.class_id, identity.config_id))

    lines += ['', '## What is not declared', '',
              'An id is only as good as the fields behind it. These are the fields a manifest does not '
              'name, so the ids above are stable but coarse, and re-declaring them changes the ids and '
              'costs a rerun (PA §6).', '']
    gaps = {target: identity.unknowns for target, identity in identities_all.items() if identity.unknowns}
    if not gaps:
        lines.append('nothing: every target declares every input of its two ids.')
    for target, unknowns in gaps.items():
        lines.append(f'- **{target}** — ' + '; '.join(sorted(set(unknowns.values()))))

    lines += ['', '## The Agents', '',
              '| agent | version | pinned by | gateway path | license |', '|---|---|---|---|---|']
    for agent in graph.agents:
        pin = _pin(root, agent)
        lines.append('| {} | {} | {} | {} | {} |'.format(
            agent['id'], pin.get('version', identities.UNKNOWN),
            Path(agent.get('pin') or agent.get('lock', '')).name, agent['endpoint'],
            pin.get('license_review', 'not reviewed')))

    lines += ['', '## Where each value came from', '',
              '| block | file |', '|---|---|',
              '| route targets | `gateway/local-coder.json` |',
              '| engine, weights, quantization, launch arguments | `backends/<id>.json` |',
              '| nodes and memory | `infra/inventory.yml` |',
              '| Agents and their pins | `agents/agents.yml`, `agents/*/pin.json`, `agents/pi/package-lock.json` |',
              '| the ids | `src/agentiac/identities.py` (PA §6) |', '']
    return '\n'.join(lines)


def _pin(root, agent):
    """The version record of an Agent: its byte pin, or the npm lock when it is pinned that way."""
    if agent.get('pin'):
        try:
            return json.loads(paths.locate(agent['pin'], root).read_text())
        except (OSError, ValueError):
            return {}
    try:
        lock = json.loads(paths.locate(agent['lock'], root).read_text())
    except (OSError, ValueError):
        return {}
    wanted = next(iter(lock.get('packages', {}).get('', {}).get('dependencies', {})), None)
    version = lock.get('packages', {}).get(f'node_modules/{wanted}', {}).get('version')
    return {'version': version, 'license_review': 'not reviewed; docs/license-review.json covers model licenses only'}


# --- the recommendation (T10.6, PA §9.3) ----------------------------------------------------------

def _check(name, status, reason=None):
    return {'check': name, 'status': status, 'reason': reason}


def _license_ledger(root):
    """docs/license-review.json, keyed by repository. It is the only place a license is recorded."""
    try:
        entries = json.loads((Path(root) / 'docs/license-review.json').read_text())
    except (OSError, ValueError):
        return {}
    return {entry['model']: entry for entry in entries if entry.get('model')}


def _check_license(row, ledger):
    repository = row['declared']['weights']['repository']
    if repository == identities.UNKNOWN:
        return _check('license', 'unknown', f"{row['declared']['backend']} declares no weight repository")
    entry = ledger.get(repository)
    if entry is None:
        return _check('license', 'unknown',
                      f'docs/license-review.json has no entry for {repository}: its license text has '
                      'never been fetched and hashed, so nothing may be promoted on it')
    if entry.get('detected_apache2') or entry.get('detected_mit'):
        return _check('license', 'ok')
    review = entry.get('owner_review')
    if review is None:
        return _check('license', 'excluded', f'{repository} is reviewed and is neither Apache-2.0 nor MIT')
    problem = owner_review_problem(review)
    if problem:
        return _check('license', 'excluded',
                      f'{repository} is neither Apache-2.0 nor MIT and its owner_review is refused: {problem}')
    return _check('license', 'ok',
                  f'{repository} is neither Apache-2.0 nor MIT; accepted by the owner on {review["decided_at"]} '
                  f'for: {review["scope"]}')


OWNER_REVIEW_KEYS = {'status', 'decided_by', 'decided_at', 'scope', 'note'}


def owner_review_problem(review):
    """None when a ledger entry's `owner_review` names who accepted a licence that is not Apache-2.0 or MIT, when,
    and for what use; otherwise the reason it does not. A malformed review is no review: the entry stays excluded.

    The ledger records the text; this records the one decision the text alone cannot make. The scope is shown
    wherever the licence check is, so an acceptance for internal use does not read as a general one.
    """
    if not isinstance(review, dict):
        return 'it is not an object'
    missing, extra = OWNER_REVIEW_KEYS - review.keys(), review.keys() - OWNER_REVIEW_KEYS
    if missing or extra:
        return f'missing {sorted(missing)}, unexpected {sorted(extra)}'
    if review['status'] != 'manual-approved':
        return f"status is {review['status']!r}, not 'manual-approved'"
    for key in ('decided_by', 'scope', 'note'):
        if not isinstance(review[key], str) or not review[key].strip():
            return f'{key} is empty'
    when = review['decided_at']
    if not isinstance(when, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', when):
        return 'decided_at is not YYYY-MM-DD'
    try:
        datetime.date.fromisoformat(when)
    except ValueError:
        return 'decided_at is not a calendar date'
    return None


def _check_node(row, wanted):
    if wanted is not None and row['node'] != wanted:
        return _check('node', 'excluded', f'served on {row["node"]}, not on {wanted}')
    workers = row['declared']['workers']
    if workers and row['node'] not in workers:
        return _check('node', 'excluded', f'backends/{row["declared"]["backend"]}.json declares workers '
                                          f'{", ".join(workers)} but the route serves this target on {row["node"]}')
    return _check('node', 'ok')


def _check_context(row, required):
    served = row['declared']['context_window']
    if served is None:
        return _check('context', 'unknown', 'the route target declares no context_window')
    if served < required:
        return _check('context', 'excluded', f'serves {served} of context; the route gives {required} today')
    return _check('context', 'ok')


def _check_memory(row, graph, worker):
    """The gate a claim would apply, checked against the only memory figure held offline: the total."""
    need, where = worker.get('min_available_gib'), f'backends/{row["declared"]["backend"]}.json (measured)'
    if need is None:
        need = (graph.nodes.get(row['node']) or {}).get('min_available_gib')
        where = 'infra/inventory.yml (declared)'
    if need is None:
        return _check('memory', 'unknown', f'neither the manifest nor the inventory declares min_available_gib for {row["node"]}')
    total = row['state']['memory_total_gib']
    if total is None:
        return _check('memory', 'unknown', f'no hardware record for {row["node"]}; run {cli_name()} ops inventory {row["node"]}')
    if total < need:
        return _check('memory', 'excluded', f'needs {need} GiB available ({where}) and {row["node"]} has '
                                            f'{total} GiB of memory in total')
    return _check('memory', 'ok', f'{need} GiB needed of {total} GiB total, {where}')


def _check_disk(row, worker, storage):
    weights = row['declared']['weights']['bytes'] / GIB
    if not weights:
        return _check('disk', 'unknown', f"{row['declared']['backend']} declares no file sizes")
    scratch = worker.get('scratch_gib', DEFAULT_SCRATCH_GIB)
    reading = (storage or {}).get(row['node']) or {}
    if not reading.get('reachable'):
        return _check('disk', 'unknown', f'no `ops storage` reading for {row["node"]}; the disk gate was not applied')
    free = reading.get('free_gib')
    if free is None or reading.get('roots') == []:   # no declared root exists yet, so its 0.0 measured nothing
        return _check('disk', 'unknown', f'`ops storage` reported no free space for {row["node"]}')
    if free < weights + scratch + FLOOR_GIB:
        # `ops storage` measures a root and does not list it, so a root that could hold the weights neither
        # proves they are there nor that a download is needed. `excluded` is an answer on evidence only.
        holders = [root['path'] for root in reading.get('roots') or () if root['gib'] >= weights]
        if holders:
            return _check('disk', 'unknown', f'{free} GiB free is short of {weights:.1f} GiB of weights plus '
                                             f'{scratch} GiB of scratch and the {FLOOR_GIB} GiB floor, but '
                                             f'{", ".join(holders)} is large enough '
                                             'to hold them already; `ops storage` does not read file names')
        return _check('disk', 'excluded', f'the weights are {weights:.1f} GiB plus {scratch} GiB of scratch and '
                                          f'the {FLOOR_GIB} GiB floor, and {row["node"]} has {free} GiB free')
    return _check('disk', 'ok', f'{weights:.1f} GiB of weights, {scratch} GiB of scratch and the {FLOOR_GIB} GiB '
                                f'floor fit in {free} GiB free')


def filters(row, graph, *, node=None, required_context=0, storage=None, ledger=None):
    """PA §9.3's filter. Every check answers, so the screen can print a reason for every candidate.

    A check answers `excluded` only on evidence. When there is nothing to check against it answers
    `unknown`, which does not remove the candidate but does keep it out of the conclusion: an
    unreviewed license or an unmeasured disk is not a pass, it is a gap, and PA §9.3 automates
    filter and rank, not the benefit of the doubt.
    """
    manifest = graph.backends.get(row['declared']['backend']) or {}
    worker = (manifest.get('workers') or {}).get(row['node'], {})
    ledger = _license_ledger(graph.root) if ledger is None else ledger
    return [_check_license(row, ledger), _check_node(row, node), _check_context(row, required_context),
            _check_memory(row, graph, worker), _check_disk(row, worker, storage)]


def _measured(root, config_id):
    """One configuration's rows that may rank, by (Agent, Agent version, task), or None with no database.

    A `config_id` says what served the model and not who drove it or on what, so a pool across this key
    would compare different work. A key that lacks one of the compared metrics is left out.
    """
    found = collections.defaultdict(dict)
    for name in COMPARED:
        try:
            rows = evidence.query(root, config_id=config_id, metric=name)
        except FileNotFoundError:
            return None
        for row in rows:
            if evidence.usable(row):
                found[row['agent'], row['agent_version'], row['workload']].setdefault(name, []).append(row)
    return {key: metrics for key, metrics in found.items() if len(metrics) == len(COMPARED)}


def _pooled(measured, keys):
    """The pooled measurement over exactly these keys.

    Rates are pooled over their counts, which is what a whole-suite pass rate means; wall time is
    the median of the per-task medians, because the rows are per task and no row holds the raw
    attempts. Both are stated in the output rather than presented as the run itself.
    """
    out = {}
    for name in COMPARED:
        rows = [row for key in keys for row in measured[key][name]]
        counts = sum(row['n'] or 0 for row in rows)
        if name.endswith('pass_rate'):
            passed = sum((row['value'] or 0.0) * (row['n'] or 0) for row in rows)
            low, high = evidence.wilson(passed, counts)
            out[name] = {'value': round(passed / counts, 4) if counts else None, 'n': counts,
                         'low': low, 'high': high}
        else:
            values = [row['value'] for row in rows]
            bounds = [row[key] for row in rows for key in ('ci_low', 'ci_high') if row[key] is not None]
            out[name] = {'value': round(statistics.median(values), 3), 'n': counts,
                         'low': min(bounds) if bounds else min(values),
                         'high': max(bounds) if bounds else max(values)}
    return out


def compare(candidate, incumbent, root):
    """The Roadmap Phase 5 promotion rule, one condition at a time, over rows that may rank (X21).

    `no new regression class` is measured as the aggregate regression pass rate, because that is
    all the Evidence database stores; the failing test *names* are in the trace, and comparing
    those is T11.2's job. The output says so rather than claiming the stronger check.

    Both sides are pooled over the same work only: the tasks each ran with the same Agent at the same
    version. What only one side ran is listed, and `_conclude` will not promote a candidate that left
    out any of the incumbent's tasks.
    """
    candidate_rows = _measured(root, candidate['identity']['config_id'])
    incumbent_rows = _measured(root, incumbent['identity']['config_id'])
    if not candidate_rows or not incumbent_rows:
        missing = [name for name, block in (('candidate', candidate_rows), ('incumbent', incumbent_rows))
                   if not block]
        return {'state': 'insufficient-evidence', 'missing_for': missing, 'conditions': {}, 'passes': False,
                'shared_tasks': [], 'candidate_only': [], 'incumbent_only': [],
                'note': 'no usable rows for one of the two configurations; a measurement nobody can rank '
                        'is not a comparison (X21)'}
    shared = sorted(set(candidate_rows) & set(incumbent_rows))
    coverage = {'shared_tasks': [_label(key) for key in shared],
                'candidate_only': [_label(key) for key in sorted(set(candidate_rows) - set(shared))],
                'incumbent_only': [_label(key) for key in sorted(set(incumbent_rows) - set(shared))]}
    if not shared:
        return {'state': 'insufficient-evidence', 'missing_for': [], 'conditions': {}, 'passes': False, **coverage,
                'note': 'the two configurations share no task run by the same Agent at the same version, '
                        'so their numbers describe different work'}
    mine, theirs = _pooled(candidate_rows, shared), _pooled(incumbent_rows, shared)
    conditions = {
        'pass_rate': {'ok': mine['pass_rate']['value'] >= theirs['pass_rate']['low'],
                      'candidate': mine['pass_rate']['value'], 'incumbent': theirs['pass_rate']['value'],
                      'bound': theirs['pass_rate']['low']},
        'no_new_regression_class': {'ok': mine['regression_pass_rate']['value'] >= theirs['regression_pass_rate']['low'],
                                    'candidate': mine['regression_pass_rate']['value'],
                                    'incumbent': theirs['regression_pass_rate']['value'],
                                    'bound': theirs['regression_pass_rate']['low'],
                                    'measured_as': 'aggregate regression pass rate; the failing test names '
                                                   'belong to eval report --failures (T11.2)'},
        'wall_time_s': {'ok': mine['wall_time_s']['value'] <= WALL_MARGIN * theirs['wall_time_s']['value'],
                        'candidate': mine['wall_time_s']['value'], 'incumbent': theirs['wall_time_s']['value'],
                        'bound': round(WALL_MARGIN * theirs['wall_time_s']['value'], 3)},
        'capacity_gain': {'ok': (candidate['declared']['context_window'] or 0) >
                                (incumbent['declared']['context_window'] or 0),
                          'candidate': candidate['declared']['context_window'],
                          'incumbent': incumbent['declared']['context_window']},
    }
    passes = (conditions['pass_rate']['ok'] and conditions['no_new_regression_class']['ok']
              and (conditions['wall_time_s']['ok'] or conditions['capacity_gain']['ok']))
    return {'state': 'compared', 'conditions': conditions, 'passes': passes, **coverage,
            'note': 'rollback is not tested here: a promotion is an operator action, and this command '
                    'proposes it and stops (PA §9.3)'}


def _label(key):
    agent, version, task = key
    return f'{task} ({agent} {version})'


def recommend(root=ROOT, *, node=None, task='coding', context=None, storage=None, current=None, now=None):
    """`agentiac model recommend`: filter, then rank, then one of three conclusions. Never anything else."""
    if task not in TASKS:
        raise ValueError(f'--task {task} has no evidence; the tasks with measurements today are {", ".join(TASKS)}')
    graph = declared.load(root)
    current = _resolved(graph, current_target(root, graph) if current is None else current)
    if current not in graph.route['targets']:
        raise ValueError(f'local-coder points at {current}, which the route does not declare')
    incumbent = show(root, current, current=current, now=now)
    required = context if context is not None else (incumbent['declared']['context_window'] or 0)
    ledger = _license_ledger(root)
    candidates = []
    for row in targets(root, current=current, now=now, graph=graph):
        entry = {'target': row['target'], 'node': row['node'], 'incumbent': row['target'] == current,
                 'declared': row['declared'], 'identity': row['identity'], 'evidence': row['evidence']}
        entry['filters'] = filters(row, graph, node=node, required_context=required,
                                   storage=storage, ledger=ledger)
        entry['excluded'] = [check for check in entry['filters'] if check['status'] == 'excluded']
        entry['gaps'] = [check for check in entry['filters'] if check['status'] == 'unknown']
        entry['comparison'] = None if entry['incumbent'] else compare(row, incumbent, root)
        candidates.append(entry)
    return {'schema_version': 1, 'task': task, 'node': node, 'required_context': required,
            'rule': PROMOTION_RULE, 'incumbent': _brief(incumbent), 'conclusion': _conclude(candidates),
            'candidates': candidates, 'recommended': None}


def _brief(row):
    return {'target': row['target'], 'node': row['node'], 'config_id': row['identity']['config_id'],
            'context_window': row['declared']['context_window']}


def _conclude(candidates):
    """PA §9.3's three values. The filter deciding is a decision; having nothing to compare is not.

    `incumbent` when nothing survived the filter (the filter answered, and the answer was no) or
    when everything that survived was compared and failed the promotion rule; `candidate` when one
    of them passed with every gate read and every task of the incumbent's run; otherwise
    `insufficient-evidence`, because a survivor nobody compared could still be the one that wins.
    """
    survivors = [entry for entry in candidates if not entry['excluded'] and not entry['incumbent']]
    if not survivors:
        return 'incumbent'
    compared = [entry for entry in survivors if entry['comparison']['state'] == 'compared']
    winners = [entry for entry in compared if entry['comparison']['passes']]
    if any(not entry['gaps'] and not entry['comparison']['incumbent_only'] for entry in winners):
        return 'candidate'
    if winners:
        # something would win if the gates had been read or the incumbent's tasks had all been run: the
        # honest answer is that the records cannot settle it, not that the current route has been defended
        return 'insufficient-evidence'
    return 'incumbent' if len(compared) == len(survivors) else 'insufficient-evidence'
