"""Evidence: the derived DB of PA §9.2 (T10.4) — what has been measured, under which keys.

The suite DB (`artifacts/suite/state.sqlite`) is the primary record and is never touched
here; this module reads it and writes `artifacts/evidence/evidence.sqlite`, which PA §5.2
lists as *regenerable*: delete it and `agentiac evidence import` rebuilds it from the
same sources, byte for byte in the columns that matter. Nothing is authored into it by
hand except what PA §9.2 says must be hand-written — the alias-to-configuration mapping in
`evidence/alias-history.yml` and the transcribed values in `evidence/reports.yml`, both of
which are validated before a single row is written.

Three rules decide whether a row may be believed, and they are the completion conditions
of T10.4:

* **A row is only as good as its keys.** A measurement carries `class_id`, `config_id` and
  `hw_fp` from one of two places: recorded by T10.3 in the attempt's own result
  (`identity: recorded`), or mapped by an `evidence/alias-history.yml` entry whose window
  contains the measurement (`identity: history`). Neither → `identity: unknown` with a
  reason. An unknown row is kept and displayed, and never used to rank (X21).
* **A key that moved makes the row stale, not deleted.** At import the declared graph and
  the hardware inventory are read again; a row whose `class_id`/`config_id` no longer
  follows from the declaration, or whose `hw_fp` no longer matches the inventory record of
  its node, keeps its values and gains a `stale_reason`. Re-measuring is a decision, not a
  side effect of an edit (Roadmap: no evaluation without a decision). A keyed row that cannot
  be checked at all gains one too: a row may rank because its keys were confirmed, not
  because nothing looked.
* **Confidence is recorded, not implied.** `suite` (the task suite), `probe` (a performance
  run), `report` (a value transcribed from `docs/results/`) and `published` (a vendor
  number). `published` is reference only and never ranks, and speed and quality are always
  separate rows: PA §3's slowest-decoding machine is the fastest at the task.

An id computed over undeclared inputs is not an identity: when no backend manifest names a
target's weights, distinct targets hash the same `unknown` inputs and collide on one `class_id`.
`validate_history` refuses to bind measurements to such an id, so unmanifested historical
attempts stay `unknown` instead of becoming false evidence.
"""
import collections
import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
import tempfile
import os

import yaml

from agentiac import declared, failures, hardware, identities, paths
from agentiac.errors import command as cli_name

ROOT = paths.root()

#: where the measurement came from; `confidence` says how much to believe it
SOURCES = ('suite', 'ladder', 'report')

#: PA §9.2's confidence ladder. `published` is never ranked; `unknown` marks a row nobody believes yet.
CONFIDENCE = ('suite', 'probe', 'report', 'published', 'unknown')
RANKING_CONFIDENCE = ('suite', 'probe', 'report')

#: PA §9.2's metric list. Speed and quality are different rows of the same run, never one score.
METRICS = ('pass_rate', 'regression_pass_rate', 'wall_time_s', 'tool_errors_per_run',
           'ttft_s', 'prefill_tok_s', 'decode_tok_s', 'answer_pass_rate',
           'min_available_gib', 'peak_vram_gib')

#: a measurement with no Agent in front of it (the ladder talks to the backend directly)
NO_AGENT = 'none'

#: how a row got its keys
IDENTITIES = ('recorded', 'history', 'unknown')

#: the fields an id is taken over: two rows share an id exactly when they are the same measurement
ID_FIELDS = ('config_id', 'class_id', 'hw_fp', 'agent', 'agent_version', 'workload', 'metric',
             'source', 'source_ref', 'measured_at')

COLUMNS = ('id', 'config_id', 'class_id', 'hw_fp', 'node', 'target', 'agent', 'agent_version',
           'workload', 'metric', 'value', 'n', 'ci_low', 'ci_high', 'source', 'source_ref',
           'confidence', 'measured_at', 'stale_reason', 'identity', 'identity_reason')

SCHEMA = '''
CREATE TABLE evidence (
  id TEXT PRIMARY KEY,
  config_id TEXT NOT NULL, class_id TEXT NOT NULL, hw_fp TEXT NOT NULL,
  node TEXT, target TEXT,
  agent TEXT NOT NULL, agent_version TEXT NOT NULL,
  workload TEXT NOT NULL, metric TEXT NOT NULL,
  value REAL, n INTEGER, ci_low REAL, ci_high REAL,
  source TEXT NOT NULL, source_ref TEXT NOT NULL,
  confidence TEXT NOT NULL, measured_at TEXT NOT NULL,
  stale_reason TEXT,
  identity TEXT NOT NULL, identity_reason TEXT
);
CREATE TABLE imports (
  imported_at TEXT NOT NULL, sources TEXT NOT NULL, rows INTEGER NOT NULL,
  usable INTEGER NOT NULL, unknown INTEGER NOT NULL, stale INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_key ON evidence(config_id, metric, workload);
'''


def wilson(passed, total, z=1.96):
    """Wilson 95% interval of a pass rate — the same formula `scripts/suite.py` reports with.

    Duplicated rather than imported because this module must stay importable from a venv
    without the gateway, docker or pi in it; `test_wilson_matches_the_suites` pins the two
    against each other so they cannot drift.
    """
    if not total:
        return (0.0, 0.0)
    p = passed / total
    centre, half = p + z * z / (2 * total), z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (round((centre - half) / (1 + z * z / total), 3), round((centre + half) / (1 + z * z / total), 3))


def _spread(values):
    """The observed extremes of a measurement, or None when one sample says nothing about spread.

    These are *not* a confidence interval of the mean: with n=3 repetitions of a ladder
    depth, min and max are the honest description, and calling them a CI would borrow a
    precision the sample does not have.
    """
    if len(values) < 2:
        return (None, None)
    return (round(min(values), 4), round(max(values), 4))


def _iso(stamp):
    """UTC ISO seconds from a unix timestamp; the one time format the DB stores."""
    return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).isoformat(timespec='seconds')


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _moment(value):
    """An aware datetime from an ISO string or a YAML timestamp; ValueError when it is neither.

    PyYAML reads an unquoted ISO-8601 `from:` timestamp as a `datetime`, not a string, so a
    validator that only accepted text would reject a correct file. A naive timestamp is read
    as UTC: the windows are wall-clock facts about a run, and an unmarked one is nobody's local time.
    """
    if isinstance(value, str):
        value = datetime.datetime.fromisoformat(value)
    if not isinstance(value, datetime.datetime):
        raise ValueError(f'{value!r} is not a timestamp')
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def evidence_id(row):
    """The row's identity: the same measurement re-imported is the same row, however often it is rebuilt."""
    key = {name: row[name] for name in ID_FIELDS}
    return hashlib.sha256(json.dumps(key, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]


# --- where the keys come from -------------------------------------------------------------------

def validate_history(entries, graph=None):
    """Every way a hand-written alias-history entry can be a guess, as strings; empty means it may bind rows.

    The file is the one place a human asserts what an alias used to point at, so the checks
    are the ones that catch a typo or a lazy copy: an id must look like an id, a window must
    be a window, two windows of one alias may not disagree about what it was, and every entry
    must say how its author knows.
    """
    problems = []
    seen = {}
    for index, entry in enumerate(entries or []):
        where = f'entry {index}'
        if not isinstance(entry, dict):
            problems.append(f'{where}: not a mapping')
            continue
        unknown_keys = sorted(set(entry) - {'alias', 'from', 'to', 'node', 'target',
                                            'class_id', 'config_id', 'hw_fp', 'why'})
        if unknown_keys:
            problems.append(f"{where}: unknown field(s) {', '.join(unknown_keys)}")
        alias = entry.get('alias')
        if not alias:
            problems.append(f'{where}: no alias')
            continue
        where = f'alias {alias}'
        if not entry.get('why'):
            problems.append(f'{where}: no why — a mapping nobody can cite is a guess')
        try:
            start = _moment(entry['from'])
            end = _moment(entry['to'])
        except (KeyError, TypeError, ValueError):
            problems.append(f'{where}: from/to is not a pair of ISO timestamps')
            continue
        if end <= start:
            problems.append(f'{where}: to {entry["to"]} is not after from {entry["from"]}')
        ids = [entry.get(name) for name in ('class_id', 'config_id', 'hw_fp')]
        given = [name for name, value in zip(('class_id', 'config_id', 'hw_fp'), ids) if value]
        if given and len(given) != 3:
            problems.append(f'{where}: {", ".join(given)} given without the others — '
                            'a row needs all three keys or none (an id computed over missing inputs is not an id)')
        for name, value in zip(('class_id', 'config_id', 'hw_fp'), ids):
            if value and not (isinstance(value, str) and len(value) == 16 and all(c in '0123456789abcdef' for c in value)):
                problems.append(f'{where}: {name} {value!r} is not 16 hex digits')
        bucket = seen.setdefault(alias, [])
        for other in bucket:
            if start < other['to'] and other['from'] < end:     # `resolve_identity` reads any overlap as ambiguous
                problems.append(f'{where}: window {entry["from"]}..{entry["to"]} overlaps '
                                f'{other["from"]}..{other["to"]} of the same alias')
        bucket.append(dict(entry, **{'from': start, 'to': end}))
        target = entry.get('target')
        if graph is not None and target and target not in graph.route['targets']:
            # a history entry keeps the key it was written with; a retired key resolves (naming stage 2)
            target = graph.route.get('legacy_targets', {}).get(target) if graph.route.get('version', 1) >= 2 else None
        if graph is not None and target and entry.get('class_id') and target in graph.route['targets']:
            # the id of a target whose declaration names no weights is the id of every such target
            empty = identities.compute_target(_Stripped(graph, target), target)
            if entry['class_id'] == empty.class_id:
                problems.append(f'{where}: class_id {entry["class_id"]} is what the declaration of '
                                f'{entry["target"]} hashes with no backend manifest — it does not name the weights')
    return problems


class _Stripped:
    """A declared graph whose one target has no backend manifest: what the id would be if nothing were declared."""

    def __init__(self, graph, target):
        self._graph = graph
        self.route = {'alias': graph.route['alias'], 'default': graph.route['default'],
                      'targets': {target: dict(graph.route['targets'][target], backend=None,
                                               source_alias='__undeclared__')}}
        self.backends = {}

    def __getattr__(self, name):
        return getattr(self._graph, name)


def load_history(root=ROOT, graph=None):
    """The hand-written alias-to-configuration windows of `evidence/alias-history.yml`; [] when the file is absent.

    The declared graph is read here, not passed in by every caller, because the file's
    honesty is a property of the declaration it claims to describe: an entry that recites the
    all-unknown id of a target with no manifest is a guess about the weights, and only the
    declaration can say so. A checkout whose declarations cannot be read loses that one check
    and keeps every other.
    """
    path = Path(root) / 'evidence/alias-history.yml'
    if not path.exists():
        return []
    entries = yaml.safe_load(path.read_text()) or []
    if graph is None:
        try:
            graph = declared.load(root)
        except Exception:
            graph = None
    problems = validate_history(entries, graph)
    if problems:
        raise ValueError('evidence/alias-history.yml: ' + '; '.join(problems))
    return entries


def resolve_identity(measurement, history, now=None):
    """The PA §6 keys of one measurement: recorded by the run itself, else mapped by history, else unknown.

    Never a guess from today's declaration: an earlier run is not served by what
    `gateway/local-coder.json` says today, and recomputing it would silently re-key history.
    """
    provenance = (measurement.get('provenance') or {})
    recorded = [provenance.get(name) for name in ('class_id', 'config_id', 'hw_fp')]
    if all(recorded):
        return {'config_id': provenance['config_id'], 'class_id': provenance['class_id'],
                'hw_fp': provenance['hw_fp'], 'node': provenance.get('node'),
                'target': provenance.get('target'), 'identity': 'recorded', 'identity_reason': None}
    when = measurement['measured_at']
    moment = _moment(when)
    matches = [entry for entry in history or []
               if entry.get('alias') == measurement['alias']
               and _moment(entry['from']) <= moment < _moment(entry['to'])]
    if len(matches) > 1:
        return _unknown(f'{len(matches)} alias-history entries cover {measurement["alias"]} at {when}; '
                        'the mapping is ambiguous')
    if len(matches) == 1:
        entry = matches[0]
        if entry.get('class_id'):
            return {'config_id': entry['config_id'], 'class_id': entry['class_id'], 'hw_fp': entry['hw_fp'],
                    'node': entry.get('node') or measurement.get('node'), 'target': entry.get('target'),
                    'identity': 'history', 'identity_reason': None}
        return _unknown(f'alias-history entry of {measurement["alias"]} carries no keys: {entry.get("why")}')
    return _unknown(measurement.get('identity_reason')
                    or 'the run recorded no provenance and no alias-history entry covers this alias and time')


def _unknown(reason):
    return {'config_id': identities.UNKNOWN, 'class_id': identities.UNKNOWN, 'hw_fp': identities.UNKNOWN,
            'node': None, 'target': None, 'identity': 'unknown', 'identity_reason': reason}


# --- the sources --------------------------------------------------------------------------------

def _latest_attempts(db):
    """The last attempt of every case, as the suite report reads it: history is kept, the answer is the latest."""
    rows = db.execute('''SELECT c.id case_id, c.spec spec, a.number number, a.status status,
                                a.result result, a.finished finished, a.started started
                         FROM cases c JOIN attempts a ON a.case_id = c.id AND a.number = c.attempts
                         WHERE a.status IN ('complete', 'failed')''')
    return [dict(row) for row in rows]


def suite_rows(root=ROOT, history=(), path=None):
    """Quality and speed rows of the task suite: one row per (keys, agent, task, metric).

    Grouping is by the resolved keys, never by the alias: an alias that changed configuration
    mid-history splits into two groups on its own, and two aliases that were replicas of one
    configuration on one node join. Infrastructure failures are not measurements — they are
    counted in the import summary and produce no row, the same denominator the suite report uses.
    A case whose task manifest has been edited since it ran is not the task the group is about, so
    it is left out and counted the same way, as `eval suite report` and `eval report --failures` do.
    """
    path = Path(path) if path else Path(root) / 'artifacts/suite/state.sqlite'
    if not path.exists():
        return [], {'source': 'suite', 'ref': str(path.relative_to(root) if _under(path, root) else path),
                    'cases': 0, 'skipped': {}}
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        attempts = _latest_attempts(db)
    groups, skipped = {}, collections.Counter()
    current = failures.manifests(root)
    for row in attempts:
        spec = json.loads(row['spec'])
        result = json.loads(row['result'] or '{}')
        if current.get(spec.get('task')) != spec.get('task_sha256'):
            skipped['superseded'] += 1
            continue
        if 'infrastructure_error' in result:
            skipped['infrastructure_error'] += 1
            continue
        if result.get('passed') is None:
            skipped['unclassified'] += 1
            continue
        alias = spec.get('alias') or identities.UNKNOWN
        # The suite report's own split (`label`): a run with thinking or the loop guard is a different run of
        # the task, and `class_id` hashes the thinking switch, not the level.
        workload = (spec.get('task') or identities.UNKNOWN) + (' +loop-guard' if spec.get('loop_guard') else '') \
            + (f" +thinking-{spec['thinking']}" if spec.get('thinking') else '')
        finished = row['finished'] or row['started']
        provenance = result.get('provenance') or {}
        agent = provenance.get('agent') or {}
        name, version = agent.get('name'), agent.get('version')
        if not name:  # a case from before T10.3 names its Agent in the spec, which is what was pinned then
            frontend = spec.get('frontend') or NO_AGENT
            name, _, version = frontend.rpartition(' ')
            name, version = (name or frontend), (version or 'unrecorded')
        measurement = {'alias': alias, 'measured_at': _iso(finished), 'provenance': provenance}
        keys = resolve_identity(measurement, history)
        if keys['identity'] == 'unknown':       # no keys in common is not one configuration: each alias stays apart
            keys = dict(keys, identity_reason=f"{alias}: {keys['identity_reason']}")
        group = (keys['config_id'], keys['class_id'], keys['hw_fp'], name, version, workload,
                 alias if keys['identity'] == 'unknown' else None)
        groups.setdefault(group, {'keys': keys, 'agent': name, 'agent_version': version,
                                  'workload': workload,
                                  'measured_at': _iso(finished), 'cases': [], 'results': []})
        entry = groups[group]
        entry['cases'].append(row['case_id'])
        entry['results'].append(result)
        entry['measured_at'] = max(entry['measured_at'], _iso(finished))
    rows = []
    for entry in groups.values():
        results = entry['results']
        scored = [r for r in results if r.get('passed') is not None]
        cases = ','.join(sorted(c[:12] for c in entry['cases']))
        base = {'source': 'suite', 'confidence': 'suite', 'source_ref': f'artifacts/suite/state.sqlite#{cases}',
                'measured_at': entry['measured_at'], 'agent': entry['agent'], 'agent_version': entry['agent_version'],
                'workload': entry['workload'], **entry['keys']}
        if scored:
            passed = sum(1 for r in scored if r['passed'])
            low, high = wilson(passed, len(scored))
            rows.append({**base, 'metric': 'pass_rate', 'value': round(passed / len(scored), 4),
                         'n': len(scored), 'ci_low': low, 'ci_high': high})
        checks = [(r.get('regression_total') or 0, len(r.get('regression_failed') or [])) for r in scored]
        total, failed = sum(check[0] for check in checks), sum(check[1] for check in checks)
        if total:
            low, high = wilson(total - failed, total)
            rows.append({**base, 'metric': 'regression_pass_rate',
                         'value': round(1 - failed / total, 6), 'n': total, 'ci_low': low, 'ci_high': high})
        walls = [v for v in (_number(r.get('wall_s')) for r in scored) if v is not None]
        if walls:
            low, high = _spread(walls)
            rows.append({**base, 'metric': 'wall_time_s', 'value': round(statistics.median(walls), 3),
                         'n': len(walls), 'ci_low': low, 'ci_high': high})
        errors = [v for v in (_number(r.get('tool_errors')) for r in scored) if v is not None]
        if errors:
            low, high = _spread(errors)
            rows.append({**base, 'metric': 'tool_errors_per_run', 'value': round(statistics.mean(errors), 3),
                         'n': len(errors), 'ci_low': low, 'ci_high': high})
    return rows, {'source': 'suite', 'ref': 'artifacts/suite/state.sqlite',
                  'cases': len(attempts), 'skipped': dict(skipped)}


def ladder_rows(root=ROOT, history=()):
    """Speed rows of the context ladder (`artifacts/ladder/<label>/`): TTFT, prefill, decode, answer rate.

    These runs talk to a backend directly, so no Agent ran and no PA §6 key was recorded —
    the manifest names a source alias and a context, which is not enough to tell two
    configurations apart (the RTX 3090 host's two 128K labels differ in something the runner never
    recorded). They are imported as `identity: unknown`: visible, never ranked, and a
    standing prompt to bind them in `evidence/alias-history.yml` if a decision ever needs them.
    """
    rows, count, workers = [], 0, _workers(root)
    for folder in sorted((Path(root) / 'artifacts/ladder').glob('*')) if (Path(root) / 'artifacts/ladder').is_dir() else []:
        manifest, results = folder / 'manifest.json', folder / 'results.jsonl'
        if not (manifest.exists() and results.exists()):
            continue
        options = json.loads(manifest.read_text())
        cases = [json.loads(line) for line in results.read_text().splitlines() if line.strip()]
        done = {}
        for case in cases:  # a rerun appends; the last word on a case is the one that stands
            done[case['case']] = case
        ok = [case for case in done.values() if case.get('status') == 'ok']
        count += len(done)
        for depth in sorted({case['depth'] for case in ok}):
            group = [case for case in ok if case['depth'] == depth]
            measurement = {'alias': options.get('alias') or identities.UNKNOWN,
                           'measured_at': max(case['finished'] for case in group if case.get('finished'))
                           if any(case.get('finished') for case in group) else _iso(0),
                           'node': None}
            keys = resolve_identity(measurement, history)
            keys['node'] = keys['node'] or _node_of(options.get('alias'), workers)
            base = {'source': 'ladder', 'confidence': 'probe', 'identity_reason': keys['identity_reason'],
                    'source_ref': f'artifacts/ladder/{folder.name}/results.jsonl#{depth}',
                    'measured_at': measurement['measured_at'], 'agent': NO_AGENT, 'agent_version': NO_AGENT,
                    'workload': f'context-{depth}', **keys}
            for metric, key in (('ttft_s', 'cold_ttft_s'), ('prefill_tok_s', 'prefill_tok_s'),
                                ('decode_tok_s', 'decode_tok_s')):
                values = [v for v in (_number(case.get(key)) for case in group) if v is not None]
                if values:
                    low, high = _spread(values)
                    rows.append({**base, 'metric': metric, 'value': round(statistics.median(values), 3),
                                 'n': len(values), 'ci_low': low, 'ci_high': high})
            answered = [case for case in group if 'answer_passed' in case]
            if answered:
                passed = sum(1 for case in answered if case['answer_passed'])
                low, high = wilson(passed, len(answered))
                rows.append({**base, 'metric': 'answer_pass_rate', 'value': round(passed / len(answered), 4),
                             'n': len(answered), 'ci_low': low, 'ci_high': high})
    return rows, {'source': 'ladder', 'ref': 'artifacts/ladder', 'cases': count, 'skipped': {}}


def _workers(root):
    """(name, host) for every worker (every host but the local one) and every former name of one, longest name first;
    [] for a root without an inventory. An alias recorded before a rename carries the old name (T8.10)."""
    hosts = declared.hosts(root)
    pairs = [(name, name) for name, facts in hosts.items() if (facts or {}).get('ansible_connection') != 'local']
    pairs += [(old, host) for old, host in declared.former_names(hosts).items()]
    return sorted(pairs, key=lambda pair: (-len(pair[0]), pair[0]))


def _node_of(alias, workers):
    """Which node a Strata source alias belongs to, from its name (a declared worker's name, current or former, in
    it); None when the name does not say."""
    for name, node in workers:
        if name in (alias or ''):
            return node
    return None


def report_rows(root=ROOT):
    """Values a person transcribed from a report or a vendor page (`evidence/reports.yml`).

    A transcribed number is the easiest lie in this database, so a row must cite a file that
    exists and say whether it came from our own report (`report`) or from someone else's
    (`published`, which never ranks).
    """
    path = Path(root) / 'evidence/reports.yml'
    if not path.exists():
        return [], {'source': 'report', 'ref': 'evidence/reports.yml', 'cases': 0, 'skipped': {}}
    entries = yaml.safe_load(path.read_text()) or []
    problems, rows = [], []
    for index, entry in enumerate(entries):
        where = f'evidence/reports.yml entry {index}'
        before = len(problems)
        if not isinstance(entry, dict):
            problems.append(f'{where}: not a mapping')
            continue
        missing = sorted({'metric', 'value', 'n', 'workload', 'confidence', 'source_ref', 'measured_at'} - set(entry))
        if missing:
            problems.append(f'{where}: missing {", ".join(missing)}')
            continue
        if entry['metric'] not in METRICS:
            problems.append(f"{where}: metric {entry['metric']!r} is not one of {METRICS}")
        if entry['confidence'] not in ('report', 'published'):
            problems.append(f"{where}: confidence {entry['confidence']!r}; a transcribed row is report or published")
        if not (Path(root) / entry['source_ref']).exists():
            problems.append(f"{where}: source_ref {entry['source_ref']} does not exist — a transcribed value cites a real file")
        try:
            _moment(entry['measured_at'])
        except (TypeError, ValueError):
            problems.append(f'{where}: measured_at {entry["measured_at"]!r} is not an ISO timestamp')
        if not isinstance(entry['value'], (int, float)) or isinstance(entry['value'], bool):
            problems.append(f'{where}: value is not a number')
        if not (isinstance(entry['n'], int) and entry['n'] >= 1):
            problems.append(f'{where}: n must be a positive count of measurements')
        ids = [entry.get(name) for name in ('class_id', 'config_id', 'hw_fp')]
        if any(ids) and not all(ids):
            problems.append(f'{where}: class_id, config_id and hw_fp go together or none of them')
        if len(problems) > before:
            continue
        keys = ({'config_id': entry['config_id'], 'class_id': entry['class_id'], 'hw_fp': entry['hw_fp'],
                 'node': entry.get('node'), 'target': entry.get('target'),
                 'identity': 'history', 'identity_reason': None} if all(ids)
                else _unknown(f'transcribed without keys: {entry.get("why", "no why given")}'))
        rows.append({'metric': entry['metric'], 'value': float(entry['value']), 'n': entry['n'],
                     'ci_low': entry.get('ci_low'), 'ci_high': entry.get('ci_high'),
                     'source': 'report', 'source_ref': entry['source_ref'], 'confidence': entry['confidence'],
                     'measured_at': _moment(entry['measured_at']).isoformat(timespec='seconds'),  # YAML reads a datetime
                     'agent': entry.get('agent', NO_AGENT),
                     'agent_version': entry.get('agent_version', NO_AGENT), 'workload': entry['workload'], **keys})
    if problems:
        raise ValueError('; '.join(problems))
    return rows, {'source': 'report', 'ref': 'evidence/reports.yml', 'cases': len(entries), 'skipped': {}}


# --- staleness ----------------------------------------------------------------------------------

def stale_reason(row, graph, inventory_dir, now=None):
    """Why this row's keys are no longer what the system declares, or None while they still are.

    Nothing is deleted here. A re-declaration (adding the quantization Spark never declared,
    for instance) re-keys every row it produced, and the honest report of that is a reason on
    the row, not a rerun the user did not ask for.
    """
    if row['identity'] == 'unknown':
        return None
    reasons = []
    target = row.get('target')
    if graph is not None and target:
        # a row keeps the target key it was recorded under; a retired key names its new target (naming stage 2)
        from agentiac import route_aliases
        target = route_aliases.canonical(graph.route, target)
    if graph is None:
        reasons.append('the declarations could not be read, so these keys were not checked')
    elif not target:        # a transcribed row names its keys and no target: some target still has to declare them
        try:
            declared_now = identities.compute_all(graph).values()
        except Exception as error:  # the same answer as one target whose declaration cannot be read
            reasons.append(f'the declarations could not be read ({type(error).__name__})')
        else:
            if not any((row['class_id'], row['config_id']) == (identity.class_id, identity.config_id)
                       for identity in declared_now):
                reasons.append(f'no declared target carries class_id {row["class_id"]} and config_id {row["config_id"]}')
    elif target not in graph.route['targets']:
        reasons.append(f'{target} is no longer declared, so its keys cannot be checked')
    else:
        try:
            now_declared = identities.compute_target(graph, target)
        except Exception as error:  # a declaration that cannot be read costs the comparison, not the row
            reasons.append(f'the declaration of {target} could not be read ({type(error).__name__})')
        else:
            for name in ('class_id', 'config_id'):
                if row[name] != getattr(now_declared, name):
                    reasons.append(f'{name} {row[name]} is not what {target} now declares ({getattr(now_declared, name)})')
    node, recorded = row.get('node'), row['hw_fp']
    if recorded != identities.UNKNOWN:
        reading = hardware.fingerprint(inventory_dir, node, now=now) if node else {'hw_fp': identities.UNKNOWN,
                                                                                   'reason': 'the row names no node'}
        if reading['hw_fp'] == identities.UNKNOWN:
            reasons.append(f'hw_fp {recorded} could not be checked: {reading.get("reason", "no reading")}')
        elif reading['hw_fp'] != recorded:
            reasons.append(f'hw_fp {recorded} is not what the inventory of {node} says now ({reading["hw_fp"]})')
    return '; '.join(reasons) or None


# --- the derived database -----------------------------------------------------------------------

def import_evidence(root=ROOT, *, now=None, write=True, suite_path=None):
    """Rebuild `artifacts/evidence/evidence.sqlite` from the primary records; returns what it found.

    The file is replaced, not merged: every row here is derived, so a merge would only
    preserve rows whose sources no longer produce them.
    """
    root = Path(root)
    os.umask(0o077)
    graph = None
    try:
        graph = declared.load(root)
    except Exception:
        graph = None  # the rows are still written; `stale_reason` says their keys went unchecked
    history = load_history(root, graph)
    rows, sources = [], []
    for build in (lambda: suite_rows(root, history, path=suite_path),
                  lambda: ladder_rows(root, history),
                  lambda: report_rows(root)):
        found, summary = build()
        rows += found
        sources.append(summary)
    inventory_dir = hardware.directory(root)
    for row in rows:
        if graph is not None and row.get('node'):
            row['node'] = declared.current_host(row['node'], graph.nodes)   # recorded before a rename (T8.10)
        row['stale_reason'] = stale_reason(row, graph, inventory_dir, now=now)
        row['id'] = evidence_id(row)
    by_id = {}
    for row in rows:
        by_id[row['id']] = row  # the same measurement reached from two sources is one row
    rows = [by_id[key] for key in sorted(by_id)]
    summary = {'rows': len(rows),
               'usable': sum(1 for row in rows if usable(row)),
               'unknown': sum(1 for row in rows if row['identity'] == 'unknown'),
               'stale': sum(1 for row in rows if row['stale_reason']),
               'by_confidence': _counts(row['confidence'] for row in rows),
               'by_metric': _counts(row['metric'] for row in rows),
               'sources': sources,
               'imported_at': _iso((now or datetime.datetime.now(datetime.timezone.utc)).timestamp())}
    if not write:
        return summary, rows
    target = database(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent, suffix='.sqlite.tmp')
    os.close(handle)
    db = sqlite3.connect(temporary)
    try:
        db.executescript(SCHEMA)
        with db:
            for row in rows:
                db.execute(f'INSERT INTO evidence({",".join(COLUMNS)}) '
                           f'VALUES({",".join("?" * len(COLUMNS))})', tuple(row[name] for name in COLUMNS))
            db.execute('INSERT INTO imports VALUES(?,?,?,?,?,?)',
                       (summary['imported_at'], json.dumps(sources, sort_keys=True), summary['rows'],
                        summary['usable'], summary['unknown'], summary['stale']))
        db.commit()
    finally:
        db.close()
    os.replace(temporary, target)  # a reader never sees a half-built database
    return summary, rows


def _counts(values):
    out = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def _under(path, root):
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


def database(root=ROOT):
    """PA §5.2's regenerable Evidence store."""
    return Path(root) / 'artifacts/evidence/evidence.sqlite'


def usable(row):
    """May this row decide a ranking? PA §9.2 and X21, in one place so no query forgets it."""
    if row['identity'] == 'unknown':
        return False
    if row['confidence'] not in RANKING_CONFIDENCE:
        return False
    if row.get('stale_reason'):
        return False
    return not any(row[name] == identities.UNKNOWN for name in ('class_id', 'config_id', 'hw_fp'))


def unusable_reason(row):
    """Why `usable` said no, for the screen that shows the excluded rows rather than hiding them."""
    if row['identity'] == 'unknown':
        return row.get('identity_reason') or 'no keys'
    if row['confidence'] not in RANKING_CONFIDENCE:
        return f'confidence {row["confidence"]} is reference only'
    if row.get('stale_reason'):
        return f'stale: {row["stale_reason"]}'
    missing = [name for name in ('class_id', 'config_id', 'hw_fp') if row[name] == identities.UNKNOWN]
    return f'{", ".join(missing)} unknown' if missing else None


def query(root=ROOT, *, node=None, target=None, agent=None, metric=None, workload=None,
          confidence=None, identity=None, usable_only=False, stale=None, limit=None, path=None,
          config_id=None, class_id=None):
    """Read the derived DB. Every filter is optional; `usable_only` is the ranking view of PA §9.3."""
    path = Path(path) if path else database(root)
    if not path.exists():
        raise FileNotFoundError(f'{path} does not exist; run {cli_name()} evidence import')
    sql, where, args = f'SELECT {",".join(COLUMNS)} FROM evidence', [], []
    for column, value in (('node', node), ('target', target), ('agent', agent), ('metric', metric),
                          ('workload', workload), ('confidence', confidence), ('identity', identity),
                          ('config_id', config_id), ('class_id', class_id)):
        if value is not None:
            where.append(f'{column} = ?')
            args.append(value)
    if usable_only:
        where.append("identity != 'unknown' AND confidence IN (%s) AND stale_reason IS NULL"
                     % ','.join('?' * len(RANKING_CONFIDENCE)))
        args += list(RANKING_CONFIDENCE)
        where.append("config_id != 'unknown' AND class_id != 'unknown' AND hw_fp != 'unknown'")
    if stale is True:
        where.append('stale_reason IS NOT NULL')
    elif stale is False:
        where.append('stale_reason IS NULL')
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY workload, metric, config_id, agent, measured_at'
    if limit:
        sql += f' LIMIT {int(limit)}'
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, args)]
