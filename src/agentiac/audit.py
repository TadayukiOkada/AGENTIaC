"""What is known against the locked packages (T9.7, PA §15). It reads locks, reports and fixes nothing (ADR 0009).

Every lock goes to one source, OSV, which answers for npm and PyPI alike with no binary, because a timer has neither the
owner's npm nor a pip-audit.

**What leaves this host:** the name and version of each locked package, and no key. U5 allowed names alone; the version is
what an audit asks about, so PA §15 and U5 say so.

**A count of 0 does not mean safe.** OSV holds what reached a public database, and an advisory published only on a
project's own repository may not appear in it.
"""
import concurrent.futures
import datetime
import http.client
import json
from pathlib import Path
import urllib.parse
import urllib.request

from agentiac import paths

SCHEMA_VERSION = 1
SOURCE = 'https://api.osv.dev/v1'
TIMEOUT_S = 30
BATCH = 500
OLD_AFTER_DAYS = 3         # the timer runs daily, so this much silence means it has stopped
LOCKS = (('agents/pi/package-lock.json', 'npm'), ('requirements-control.lock', 'PyPI'),
         ('requirements-gateway.lock', 'PyPI'), ('requirements-lint.lock', 'PyPI'))
SEVERITIES = ('critical', 'high', 'moderate', 'low', 'unknown')
FAILURES = (OSError, ValueError, LookupError, TypeError, http.client.HTTPException)


def packages(root, lock, ecosystem):
    """(name, version) of everything a lock installs, once each."""
    text = paths.locate(lock, root).read_text()
    if ecosystem == 'npm':
        found = {(entry.get('name') or path.rsplit('node_modules/', 1)[-1], entry['version'])
                 for path, entry in json.loads(text)['packages'].items() if path and 'version' in entry}
    else:
        found = {tuple(line.strip().split('==', 1)) for line in text.splitlines() if '==' in line and line[0] != '#'}
    if not found:
        raise ValueError(f'{lock} names no package')
    return sorted(found)


def _post(opener, url, body):
    request = urllib.request.Request(url, json.dumps(body).encode(), {'Content-Type': 'application/json'})
    with opener(request, timeout=TIMEOUT_S) as response:
        return json.load(response)


def _ids(found, ecosystem, opener):
    """Every package with its advisory ids, following a page token until OSV has no more for it."""
    hits = {}
    queries = [{'package': {'name': name, 'ecosystem': ecosystem}, 'version': version} for name, version in found]
    while queries:
        batch, queries = queries[:BATCH], queries[BATCH:]
        results = _post(opener, f'{SOURCE}/querybatch', {'queries': batch})['results']
        if len(results) != len(batch):
            raise ValueError(f'{len(batch)} queries got {len(results)} answers')
        for query, result in zip(batch, results):
            key = (query['package']['name'], query['version'])
            hits.setdefault(key, []).extend(item['id'] for item in result.get('vulns') or [])
            if result.get('next_page_token'):
                queries.append({**query, 'page_token': result['next_page_token']})
    return hits


def _detail(identifier, opener):
    """(severity, withdrawn). One advisory that cannot be read counts as `unknown`: it is still an advisory."""
    url = f'{SOURCE}/vulns/{urllib.parse.quote(identifier)}'
    try:
        with opener(urllib.request.Request(url), timeout=TIMEOUT_S) as response:
            record = json.load(response)
        severity = str((record.get('database_specific') or {}).get('severity') or 'unknown').lower()
        return (severity if severity in SEVERITIES else 'unknown'), bool(record.get('withdrawn'))
    except FAILURES:
        return 'unknown', False


def _lock(lock, ecosystem, root, opener, now):
    found = packages(root, lock, ecosystem)
    hits = _ids(found, ecosystem, opener)
    identifiers = sorted({identifier for ids in hits.values() for identifier in ids})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        details = dict(zip(identifiers, pool.map(lambda identifier: _detail(identifier, opener), identifiers)))
    advisories = [{'id': identifier, 'package': name, 'version': version, 'severity': details[identifier][0]}
                  for (name, version), ids in sorted(hits.items()) for identifier in sorted(set(ids))
                  if not details[identifier][1]]
    return {'lock': lock, 'ecosystem': ecosystem, 'packages': len(found), 'advisories': advisories, 'fetched_at': now,
            'stale': False}


def run(root, previous=None, opener=urllib.request.urlopen, now=None):
    """Each lock on its own. One that cannot be read keeps its last answer, marked stale, as `ops update check` does."""
    now = now or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    last = {entry['lock']: entry for entry in (previous or {}).get('locks', [])}
    locks = []
    for lock, ecosystem in LOCKS:
        try:
            locks.append(_lock(lock, ecosystem, root, opener, now))
        except FAILURES as error:
            reason = f'{type(error).__name__}: {error}'[:200]
            kept = last.get(lock)
            locks.append({**kept, 'stale': True, 'reason': reason} if kept else
                         {'lock': lock, 'ecosystem': ecosystem, 'packages': None, 'advisories': [], 'fetched_at': None,
                          'stale': False, 'reason': reason})
    return {'schema_version': SCHEMA_VERSION, 'checked_at': now, 'source': SOURCE, 'locks': locks}


def load(path):
    """A saved report, or None for one that cannot be used."""
    try:
        report = json.loads(Path(path).read_text())
        return report if report['schema_version'] == SCHEMA_VERSION and isinstance(report['locks'], list) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def counts(report):
    """What `doctor` and `ops update check` print: counts, with the packages named nowhere but the record."""
    locks, advisories = report['locks'], [item for lock in report['locks'] for item in lock['advisories']]
    distinct = {(item['id'], item['package'], item['version']): item for item in advisories}
    return {'advisories': len(distinct), 'packages': len({(item['package'], item['version']) for item in distinct.values()}),
            'severity': {name: sum(item['severity'] == name for item in distinct.values()) for name in SEVERITIES},
            'stale': sum(bool(lock['stale']) for lock in locks), 'unread': sum(lock['packages'] is None for lock in locks)}


def line(report):
    """One sentence. An unread lock is said, because `0 advisories` would otherwise cover what nobody looked at."""
    numbers = counts(report)
    shown = ', '.join(f'{numbers["severity"][name]} {name}' for name in SEVERITIES if numbers['severity'][name])
    text = f'{numbers["advisories"]} advisories in {numbers["packages"]} packages' + (f' ({shown})' if shown else '')
    if numbers['unread']:
        text += f', {numbers["unread"]} locks not read'
    if numbers['stale']:
        text += f', {numbers["stale"]} locks stale'
    return f'{text}, checked {report["checked_at"]}'


def age_days(report, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now - datetime.datetime.fromisoformat(report['checked_at'])).total_seconds() / 86400
