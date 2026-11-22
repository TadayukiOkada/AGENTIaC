"""T11.2: what failed, read out of the fields the runner already stored.

PA §10 moves 失敗の集計 off "a person or an Agent reads the trace" and onto this command, and PA §14 puts
it between the observation and the Improvement Proposal draft. Three rules hold the module together.

**A class names the field it was read from, and prints its value.** A classification nobody can check
is a long trace wearing a shorter coat: the point of the command is that the reason is *in* the output.

**Absence is not evidence.** An adapter that reported no `assistant_turns` did not fail to answer, it
said nothing about answering. Every rule below asks whether the field is there before believing it, so
an adapter that parses less cannot look worse than one that parses more.

**Order is an argument.** An attempt whose Agent process died has no diff, no assistant message and no
pytest run either; calling that `no-change` would blame the model for a crash. The rules are tried in
the order below and the first one that holds is the answer.

**The verdict comes first.** An attempt the grader passed did not fail, whatever else happened on the
way: `passed` is the runner's answer, and the rules explain a failure, they do not overrule one.

Where the stored fields do not say, the answer is `unexplained`. It is counted and printed rather than
distributed among the classes it might have been: it is the amount this command does not know, and the
first number that should shrink.
"""
from __future__ import annotations

import collections
import hashlib
import json
from pathlib import Path
import sqlite3

from agentiac import paths

ROOT = paths.root()

# The fields the rules read, and nothing else. `usage`, `tools` and `provenance` are in the same result
# and are deliberately not consulted: a class that could be named from a token count could also be
# named from a mood, and the reader would have no way to tell.
FIELDS = ('infrastructure_error', 'timed_out', 'frontend_returncode', 'assistant_turns',
          'pytest_returncode', 'diff_stat', 'stop_reason', 'acceptance_failed', 'regression_failed')

# Ordered, first match wins. `unexplained` is last and is a rule with no condition, not a fallback that
# is silently reached: a new class inserted above it must say which of these it takes over.
RULES = (
    ('infrastructure',
     lambda r: 'infrastructure_error' in r,
     lambda r: f'the runner recorded {r["infrastructure_error"]}, so nothing was measured'),
    ('timeout',
     lambda r: bool(r.get('timed_out')),
     lambda r: f'the attempt was killed at its timeout after {r.get("wall_s")} s'),
    ('no-result',
     lambda r: not r,
     lambda r: 'the attempt finished and left no result behind'),
    ('agent-crashed',
     lambda r: _seen(r, 'frontend_returncode') and r['frontend_returncode'] != 0,
     lambda r: f'the Agent process exited {r["frontend_returncode"]}'),
    ('no-answer',
     lambda r: _seen(r, 'assistant_turns') and r['assistant_turns'] == 0,
     lambda r: 'the Agent produced no assistant message'),
    ('grader-error',
     lambda r: _seen(r, 'pytest_returncode') and r['pytest_returncode'] not in (0, 1),
     lambda r: f'pytest exited {r["pytest_returncode"]}, so no test result stands: the grader broke, or the '
               "Agent's edit stopped the tests from being collected"),
    # Before no-change: an answer cut off at the cap often leaves no diff, and the cap is the cause.
    ('output-capped',
     lambda r: _seen(r, 'stop_reason') and r['stop_reason'] in ('length', 'max_tokens'),   # pi, Claude Code
     lambda r: 'the answer was cut off at the token cap'),
    ('no-change',
     lambda r: _seen(r, 'diff_stat') and not str(r['diff_stat']).strip(),
     lambda r: 'the Agent changed no file, so there was nothing to grade'),
    ('acceptance-missed',
     lambda r: _failed(r, 'acceptance') and not _failed(r, 'regression'),
     lambda r: f'{len(r["acceptance_failed"])} of {r.get("acceptance_total", "?")} acceptance tests still fail'),
    ('regression-broken',
     lambda r: _failed(r, 'regression') and not _failed(r, 'acceptance'),
     lambda r: f'{len(r["regression_failed"])} of {r.get("regression_total", "?")} regression tests broke'),
    ('both-failed',
     lambda r: _failed(r, 'regression') and _failed(r, 'acceptance'),
     lambda r: f'{len(r["acceptance_failed"])} acceptance and {len(r["regression_failed"])} regression tests fail'),
)

CLASSES = tuple(name for name, _, _ in RULES) + ('unexplained',)

INFRASTRUCTURE, UNEXPLAINED = 'infrastructure', 'unexplained'

TOP_NAMES = 20        # names on the screen; the record (`--json`) holds all of them


def _seen(result, key):
    """The field is there and carries a value. A missing field is a gap in the record, never a symptom."""
    return key in result and result[key] is not None


def _failed(result, kind):
    return bool(result.get(kind + '_failed'))


def classify(result):
    """One class, the reason, and the fields that produced it. `result` is what the runner stored."""
    if result.get('passed') is True and 'infrastructure_error' not in result:
        return {'class': None, 'reason': None, 'fields': _fields(result)}
    for name, when, why in RULES:
        if when(result):
            return {'class': name, 'reason': why(result), 'fields': _fields(result)}
    if result.get('passed') is False:
        return {'class': UNEXPLAINED,
                'reason': 'the attempt is marked failed and no rule holds: the fields disagree with the verdict',
                'fields': _fields(result)}
    return {'class': None, 'reason': None, 'fields': _fields(result)}


def _fields(result):
    """What the reader would have to open the trace to see, printed instead. Long values are one line."""
    fields = {key: result[key] for key in FIELDS if key in result}
    if isinstance(fields.get('diff_stat'), str):
        fields['diff_stat'] = fields['diff_stat'].strip().splitlines()[:1] or ['']
    return fields


# --- the record ------------------------------------------------------------------------------------

def _latest_attempts(db):
    """The last attempt of every case. History is kept; the answer is the latest (as in `evidence`)."""
    rows = db.execute('''SELECT c.id case_id, c.spec spec, a.number number, a.status status,
                                a.result result
                         FROM cases c JOIN attempts a ON a.case_id = c.id AND a.number = c.attempts
                         WHERE a.status IN ('complete', 'failed')''')
    return [dict(row) for row in rows]


def manifests(root=ROOT):
    """The hash of every task manifest now in the checkout, so a superseded case can be named and left out."""
    folder = paths.locate('benchmarks/suite/tasks', root)
    return {path.stem: hashlib.sha256(path.read_bytes()).hexdigest() for path in folder.glob('*.json')}


def attempts(root=ROOT, alias=None, task=None, agent=None):
    """Every case's latest attempt, classified. The database is opened read-only; no trace is opened at all.

    A case whose manifest has been edited since it ran is returned as `superseded` instead of being
    dropped in silence: `eval suite report` hides those rows, and this command reports that it hid them.
    """
    path = Path(root) / 'artifacts/suite/state.sqlite'
    if not path.exists():
        return []
    current = manifests(root)
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = _latest_attempts(db)
    found = []
    for row in rows:
        spec = json.loads(row['spec'])
        if alias is not None and spec.get('alias') != alias:
            continue
        if task is not None and spec.get('task') != task:
            continue
        if agent is not None and spec.get('agent', 'pi') != agent:     # the baseline's specs name no Agent (X18)
            continue
        result = json.loads(row['result'] or '{}')
        found.append({'alias': spec.get('alias') or 'unknown', 'agent': spec.get('frontend') or 'unknown',
                      'task': spec.get('task'), 'trial': spec.get('trial'), 'case': row['case_id'],
                      'attempt': row['number'], 'passed': result.get('passed'), 'result': result,
                      'variant': (spec.get('frontend') or 'unknown') + (' +loop-guard' if spec.get('loop_guard') else '')
                      + (f" +thinking-{spec['thinking']}" if spec.get('thinking') else ''),
                      'superseded': current.get(spec.get('task')) != spec.get('task_sha256'),
                      'trace': f'artifacts/suite/runs/{row["case_id"]}/attempt-{row["number"]}',
                      **classify(result)})
    return found


def test_ids(rows):
    """The failing test ids, grouped, with the kind of test each one was.

    These names are what the Evidence DB does not carry (it stores aggregates), and what PA §9.3 could
    not compare without this command: `no new regression class` is a statement about names.
    """
    grouped = collections.defaultdict(lambda: {'attempts': 0, 'aliases': set(), 'kinds': set(), 'seen': set()})
    for row in rows:
        for kind in ('acceptance', 'regression'):
            for name in row['result'].get(kind + '_failed') or ():
                entry = grouped[name]
                which = (row['case'], row['attempt'])
                if which not in entry['seen']:              # a name in both lists is still one attempt failing
                    entry['attempts'] += 1
                    entry['seen'].add(which)
                entry['aliases'].add(row['alias'])
                entry['kinds'].add(kind)
    return [{'test': name, 'attempts': entry['attempts'], 'kind': '+'.join(sorted(entry['kinds'])),
             'aliases': sorted(entry['aliases'])}
            for name, entry in sorted(grouped.items(), key=lambda item: (-item[1]['attempts'], item[0]))]


def summarise(root=ROOT, alias=None, task=None, agent=None):
    """The failure report: counts by class, the test ids behind them, and what the command refused to score."""
    rows = attempts(root, alias, task, agent)
    reported = [row for row in rows if not row['superseded']]
    failures = [row for row in reported if row['class'] not in (None, INFRASTRUCTURE)]
    # Infrastructure is counted in the header and kept out of the table: it is not a way the model failed,
    # and a share of the failures is a denominator it does not belong to.
    counts = collections.Counter(row['class'] for row in failures)
    return {'schema_version': 1, 'source': 'artifacts/suite/state.sqlite',
            'alias': alias, 'task': task, 'agent': agent,
            'attempts': len(reported),
            'passed': sum(row['passed'] is True and row['class'] != INFRASTRUCTURE for row in reported),
            'failed': len(failures),
            'infrastructure': sum(row['class'] == INFRASTRUCTURE for row in reported),
            'unexplained': counts.get(UNEXPLAINED, 0),
            'superseded': len(rows) - len(reported),
            'classes': [{'class': name, 'attempts': counts[name],
                         'share': round(counts[name] / len(failures), 3) if failures else None,
                         'aliases': sorted({row['alias'] for row in failures if row['class'] == name}),
                         'tasks': sorted({row['task'] for row in failures if row['class'] == name}),
                         'reason': next(row['reason'] for row in failures if row['class'] == name),
                         'example': _example(next(row for row in failures if row['class'] == name))}
                        for name in CLASSES if name in counts],
            'test_ids': test_ids(failures)}


def _example(row):
    return {'alias': row['alias'], 'task': row['task'], 'trial': row['trial'],
            'case': row['case'][:12], 'attempt': row['attempt'], 'trace': row['trace']}


def new_regressions(root=ROOT, candidate=None, against=None, task=None):
    """The comparison PA §9.3 could not make from aggregates: which test names break for one alias and not the other.

    A regression test is a test the base already passes, so a name that fails for the candidate and not
    for the incumbent is a new regression class. A task the incumbent never ran is `unknown_tasks`, not
    an absence of regressions — saying "no new regressions" about a task nobody compared would be the
    same lie as a gate read as passed. A run is compared with the same Agent at the same thinking and
    loop-guard settings, and an attempt with no test verdict (`grader-error`) has no names to compare.
    """
    def failing(which):
        rows = [row for row in attempts(root, which, task) if not row['superseded']
                and row['class'] not in (INFRASTRUCTURE, 'grader-error')]
        by_run = collections.defaultdict(set)
        for row in rows:
            for name in row['result'].get('regression_failed') or ():
                by_run[row['task'], row['variant']].add(name)
        return {(row['task'], row['variant']) for row in rows}, by_run

    candidate_runs, candidate_failing = failing(candidate)
    against_runs, against_failing = failing(against)
    compared = sorted(candidate_runs & against_runs)
    unknown = sorted({run[0] for run in candidate_runs - against_runs})
    new = [{'test': name, 'task': run[0]} for run in compared for name in sorted(candidate_failing[run])
           if name not in against_failing[run]]
    if not candidate_runs or not against_runs:
        verdict = 'insufficient-evidence'          # one side never ran; there is nothing to compare
    elif new:
        verdict = 'new-regression'
    elif unknown:
        verdict = 'insufficient-evidence'          # something failed, and the incumbent is silent about that task
    else:
        verdict = 'no-new-regression'
    return {'schema_version': 1, 'candidate': candidate, 'against': against, 'verdict': verdict,
            'new': new, 'compared_tasks': sorted({run[0] for run in compared}), 'unknown_tasks': unknown,
            'candidate_runs': len(candidate_runs), 'against_runs': len(against_runs)}


# --- the screen ------------------------------------------------------------------------------------

def markdown(report):
    """The classes, then the names. Both are read from the record; neither asks anything of the reader."""
    lines = [f'{report["failed"]} failed of {report["attempts"]} attempts '
             f'({report["passed"]} passed, {report["infrastructure"]} infrastructure, '
             f'{report["superseded"]} superseded)', '']
    if not report['classes']:
        return '\n'.join([*lines, 'no failures to classify'])
    lines += ['| class | attempts | share | aliases | tasks | why (field: value) | example |',
              '|---|---:|---:|---|---|---|---|']
    for entry in report['classes']:
        example = entry['example']
        lines.append(f'| {entry["class"]} | {entry["attempts"]} | '
                     f'{"-" if entry["share"] is None else f"{entry['share']:.0%}"} | '
                     f'{", ".join(entry["aliases"]) or "-"} | {", ".join(entry["tasks"]) or "-"} | '
                     f'{entry["reason"]} | {example["trace"]} |')
    lines += ['', f'unexplained: {report["unexplained"]} — the fields do not say, and the trace was not read', '']
    ids = report['test_ids']
    if ids:
        lines += ['| test id | kind | attempts | aliases |', '|---|---|---:|---|']
        lines += [f'| `{item["test"]}` | {item["kind"]} | {item["attempts"]} | {", ".join(item["aliases"])} |'
                  for item in ids[:TOP_NAMES]]
        if len(ids) > TOP_NAMES:
            # A whole suite failing at import names hundreds of tests and says one thing: the screen groups
            # the tail by file instead of printing it, and `--json` still carries every name.
            files = collections.Counter(item['test'].split('::')[0] for item in ids[TOP_NAMES:])
            lines += ['', f'and {len(ids) - TOP_NAMES} more names, in ' +
                      ', '.join(f'{name} ({count})' for name, count in files.most_common(6)) +
                      '; `--json` lists them all']
    return '\n'.join(lines)


def markdown_comparison(comparison):
    """The verdict first, then the names, then what was never compared. A verdict without the last part invites a guess."""
    lines = [f'{comparison["candidate"]} against {comparison["against"]}: **{comparison["verdict"]}**',
             '', f'compared tasks: {", ".join(comparison["compared_tasks"]) or "-"}',
             f'never ran for the incumbent (not compared): {", ".join(comparison["unknown_tasks"]) or "-"}', '']
    if not comparison['new']:
        return '\n'.join([*lines, 'nothing new to name' if comparison['verdict'] != 'no-new-regression'
                          else 'no test id fails for the candidate that the incumbent passes'])
    lines += ['| test id | task |', '|---|---|']
    lines += [f'| `{item["test"]}` | {item["task"]} |' for item in comparison['new'][:TOP_NAMES]]
    if len(comparison['new']) > TOP_NAMES:
        files = collections.Counter(item['test'].split('::')[0] for item in comparison['new'][TOP_NAMES:])
        lines += ['', f'and {len(comparison["new"]) - TOP_NAMES} more new names, in ' +
                  ', '.join(f'{name} ({count})' for name, count in files.most_common(6)) +
                  '; `--json` lists them all']
    return '\n'.join(lines)
