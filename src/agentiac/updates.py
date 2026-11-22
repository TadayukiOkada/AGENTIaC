"""What upstream offers for the declared components (T9.2, PA §8), what the machines run (T9.3), and what an update
would take (T9.4). It reads metadata and versions and changes nothing (ADR 0009).

Only package and repository names leave this host (U5): no key is sent, and a registry's anonymous token is used for one
request and never stored. Versions are never compared as plain strings: each source has its own rule.

One failed read does not stop the rest. The last good answer stays, marked `stale`, so a network problem cannot make a
component look up to date or remove what was known about it.
"""
import concurrent.futures
import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from agentiac.adapters import AdapterError
from agentiac.errors import explain, headline
from agentiac.errors import command as cli_name

SCHEMA_VERSION = 1
USER_AGENT = 'agentiac'
TIMEOUT_S = 20
MANIFESTS = ('application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, '
             'application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json')
FAILURES = (OSError, ValueError, LookupError, TypeError, http.client.HTTPException)
SEMVER = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$')
COMMIT = re.compile(r'^[0-9a-f]{40}$')


def request(url, method='GET', **headers):
    return urllib.request.Request(url, method=method, headers={'User-Agent': USER_AGENT, **headers})


def read_json(opener, url, **headers):
    with opener(request(url, **headers), timeout=TIMEOUT_S) as response:
        return json.load(response)


# Each provider returns (latest, url read). They raise on anything they cannot use; `check` turns that into `stale`.

def npm(source, pin, opener):
    url = 'https://registry.npmjs.org/' + urllib.parse.quote(source['package'], safe='@/') + '/latest'
    return read_json(opener, url)['version'], url


def pypi(source, pin, opener):
    url = f"https://pypi.org/pypi/{urllib.parse.quote(source['package'])}/json"
    return read_json(opener, url)['info']['version'], url


def github(source, pin, opener):
    """The latest release against a pinned version; a commit only where there is no version to compare (PA §8.1).
    HEAD against a pinned tag would report every repository as behind, because a branch moves past its releases."""
    base, accept = f"https://api.github.com/repos/{source['repository']}", {'Accept': 'application/vnd.github+json'}
    if 'version' in pin:
        url = base + '/releases/latest'
        try:
            return read_json(opener, url, **accept)['tag_name'], url
        except urllib.error.HTTPError as error:
            if error.code != 404:  # a 404 means no releases; a 403 is a rate limit and must not look like one
                raise
    url = base + '/commits/HEAD'
    return read_json(opener, url, **accept)['sha'], url


def huggingface(source, pin, opener):
    url = f"https://huggingface.co/api/models/{source['repository']}"
    return read_json(opener, url)['sha'], url


def digest(source, pin, opener):
    """What the tag points at now, by two anonymous requests to Docker Hub."""
    name = source['image'] if '/' in source['image'] else f"library/{source['image']}"
    token = read_json(opener, f'https://auth.docker.io/token?service=registry.docker.io&scope=repository:{name}:pull')['token']
    url = f"https://registry-1.docker.io/v2/{name}/manifests/{source['tag']}"
    with opener(request(url, method='HEAD', Authorization=f'Bearer {token}', Accept=MANIFESTS), timeout=TIMEOUT_S) as response:
        return response.headers.get('Docker-Content-Digest'), url


PROVIDERS = {'npm': npm, 'pypi': pypi, 'github': github, 'huggingface': huggingface, 'digest': digest}


def _semver_key(version):
    match = SEMVER.match(version)
    if not match:
        raise ValueError(f'{version} is not a SemVer version')
    major, minor, patch, pre = match.groups()
    # A prerelease sorts below its release; inside one, numbers sort below names (SemVer §11).
    identifiers = tuple((0, int(part), '') if part.isdigit() else (1, 0, part) for part in pre.split('.')) if pre else ()
    return int(major), int(minor), int(patch), not pre, identifiers


def _order(pinned, latest):
    return 'current' if pinned == latest else 'behind' if pinned < latest else 'ahead'


def _pep440(pinned, latest):
    from packaging.version import Version  # requirements-control.lock
    return _order(Version(pinned), Version(latest))


def compare(component, latest):
    """current, behind or ahead where versions have an order; current or differs where only equality means anything."""
    kind, pin = component['source']['type'], component['pin']
    try:
        if kind == 'npm':
            return _order(_semver_key(pin['version']), _semver_key(latest))
        if kind == 'pypi':
            return _pep440(pin['version'], latest)
        if kind == 'github' and not COMMIT.match(latest):
            return _pep440(pin['version'], latest)
    except (ValueError, KeyError):
        return 'unknown'
    expected = pin.get('digest') if kind == 'digest' else pin.get('revision')
    return 'current' if expected == latest else 'differs'


def pinned(component):
    pin = component.get('pin') or {}
    return pin.get('version') or pin.get('revision') or pin.get('digest')


def _reason(error):
    return f'{type(error).__name__}: {error}'[:200]


def check_one(component, previous, opener, now):
    entry = {'id': component['id'], 'owner': component['owner'], 'policy': component['policy'],
             'source': component['source']['type'], 'pinned': pinned(component), 'latest': None, 'status': 'frozen',
             'source_url': None, 'fetched_at': None, 'stale': False}
    if component['policy'] == 'frozen':
        return entry
    if entry['source'] == 'observed':
        return {**entry, 'status': 'observed', 'reason': 'no upstream is declared'}
    try:
        latest, url = PROVIDERS[entry['source']](component['source'], component['pin'], opener)
        if not (isinstance(latest, str) and latest):
            raise ValueError(f'upstream answered {latest!r}')
    except FAILURES as error:
        last = previous.get(component['id']) or {}
        if last.get('latest') is None:
            return {**entry, 'status': 'unknown', 'reason': _reason(error)}
        return {**entry, 'latest': last['latest'], 'status': compare(component, last['latest']), 'stale': True,
                'source_url': last.get('source_url'), 'fetched_at': last.get('fetched_at'), 'reason': _reason(error)}
    return {**entry, 'latest': latest, 'status': compare(component, latest), 'source_url': url, 'fetched_at': now}


def check(components, previous=None, opener=urllib.request.urlopen, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    return {'schema_version': SCHEMA_VERSION, 'checked_at': now,
            'components': [check_one(component, previous or {}, opener, now) for component in components]}


def exit_code(report):
    """0 when anything could be read, since a stale entry is a note (PA §13); 3 when nothing could."""
    asked = [entry for entry in report['components'] if entry['status'] not in ('frozen', 'observed')]
    read = [entry for entry in asked if entry['latest'] is not None and not entry['stale']]
    return 3 if asked and not read else 0


def load_report(path):
    """A saved report, or None for one that cannot be used."""
    try:
        report = json.loads(Path(path).read_text())
        return report if report['schema_version'] == SCHEMA_VERSION else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load_previous(path):
    """The last report by id. A file that cannot be used costs the stale answers and nothing else."""
    try:
        return {entry['id']: entry for entry in (load_report(path) or {}).get('components', [])}
    except (KeyError, TypeError):
        return {}


def save(path, report):
    """Whole or not at all: the next check reads this for its stale answers."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, indent=1, sort_keys=True) + '\n')
    os.replace(temporary, path)


def news(report, previous):
    """`notify` components whose candidate is new since the last check, as 'id version'. A stale answer is old news."""
    return [f"{entry['id']} {entry['latest']}" for entry in report['components']
            if entry['policy'] == 'notify' and entry['status'] in ('behind', 'differs') and not entry['stale']
            and (previous.get(entry['id']) or {}).get('latest') != entry['latest']]


def _short(value):
    if value is None:
        return '-'
    return value[:19] if value.startswith('sha256:') else value[:12] if COMMIT.match(value) else value


def screen(report):
    lines = [f'{"component":<30}{"pinned":<21}{"latest":<21}status']
    for entry in report['components']:
        note = ' (stale: read ' + entry['fetched_at'] + ')' if entry['stale'] else ''
        lines.append(f"{entry['id']:<30}{_short(entry['pinned']):<21}{_short(entry['latest']):<21}{entry['status']}{note}")
        if entry.get('reason') and entry['status'] != 'observed':
            lines.append(f"  {entry['reason']}")
    lines.append(f"checked {report['checked_at']}")
    return '\n'.join(lines)


# T9.3: what is really there. Nothing below writes to a worker: REMOTE_READS is every command that goes to one.

UNKNOWN = 'unknown'
DRIVER = ('nvidia-smi', '--query-gpu=driver_version', '--format=csv,noheader')
L4T = ('head', '-1', '/etc/nv_tegra_release')
REMOTE_READS = (DRIVER, L4T)
PINNED_AGENTS = ('pi', 'claude-code', 'codex')
PERSONAL_CLIS = {'claude-user': 'claude', 'codex-user': 'codex'}
OBSERVE_FAILURES = FAILURES + (subprocess.SubprocessError, AdapterError)
L4T_RELEASE = re.compile(r'#\s*R(\d+)\s*\(release\),\s*REVISION:\s*([0-9.]+)')


def _first_line(done):
    lines = done.stdout.strip().splitlines()
    if done.returncode or not lines:
        raise ValueError((done.stderr or 'no output').strip()[:120])
    return lines[0].strip()


def _driver(name, probes):
    return {'value': _first_line(probes.remote(name, DRIVER))}


def _jetpack(name, probes):
    release = L4T_RELEASE.search(_first_line(probes.remote(name, L4T)))
    if not release:
        raise ValueError('/etc/nv_tegra_release names no release')
    return {'value': f'R{release[1]}.{release[2]}'}


def _engine_version(spec):
    return (spec.get('engine') or {}).get('version') or spec.get('source_version')


def _engine(name, probes, manifests):
    """A version counts as observed only if the front's manifest is byte for byte one this checkout declares."""
    code, status = probes.front(name)
    if code != 200:
        raise ValueError('no front answered')
    identity = status.get('identity')
    if not identity:
        raise ValueError('the front names no manifest')
    if 'error' in identity:
        raise ValueError(f"the front could not read its manifest: {identity['error']}")
    spec, digest_on_disk = manifests.get(identity.get('backend_id'), (None, None))
    if spec is None:
        raise ValueError(f"the front serves {identity.get('backend_id')}, which this checkout does not declare")
    if identity.get('manifest_hash') != digest_on_disk:
        raise ValueError(f"the manifest of {identity['backend_id']} on the worker differs from this checkout")
    return {'value': _engine_version(spec), 'backend': identity['backend_id']}


def _reader(component, probes, manifests):
    """How to read this component on one host, or None for a component nothing here can read."""
    name = component['id']
    if name in PINNED_AGENTS:
        return lambda host: {'value': probes.agent(name).strip()}
    if name in PERSONAL_CLIS:
        return lambda host: {'value': probes.personal(PERSONAL_CLIS[name]).strip()}
    if name == 'nvidia-driver':
        return lambda host: _driver(host, probes)
    if name == 'jetpack':
        return lambda host: _jetpack(host, probes)
    if component.get('kind') == 'engine':
        return lambda host: _engine(host, probes, manifests)
    return None


def manifests_by_id(root, backends):
    """Manifest id -> (spec, sha256 of its file), the same bytes a front hashes on its worker."""
    return {spec['id']: (spec, hashlib.sha256((Path(root) / f'backends/{stem}.json').read_bytes()).hexdigest())
            for stem, spec in backends.items()}


def _read_host(read, host):
    try:
        return read(host)
    except OBSERVE_FAILURES as error:
        return {'value': UNKNOWN, 'reason': f'{headline(error)}'[:200]}


def _notes(entry, previous):
    notes = []
    was = (previous.get(entry['id']) or {}).get('hosts', {})
    for host, seen in entry['hosts'].items():
        value = seen['value']
        if value == UNKNOWN:
            continue
        before = (was.get(host) or {}).get('value')
        if entry['owner'] != 'project' and before not in (None, UNKNOWN, value):
            notes.append(f"{entry['id']} on {host} changed from {before} to {value}")
        if entry['pinned'] and entry['pinned'] not in value:
            notes.append(f"{entry['id']} on {host} is {value}, and the pin is {entry['pinned']}")
    return notes


def observe(components, probes, manifests, previous=None, now=None):
    """One entry per readable component. A host that cannot be read is `unknown` with the reason: a borrowed machine
    being off is not a fault. They run together, so one unreachable host costs its own timeout and no more."""
    now = now or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    readable = [(component, read) for component in components
                for read in [_reader(component, probes, manifests)] if read]
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        tasks = [(component, host, pool.submit(_read_host, read, host))
                 for component, read in readable for host in component['hosts']]
        entries = {}
        for component, host, task in tasks:
            entry = entries.setdefault(component['id'], {'id': component['id'], 'owner': component['owner'],
                                                         'pinned': pinned(component), 'hosts': {}})
            entry['hosts'][host] = task.result()
    report = list(entries.values())
    return {'schema_version': SCHEMA_VERSION, 'observed_at': now, 'components': report,
            'notes': [note for entry in report for note in _notes(entry, previous or {})]}


def observe_exit_code(report):
    """0 when anything could be read: an unreachable borrowed host is not a failure. 3 when nothing could."""
    values = [seen['value'] for entry in report['components'] for seen in entry['hosts'].values()]
    return 3 if values and all(value == UNKNOWN for value in values) else 0


def observed_engine(report, node, backend_id):
    """The engine version the front on NODE reported for this manifest, or None if it serves something else."""
    for entry in (report or {}).get('components', []):
        seen = entry.get('hosts', {}).get(node) or {}
        if backend_id is not None and seen.get('backend') == backend_id:
            return seen['value']
    return None


def screen_observed(report):
    lines = [f'{"component":<30}{"host":<10}{"observed":<24}pinned']
    for entry in report['components']:
        for host, seen in entry['hosts'].items():
            lines.append(f"{entry['id']:<30}{host:<10}{seen['value']:<24}{entry['pinned'] or '-'}")
            if seen.get('reason'):
                lines.append(f"  {seen['reason']}")
    lines += [f"note: {note}" for note in report['notes']]
    lines.append(f"observed {report['observed_at']}")
    return '\n'.join(lines)


# T9.4: what an update would take. Reads files here and changes nothing: no network, no subprocess, no writes (ADR 0009).
# The steps are read out of PA §8.1 each time, so the document stays the only place they are written.

PROCEDURES = 'docs/platform-architecture.md'
APPROVALS = 'docs/execution-plan.md'
CONTROL_ONLY = 'none on a worker: this stays on control'
MEASURE = f'{cli_name()} ops storage gives the room on each host first (S §3.5 step 1); no size is declared, so there is no figure here'
ROUTES = {  # component id, then kind -> (row of PA §8.1, approvals, capacity)
    'pi': ('pi', ('P1', 'P11'), CONTROL_ONLY),
    'agent': (None, ('P1',), CONTROL_ONLY),
    'litellm': ('LiteLLM', ('P1', 'P6'), CONTROL_ONLY),
    'python': ('ほかの control の Python', ('P1',), CONTROL_ONLY),
    'engine': ('Strata、TensorFold recipe', ('P1', 'P11'), MEASURE),
    'model': ('モデル', ('P1', 'P11'), MEASURE + '. 重みが変わる更新では薄型化の効果がなくなる (S §4.1)'),
    'base-image': ('コンテナ base', ('P1',), 'one rebuilt image on each host; no size is declared'),
    'ci': ('GitHub Actions', ('P1',), 'none: a workflow file in this repository'),
    'skill': ('外部 Skill', ('P1',), 'none: a checkout of text and helper scripts on each host'),
}
NOTES = {  # what §8.1's row leaves to another document. These only point: the steps stay where they are written
    'pi': 'docs/operations.md, "Reinstalling pi", builds the same install from the lock',
    'tensorfold': 'docs/tensorfold-recipe.md, "Changing the recipe", has the manifest hashes and the three places the image ID lives',
    'tensorfold-recipe': 'docs/tensorfold-recipe.md, "What the 1.2 line has taken", says which of the 17 patches upstream already has',
    'ansible': 'ansible-core 2.22 needs Python 3.13 and control runs 3.12, so stay on 2.21 until that is decided (T9.9)',
}
NO_ROW = {'apply': 're-run scripts/pin-agent.py and commit the new digest (docs/usage.md)',
          'rollback': 'not written down: PA §8.1 has no row for this component'}
FROZEN_BECAUSE = {
    'vendor': ('its owner is vendor, so it is observed only (PA §8.2)',
               "replacing it needs the owner's approval (AGENTS.md), and no command here does it"),
    'user': ('its owner is user, so it is observed only (PA §8.2)',
             'it is a personal CLI that evaluation never uses (U4), so nothing here updates it'),
}


class Refused(Exception):
    """A request that cannot have a plan: exit 2, with the reason on stderr."""


def find(components, name):
    for component in components:
        if component['id'] == name:
            return component
    raise Refused(explain(f'no component is named {name}', ['it is not declared in infra/components.yml'],
                          f'{cli_name()} ops update check', 'use an id from the first column of that table'))


def refusal(component):
    gate = (f"gate: {component.get('gate')}", 'lift the gate first; infra/components.yml holds the policy')
    why, step = FROZEN_BECAUSE.get(component['owner'], gate)
    return explain(f"{component['id']} is frozen, so it has no update plan", [why],
                   f'{cli_name()} ops update observe', step)


def _row(text, name, where):
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) > 1 and cells[0] == name:
            return cells
    raise ValueError(f'{where} has no row for {name}')


def _flow(section, step):
    found = re.search(rf'^{step}\s+(\S.*)$', section, re.MULTILINE)
    if not found:
        raise ValueError(f'PA §8.1 has no {step} step')
    return found[1]


def _steps(root, row):
    section = (Path(root) / PROCEDURES).read_text().split('### 8.1', 1)[1].split('### 8.2', 1)[0]
    cells = _row(section, row, 'PA §8.1') if row else None
    return {'validate': _flow(section, 'validate'), 'apply': cells[3] if cells else NO_ROW['apply'],
            'verify': _flow(section, 'verify'), 'rollback': cells[4] if cells else NO_ROW['rollback']}


def _approvals(root, wanted):
    text = (Path(root) / APPROVALS).read_text()
    return [{'id': name, 'what': _row(text, name, APPROVALS)[1]} for name in wanted]


def _candidate(component, entry):
    """The saved answer compared again with the pin as it is now, since the pin may have moved since the check."""
    if component['source']['type'] == 'observed':
        return {'latest': None, 'status': 'observed', 'reason': 'no upstream is declared'}
    latest = (entry or {}).get('latest')
    if not (isinstance(latest, str) and latest):
        return {'latest': None, 'status': 'unknown',
                'reason': (entry or {}).get('reason') or f'not read yet: run {cli_name()} ops update check'}
    return {'latest': latest, 'status': compare(component, latest), 'fetched_at': entry.get('fetched_at'),
            'stale': bool(entry.get('stale'))}


def _observed_hosts(report, name):
    for entry in (report or {}).get('components', []):
        if entry.get('id') == name:
            return entry.get('hosts', {})
    return {}


def plan(component, latest, observed, root):
    """The plan for one component from the saved reports. LATEST is by id; OBSERVED is the last observe report or None."""
    if component['policy'] == 'frozen':
        raise Refused(refusal(component))
    row, approvals, capacity = ROUTES.get(component['id']) or ROUTES[component['kind']]
    return {'schema_version': SCHEMA_VERSION, 'id': component['id'], 'kind': component['kind'], 'owner': component['owner'],
            'policy': component['policy'], 'hosts': component['hosts'], 'pinned': pinned(component),
            'lock': component['pin']['lock'], 'observed': _observed_hosts(observed, component['id']),
            'observed_at': (observed or {}).get('observed_at'), 'candidate': _candidate(component, latest.get(component['id'])),
            'gate': component.get('gate'), 'capacity': capacity, 'approvals': _approvals(root, approvals),
            'procedure': row, 'steps': _steps(root, row), 'note': NOTES.get(component['id'])}


def _candidate_line(candidate):
    if candidate['latest'] is None:
        return f"none: {candidate['reason']}"
    stale = ', stale' if candidate['stale'] else ''
    return f"{candidate['latest']}, {candidate['status']} (read {candidate['fetched_at']}{stale})"


def screen_plan(shown):
    seen = [f"{host} {reading['value']}" + (f" ({reading['reason']})" if reading.get('reason') else '')
            for host, reading in shown['observed'].items()] or ['none recorded']
    lines = [f"{shown['id']}: {shown['kind']}, owner {shown['owner']}, policy {shown['policy']}, "
             f"on {', '.join(shown['hosts']) or 'no host'}",
             f"{'pinned':<11}{shown['pinned']} in {shown['lock']}",
             f"{'observed':<11}{seen[0]}", *(f"{'':<11}{line}" for line in seen[1:]),
             f"{'candidate':<11}{_candidate_line(shown['candidate'])}",
             f"{'gate':<11}{shown['gate'] or 'none declared'}",
             f"{'capacity':<11}{shown['capacity']}"]
    for index, approval in enumerate(shown['approvals']):
        lines.append(f"{'approval' if not index else '':<11}{approval['id']} {approval['what']}")
    lines.append(f"steps ({'PA §8.1: ' + shown['procedure'] if shown['procedure'] else 'PA §8.1 has no row for this component'})")
    lines += [f"  {step:<9}{text}" for step, text in shown['steps'].items()]
    if shown['note']:
        lines.append(f"{'note':<11}{shown['note']}")
    lines.append('Nothing was changed, and nothing here applies these steps (ADR 0009).')
    return '\n'.join(lines)
