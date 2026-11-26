#!/usr/bin/env python3
"""Report where the live system differs from this checkout. Reads only; exit 1 on drift.

Compares the installed units with their templates, what the gateways loaded with the current
configuration, the deployed revision with HEAD, the route with what must carry it, failed units on
every host of ours, each node's disk with what one more update needs, the manifest the route's front
serves with the one this checkout declares, and both evaluation images with what they are built from.
Each line is `ok`, `drift` (something to act on) or `note` (worth knowing, nothing is wrong). A host
that cannot be reached is a note: a borrowed machine being off is not drift. Exit 0 with nothing to act
on, 1 on drift, 3 when the report itself could not be made.
"""
import argparse
import itertools
import json
from pathlib import Path
import re
import subprocess
import sys

import jinja2

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
sys.path.insert(0, str(CODE / 'scripts'))
import borrow  # noqa: E402
from agentiac import claims  # noqa: E402
from agentiac import declared  # noqa: E402
from agentiac import identities  # noqa: E402
import evalimage  # noqa: E402
import route  # noqa: E402
import storage  # noqa: E402
from agentiac.errors import command as cli_name  # noqa: E402

CONTROL_UNITS = ('gateway.service', 'chatgpt.service', 'evaluation-watch.service', 'evaluation-watch.timer',
                 'evaluation-watch-health.service', 'evaluation-watch-health.timer',
                 'update-check.service', 'update-check.timer', 'recovery.service', 'recovery.timer')


def template(name, **variables):
    templates = jinja2.Environment(loader=jinja2.FileSystemLoader(CODE / 'infra/templates'), undefined=jinja2.StrictUndefined,
                                   keep_trailing_newline=True)
    return templates.get_template(f'{name}.j2').render(**variables)


def compare(subject, wanted, installed):
    if installed is None:
        return 'drift', subject, 'not installed'
    return ('ok', subject, 'matches its template') if wanted == installed else ('drift', subject, 'differs from its template')


def control_units(hosts):
    """Control's units, including one forward per host with a persistent tunnel unit."""
    # What Ansible renders control's units with: the local host's facts over all.vars (gateway ports, D8.2), and
    # its service account.
    local = next((host for host in hosts.values() if host.get('ansible_connection') == 'local'), {})
    facts = {**local, 'ansible_user': declared.local_account(hosts, ROOT)}
    pairs = [(declared.resource(name, ROOT), template(name, code_root=str(CODE), instance_root=str(ROOT), **facts))
             for name in CONTROL_UNITS]
    for name, host in hosts.items():
        if ('default_profile' in host or host.get('spark_tensorfold') or host.get('default_backend')) and 'tunnel_port' in host and host.get('ansible_connection') != 'local':
            pairs.append((declared.resource(f'tunnel-{name}.service', ROOT), template('tunnel.service', inventory_hostname=name, **host)))
    for unit, wanted in pairs:
        path = Path('/etc/systemd/system') / unit
        yield compare(unit, wanted, path.read_text() if path.exists() else None)


def revision():
    """The deployed site and core revisions against this checkout (D6.3, T9.8)."""
    path = ROOT / 'artifacts/deployed.json'
    if not path.exists():
        yield 'note', 'deploy', 'no deploy recorded yet'
        return
    deployed = json.loads(path.read_text())
    site = deployed.get('site') or deployed['revision']
    core = deployed.get('core') or site
    head = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    changed = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain'], capture_output=True, text=True).stdout.strip()
    core_dir = ROOT / paths.CORE_DIR
    has_core = core_dir.is_dir()
    core_head = (subprocess.run(['git', '-C', str(core_dir), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
                 if has_core else head)
    text = site[:8] if not has_core and core == site else f'{site[:8]} (core {core[:8]})'
    if deployed.get('dirty'):
        yield 'drift', 'deploy', f'{text} was deployed with uncommitted changes'
    elif site == head and core == core_head:
        yield 'ok', 'deploy', f'{text} is HEAD'
    else:
        head_text = head[:8] if not has_core and core_head == head else f'{head[:8]} (core {core_head[:8]})'
        yield 'note', 'deploy', f'{text} deployed, HEAD is {head_text}; run {cli_name()} ops deploy --check'
    if changed:
        yield 'note', 'checkout', f'{len(changed.splitlines())} uncommitted paths'


def gateways():
    for kind in route.SOURCES:
        if route.pending(kind):
            yield 'drift', kind, f'running configuration is not the current one; {cli_name()} ops deploy'
        else:
            yield 'ok', kind, 'running the current configuration'


def tunnel_active(name):
    return subprocess.run(['systemctl', 'is-active', '--quiet', declared.resource(f'tunnel-{name}', ROOT)]).returncode == 0


def front_answers(name, host):
    try:
        return borrow.ondemand_call(name, host, 'GET', 'status', timeout=10)[0] == 200
    except OSError:  # no key: never claimed
        return False


def route_path(hosts):
    """Everything local-coder needs on the way to its host."""
    target = route.current()
    node = json.loads(route.ROUTES.read_text())['targets'][target]['node']
    if hosts.get(node, {}).get('ansible_connection') == 'local':
        yield 'note', 'route', f'{target} serves on {node}'   # the local host: no tunnel to check
    elif not tunnel_active(node):
        yield 'drift', 'route', f'{target}: tunnel to {node} is down; {cli_name()} reclaim'
    elif not front_answers(node, hosts[node]):
        yield 'drift', 'route', f'{target}: no on-demand front on {node}; {cli_name()} reclaim'
    else:
        yield 'ok', 'route', f'{target}: tunnel and front answer'


def front_identity(hosts):
    """The manifest the route's front serves against the one this checkout declares (T4.1).

    A manifest reaches a worker only at a claim, but the gateway learns the new hash at a deploy. So an
    edit, even a cosmetic one, leaves the two apart, and from the next gateway start the alias answers
    503 `manifest_hash` until a re-claim. This line is that fact before it costs a request.
    """
    target = route.current()
    spec = json.loads(route.ROUTES.read_text())['targets'][target]
    expected = route.identity_policy.expected_identity(route.ROOT, spec['backend'], spec.get('context_window')) \
        if spec.get('backend') else None
    if expected is None:
        return
    try:
        code, status = borrow.ondemand_call(spec['node'], hosts[spec['node']], 'GET', 'status', timeout=10)
    except OSError:     # no front to ask: route_path says so
        return
    if code != 200:
        return
    reported = status.get('identity')
    problem = route.identity_policy.identity_problem(expected, reported)
    if reported is None:
        yield 'note', 'front identity', f'{target}: the front on {spec["node"]} reports no identity'
    elif problem:
        yield 'drift', 'front identity', (f'{target}: {spec["node"]} serves another {problem} than this checkout '
                                          'declares; re-claim it, or a gateway restart makes the alias answer 503')
    else:
        yield 'ok', 'front identity', f'{target}: {spec["node"]} serves the declared manifest'


def tunnels(hosts):
    """Transient tunnels are left from a claim; one with nothing behind it serves nobody."""
    prefix = declared.setting('resource_prefix', ROOT)
    listed = subprocess.run(['systemctl', 'list-units', '--plain', '--no-legend', '--state=active', f'{prefix}-tunnel-*'],
                            capture_output=True, text=True).stdout
    for name in re.findall(rf'^{re.escape(prefix)}-tunnel-(\S+)\.service', listed, re.M):
        host = hosts.get(name)
        if claims.read(ROOT, name) is not None:
            continue  # the `claim` line reports a recorded claim whose front does not answer
        if host and host.get('borrowed') and not front_answers(name, host):
            yield 'drift', f'tunnel {name}', f'no claim behind it; {cli_name()} release {name}'


def claim_records(hosts):
    """What control wrote down at a claim against what answers now (docs/implementation-plan, F05)."""
    for name, record in sorted(claims.all(ROOT).items()):
        host = hosts.get(name)
        if host is None:
            yield 'drift', f'claim {name}', 'a claim record names a host the inventory no longer has; remove artifacts/claims/' + name + '.json'
        elif not front_answers(name, host):
            since = record.get('claimed_at', 'an unknown time')
            yield 'drift', f'claim {name}', (f"claimed {since} ({record.get('backend')}), but no front answers; "
                                             f"{cli_name()} {'claim ' + name if not host.get('borrowed') else 'release ' + name}")
        else:
            yield 'ok', f'claim {name}', f"claimed {record.get('claimed_at')} ({record.get('backend')}); front answers"


def failed_units():
    listed = subprocess.run(['systemctl', 'list-units', '--plain', '--no-legend', '--state=failed', 'agentiac*'],
                            capture_output=True, text=True).stdout
    units = [line.split()[0] for line in listed.splitlines()]
    if units:
        yield 'note', 'failed units', f"{len(units)} on control, such as {units[0]}"


def worker_failed_units(hosts):
    """Failed units on the workers too (IP-08). `failed_units()` sees control only, so a failed one-off on
    Spark — a duplicate preparation rejected by its lock, say — never reached this report, and the
    stability decision in storage-lifecycle-plan.md §3.2 asks for failed units on every node.
    """
    for name, host in hosts.items():
        if host.get('ansible_connection') == 'local':
            continue
        argv = ['systemctl', 'list-units', '--plain', '--no-legend', '--state=failed']
        try:
            done = borrow.on_host(host, argv, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            yield 'note', f'failed units {name}', 'host not reachable; not checked'
            continue
        if done.returncode == 255:
            yield 'note', f'failed units {name}', 'host not reachable; not checked'
            continue
        # No `<prefix>*` pattern: ssh hands the argument to a remote shell, which expands the glob
        # against files in the home directory (`~/agentiac` is one), and systemctl then hears a unit
        # name that exists nowhere. Filtering the names here is what actually matches.
        units = [line.split()[0] for line in done.stdout.splitlines()
                 if line.split() and line.split()[0].startswith(declared.setting('resource_prefix', ROOT))]
        if units:
            yield 'note', f'failed units {name}', f"{len(units)} on {name}, such as {units[0]}"


def disk(hosts):
    """Free space, room for one more root and unregistered leftovers per node (F7). Reads only."""
    for node in storage.report(hosts):
        yield storage.notes(node)


def eval_image():
    """The benchmark image against the Dockerfile it is built from (IP-13). Control-local, reads only.

    Nothing compared them before: the image predates the Dockerfile's own commit by eleven minutes, so
    `benchmarks/Dockerfile` described something the served image might not be. A build without a label
    cannot be checked after the fact, so an unlabelled image is a note saying what is unknown, and an
    image built from an edited Dockerfile is drift.
    """
    state, detail = evalimage.check()
    yield (state, 'eval image', detail)


def suite_image():
    """The task suite's image against its lock: an edit here would grade against tools nobody reviewed."""
    state, detail = evalimage.check_suite()
    yield (state, 'suite image', detail)


def front_directory(host):
    """Where a persistent front's unit runs from: claim installs it in the root it serves, which for a worker with a
    strata_backend (ADR 0011, 0012) is that manifest's Strata root, not project_root."""
    if 'strata_backend' in host:
        return borrow.strata_root(host, host['strata_backend']) + '/ondemand'
    return host['project_root'] + '/ondemand'


def declared_services(name):
    """Every unit a manifest declares for host NAME, as (role, root): an engine that says how it is served.

    `infra/unit.yml` installs such a unit and `scripts/borrow.py` starts and stops it, so it is a permanent unit of
    ours like the TensorFold service, and its text has to agree with this checkout like every other one. A role the
    site has no text for is reported rather than compared: that is the declaration `ops prepare` already refuses to
    claim with, and drift is where a site sees that it is missing.
    """
    found = set()
    prefix = declared.setting('resource_prefix', ROOT)
    for path in sorted((ROOT / 'backends').glob('*.json')):
        try:
            manifest = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict) or str(manifest.get('status', '')).startswith('retired'):
            continue
        worker = (manifest.get('workers') or {}).get(name)
        service = (worker or {}).get('service') if isinstance(worker, dict) else None
        unit = service.get('unit') if isinstance(service, dict) else None
        if not isinstance(unit, str) or not unit.startswith(f'{prefix}-'):
            continue
        role = unit.removeprefix(f'{prefix}-')
        if not role:
            continue
        found.add((role, identities.service_root(manifest, name)))
    return sorted(found, key=lambda item: (item[0], item[1] or ''))


def worker_units(hosts):
    """Permanent units on workers. Transient ones have no file to compare."""
    prefix = declared.setting('resource_prefix', ROOT)
    for name, host in hosts.items():
        candidates = {}
        if host.get('ondemand_persistent'):
            unit = declared.resource('ondemand.service', ROOT)
            candidates[unit] = [template('ondemand.service', directory=front_directory(host),
                                         ansible_user=declared.account(host, ROOT))]
        elif host.get('spark_tensorfold'):
            unit = declared.resource('spark-tensorfold.service', ROOT)
            candidates[unit] = [template('spark-tensorfold.service', directory=borrow.tensorfold_root(host),
                                         ansible_user=declared.account(host, ROOT))]
        for role, root in declared_services(name):
            unit = declared.resource(f'{role}.service', ROOT)
            if role not in borrow.installed_unit_roles():
                yield 'note', f'{name} {unit}', \
                    'a manifest declares this service and infra/unit.yml has no role to install it'
                continue
            if not root:
                yield 'note', f'{name} {unit}', 'the manifest declares this unit with no `root` to compare against'
                continue
            try:
                rendered = template(f'{role}.service', directory=root, ansible_user=declared.account(host, ROOT),
                                    resource_prefix=prefix)
            except jinja2.TemplateNotFound:
                yield 'note', f'{name} {unit}', f'infra/templates/{role}.service.j2 is missing; not compared'
                continue
            # Candidate manifests can share one unit role (e.g. container-service) across service roots.
            candidates.setdefault(unit, []).append(rendered)
        for unit, variants in candidates.items():
            try:
                done = borrow.on_host(host, ['cat', f'/etc/systemd/system/{unit}'], timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                done = None
            if done is None or done.returncode == 255:
                yield 'note', f'{name} {unit}', 'host not reachable; not compared'
            elif done.returncode != 0:
                yield compare(f'{name} {unit}', variants[0], None)
            elif done.stdout in variants:
                yield 'ok', f'{name} {unit}', 'matches its template'
            else:
                yield 'drift', f'{name} {unit}', 'differs from its template'


def sections(hosts):
    """One generator per check, unstarted, so a reader can survive one of them failing (T8.3)."""
    return (control_units(hosts), revision(), gateways(), route_path(hosts), front_identity(hosts), tunnels(hosts),
            claim_records(hosts), failed_units(), worker_units(hosts), worker_failed_units(hosts), disk(hosts), eval_image(),
            suite_image())


def report(hosts):
    yield from itertools.chain.from_iterable(sections(hosts))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--json', action='store_true', help='one JSON document instead of the lines')
    args = parser.parse_args(argv)
    try:
        lines = []
        for line in report(borrow.hosts()):
            lines.append(line)
            if not args.json:
                print(f'{line[0]:6}{line[1]:50}{line[2]}', flush=True)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'the report stopped: {type(error).__name__}: {error}', file=sys.stderr)
        return 3
    drift = any(state == 'drift' for state, _, _ in lines)
    if args.json:
        print(json.dumps({'schema_version': 1, 'drift': drift,
                          'checks': [dict(zip(('state', 'subject', 'detail'), line)) for line in lines]}))
    return 1 if drift else 0


if __name__ == '__main__':
    sys.exit(main())
