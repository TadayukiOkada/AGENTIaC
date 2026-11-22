"""T8.7: `agentiac log-task` — one JSONL line per real task, and nothing of the task itself.

Roadmap Phase 3 asks for a record of real usage — repository, task type, outcome, minutes, failure
category — so that recurring failures can become suite tasks, which is T6.4. PA F10 found the command
was never implemented, which is the only reason T6.4 could not start.

The line is the product, and three rules are what make it usable later.

**Append-only.** A second call adds a second line and nothing here rewrites or removes one: a log
someone can edit is an opinion.

**No body.** The record has a fixed set of keys, most of them from a vocabulary, and a value with
whitespace in it or longer than a name is refused. A field that could hold a sentence eventually
will, and then the log is a pile of prose nobody aggregates — which is the failure mode PA L8 lists
for the manual equivalent.

**Categories are declared, not invented at the moment of writing.** T6.4 asks for the top three
failure categories, and a category invented at 2am cannot be counted with one invented at 3am.
`other` exists so that something genuinely new stays visible instead of being forced into the
nearest wrong bucket.

Counting is not this module's job: PA §14 puts aggregation in T13.2, where the suite, the gateway
metadata and the router and loop-guard logs are read together.
"""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

from agentiac import paths

ROOT = paths.root()

PATH = 'artifacts/tasks/tasks.jsonl'

OUTCOMES = ('accepted', 'edited', 'rejected', 'abandoned')          # Roadmap Phase 3, verbatim
TYPES = ('coding', 'review', 'summarising', 'tool-usage', 'other')
# The buckets real usage falls into here, not a general taxonomy: `output-capped` and `timeout` are the
# two the task suite already measures most, and `environment` keeps a broken sandbox off the model's
# record. `other` is honest, not lazy — it is the bucket that should shrink as categories are added.
FAILURES = ('wrong-code', 'incomplete', 'context-overflow', 'output-capped', 'timeout', 'tool-misuse',
            'hallucinated-api', 'refusal', 'style', 'environment', 'other')

# A repository or a target is named, never pathed: the log is read by other people, and
# `/home/<account>/...` says nothing about the work and something about the machine.
NAME = re.compile(r'^[A-Za-z0-9._-]{1,80}$')

MAX_MINUTES = 1440.0        # a task that took a day was not one task

# The fields a line may carry, in the order they read best. A key outside this set is a bug, not a feature.
FIELDS = ('schema_version', 'time', 'repository', 'type', 'outcome', 'minutes', 'failure',
          'agent', 'target', 'target_source')


def validate(repository=None, type=None, outcome=None, minutes=None, failure=None, agent=None, target=None):
    """Every problem with a proposed line, as sentences. Empty means it may be written."""
    problems = []
    if not NAME.fullmatch(repository or ''):
        problems.append(f'repository {repository!r} is not a name: one word, no spaces and no path '
                        '(the log is read by other people)')
    if type not in TYPES:
        problems.append(f'type {type!r} is not one of {", ".join(TYPES)}')
    if outcome not in OUTCOMES:
        problems.append(f'outcome {outcome!r} is not one of {", ".join(OUTCOMES)}')
    try:
        value = float(minutes)
    except (TypeError, ValueError):
        problems.append(f'minutes {minutes!r} is not a number')
    else:
        if not 0 < round(value, 1) <= MAX_MINUTES:      # the line stores one decimal, so 0.01 would be stored as 0
            problems.append(f'minutes {value} is outside 0.1-{MAX_MINUTES:g}; a task that took a day was not one task')
    if outcome in ('rejected', 'abandoned') and not failure:
        problems.append(f'a {outcome} task needs --failure: a failure nobody categorised is a failure nobody fixes')
    if outcome == 'accepted' and failure:
        problems.append(f'an accepted task carries no failure category (got {failure!r})')
    if failure is not None and failure not in FAILURES:
        problems.append(f'failure {failure!r} is not one of {", ".join(FAILURES)}; `other` is there for a real new kind')
    if agent is not None and agent not in KNOWN_AGENTS:
        problems.append(f'agent {agent!r} is not one of {", ".join(KNOWN_AGENTS)}')
    if target and not NAME.fullmatch(target):
        problems.append(f'target {target!r} is not a name: one word, no spaces and no path '
                        '(the log is read by other people)')
    return problems


def _known_agents():
    """The declared Agents. Imported late so that reading a log line never pulls in the adapter machinery."""
    from agentiac import adapters
    return tuple(sorted(adapters.ADAPTERS))


try:                                    # a plain import at call time would make `lines()` cost an import
    KNOWN_AGENTS = _known_agents()
except Exception:                       # the vocabulary must not depend on the adapters importing cleanly
    KNOWN_AGENTS = ('pi',)


def route_target(root=ROOT):
    """What the gateway serves now, from the rendered route file. `unknown` when it cannot be read.

    Two files, two answers, and they are not interchangeable: `artifacts/gateway/route.json` is what is
    being served, `gateway/local-coder.json` is what this checkout says to serve when nothing was
    rendered. Falling back from the first to the second is a declared value answering an observed
    question, so the record keeps the difference in `target_source` instead of smoothing it away.

    This is the target *now*, which is not necessarily the one that served the task a while ago; say
    `--target` when you know better.
    """
    for name, key, source in (('artifacts/gateway/route.json', 'target', 'route'),
                              ('gateway/local-coder.json', 'default', 'declared-default')):
        try:
            document = json.loads((Path(root) / name).read_text())
        except (OSError, ValueError):
            continue
        if document.get(key):
            return document[key], source
    return None, 'unavailable'


def build(root=ROOT, *, now=None, **fields):
    """The line a call would write, validated. Raises ValueError with every problem in it."""
    problems = validate(**{key: fields.get(key) for key in ('repository', 'type', 'outcome', 'minutes',
                                                             'failure', 'agent', 'target')})
    if problems:
        raise ValueError('; '.join(problems))
    target = fields.get('target')
    target_source = 'given' if target else None
    if not target:
        target, target_source = route_target(root)
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    if isinstance(moment, str):
        moment = datetime.datetime.fromisoformat(moment)
    return {'schema_version': 1, 'time': moment.isoformat(timespec='seconds'),
            'repository': fields['repository'], 'type': fields['type'], 'outcome': fields['outcome'],
            'minutes': round(float(fields['minutes']), 1), 'failure': fields.get('failure'),
            'agent': fields.get('agent'), 'target': target, 'target_source': target_source}


def record(root=ROOT, *, now=None, **fields):
    """Append one line. Returns what was written; raises ValueError rather than writing a bad line."""
    line = build(root=root, now=now, **fields)
    path = Path(root) / PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as log:
        log.write(json.dumps(line, sort_keys=True, ensure_ascii=False) + '\n')
    return line


def lines(root=ROOT):
    """Every line, oldest first. A line that does not parse is returned as `{'broken': ...}`, not dropped."""
    path = Path(root) / PATH
    if not path.exists():
        return []
    found = []
    for number, text in enumerate(path.read_text().splitlines(), start=1):
        if not text.strip():
            continue
        try:
            found.append(json.loads(text))
        except ValueError:
            found.append({'broken': number, 'line': text})
    return found
