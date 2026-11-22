#!/usr/bin/env python3
"""Read-only, provenance-separated benchmark aggregates; never export traces."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics


def aggregate(path):
    groups = {}
    # Read only, including WAL-backed committed results from an active controller.
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        rows = db.execute("""SELECT c.spec, c.status, a.result FROM cases c
            LEFT JOIN attempts a ON a.case_id=c.id AND a.number=c.attempts""")
        for raw_spec, status, raw_result in rows:
            spec = json.loads(raw_spec)
            result = json.loads(raw_result or '{}')
            identity = {k: v for k, v in spec.items() if k not in ('task', 'trial')}
            digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if digest not in groups:
                profile = spec.get('profile') or {}
                groups[digest] = {
                    'configuration_sha256': digest,
                    'benchmark_source_sha256': spec.get('benchmark_source_sha256'),
                    'alias': spec.get('alias'), 'profile': profile.get('id'),
                    'model_revision': profile.get('revision'),
                    'backend_revision': profile.get('backend', {}).get('revision'),
                    'concurrency': spec.get('concurrency'), 'tasks': {},
                }
            tasks = groups[digest]['tasks']
            task = tasks.setdefault(spec['task'], {
                'passed': 0, 'model_failed': 0, 'infrastructure_failed': 0,
                'unclassified_failed': 0, 'unfinished': 0,
                'generation_tokens_s': [], 'ttft_s': [], 'task_wall_s': [],
            })
            if status not in ('complete', 'failed'):
                task['unfinished'] += 1
                continue
            if result.get('failure_category') == 'infrastructure':
                task['infrastructure_failed'] += 1
                continue
            if result.get('error_type'):
                task['unclassified_failed'] += 1
                continue
            if status == 'complete' and result.get('passed') is True:
                task['passed'] += 1
            elif status == 'failed' and result.get('passed') is False:
                task['model_failed'] += 1
            else:
                task['unclassified_failed'] += 1
                continue
            for key in ('generation_tokens_s', 'ttft_s', 'task_wall_s'):
                value = result.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    task[key].append(value)
    for group in groups.values():
        for task in group['tasks'].values():
            total = task['passed'] + task['model_failed']
            task['scored_trials'] = total
            task['pass_rate'] = task['passed'] / total if total else None
            for key in ('generation_tokens_s', 'ttft_s', 'task_wall_s'):
                values = task.pop(key)
                task[key + '_median'] = statistics.median(values) if values else None
    return {'schema_version': 1, 'attempt_policy': 'latest attempt per stable case; infrastructure failures excluded from quality denominator',
            'groups': list(groups.values())}


def markdown(report):
    lines = ['# Stage A aggregate results', '',
             'Preliminary local engineering tests; not BFCL or coding leaderboard scores.',
             'Configuration groups remain separate. Latest attempts only; infrastructure failures do not count as model failures.', '']
    for group in report['groups']:
        lines += [f"## {group['alias']} / {group['profile']}", '',
                  f"Configuration: `{group['configuration_sha256']}`. Evaluator: `{group['benchmark_source_sha256']}`.", '',
                  '| Task | Passed/scored | Infrastructure failures | Unclassified | Unfinished | Median tok/s | Median TTFT (s) |',
                  '|---|---:|---:|---:|---:|---:|---:|']
        for name, task in sorted(group['tasks'].items()):
            def fmt(value):
                return f'{value:.3f}' if value is not None else '—'
            lines.append(f"| {name} | {task['passed']}/{task['scored_trials']} | {task['infrastructure_failed']} | {task['unclassified_failed']} | {task['unfinished']} | {fmt(task['generation_tokens_s_median'])} | {fmt(task['ttft_s_median'])} |")
        lines.append('')
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--format', choices=('json', 'markdown'), default='json')
    parser.add_argument('--source-sha', help='include only this exact benchmark source SHA256')
    args = parser.parse_args()
    report = aggregate(args.database)
    if args.source_sha:
        report['groups'] = [g for g in report['groups'] if g['benchmark_source_sha256'] == args.source_sha]
        if not report['groups']:
            parser.error('no configuration matches the exact requested source SHA256')
    print(markdown(report) if args.format == 'markdown' else json.dumps(report, indent=2), end='')


if __name__ == '__main__':
    main()
