"""Rules for infra/retention.yml (T5.1; ADR 0008; storage-lifecycle-plan.md §3.1, §4.2; platform-architecture.md §7.1, §11).

The ledger is the declared answer to "what does this host hold, and in what state". This module is what makes
it agree with the rest of the declaration: a generation it names has to be a backend manifest that serves that
host, at the root `infra/inventory.yml` declares for it, and every route target has to name a generation the
ledger holds. `make check` fails on the disagreement (tests/test_retention.py, and `declared.references`).

It reads files only. It never deletes, moves or measures anything — detection is automatic, deletion is
approved (ADR 0008) — and `candidates` is the read T5.2's `ops retire` and PA §11's `ops storage --candidates`
are meant to feed on, which is why `serving` and the out-of-scope classes are excluded there by a rule with
tests rather than by a promise every caller remembers to make.
"""
import json
from pathlib import Path

from agentiac import identities, paths

ROOT = paths.root()
CODE = paths.CODE
SCHEMA = CODE / 'infra/retention.schema.json'

HELD = ('serving', 'rollback', 'candidate', 'quarantine')   # storage-lifecycle-plan.md §3.1: on disk
STATES = HELD + ('retired',)
AREAS = ('staging', 'areas')
CLASSES = ('persistent', 'managed-cache', 'ephemeral', 'user-owned', 'unknown')   # PA §11
OUT_OF_SCOPE = ('user-owned', 'unknown')   # §3.4 and PA §11: never a candidate, never what a command targets


def shape(ledger):
    """The file's form, as 'where: what' strings."""
    import jsonschema  # requirements-control.lock; only `make check` and its tests need it
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    return [f'infra/retention.yml {"/".join(map(str, error.absolute_path)) or "root"}: {error.message}'
            for error in sorted(validator.iter_errors(ledger), key=lambda e: list(map(str, e.absolute_path)))]


def host_roots(facts, host_name=None, manifests=()):
    """The engine roots declared for one node: the inventory's (`strata_trial_roots`, `strata_root`) and, with
    HOST_NAME and MANIFESTS given, the roots those manifests declare for it (`workers.<host>.service.root`).

    The second half is what an engine that serves from a container has: no trial root and no host-wide flag, only
    its own declaration. A ledger that cannot name it would report the directory that engine prepares into as a
    root this host holds nothing in - and `ops retire` would be unable to name the generation at all.
    """
    roots = list((facts.get('strata_trial_roots') or {}).values())
    if facts.get('strata_root'):
        roots.append(facts['strata_root'])
    if host_name:
        roots += [root for root in (identities.service_root(manifest, host_name) for manifest in manifests) if root]
    return sorted(set(roots))


def inventory_root(facts, stem, manifest, host_name=None):
    """The root one backend is served from on one node: what the manifest declares first, then the inventory.

    A manifest that declares its `service` says where that engine is served, which is more specific than any
    host-wide root map; a manifest that does not is served from the root `infra/inventory.yml` declares for it,
    by manifest ID then engine version.
    """
    if host_name:
        declared_root = identities.service_root(manifest, host_name) if manifest else None
        if declared_root:
            return declared_root
    roots = facts.get('strata_trial_roots') or {}
    if stem in roots:
        return roots[stem]
    version = (manifest or {}).get('source_version')
    if version and version in roots:
        return roots[version]
    return facts.get('strata_root')


def handle(entry):
    """What a line is about, in the order the file names things: an engine, then a name, then a path or an image."""
    return entry.get('backend') or entry.get('name') or entry.get('path') or entry.get('image')


def _where(host, state, entry):
    return f'infra/retention.yml {host} {state} {handle(entry)}'


def problems(ledger, nodes, backends, route):
    """Every way the ledger can disagree with the inventory, the manifests or the route table. Empty means sound."""
    found = shape(ledger)
    found += [f'infra/retention.yml: {host} appears in infra/inventory.yml and holds nothing in the ledger'
              for host in sorted(nodes) if host not in ledger]
    found += [f'infra/retention.yml host {host}: is not a node in infra/inventory.yml'
              for host in sorted(ledger) if host not in nodes]
    if found:
        return found      # the rules below read node facts; on a malformed file they would only add noise

    for host, block in sorted(ledger.items()):
        facts = nodes[host]
        roots = host_roots(facts, host, list(backends.values()))
        staging = [entry['path'] for entry in block['staging'] if 'path' in entry]
        held, retired, targets = {}, set(), {spec['backend'] for spec in route['targets'].values()
                                             if spec['node'] == host and 'backend' in spec}

        for state in HELD:
            for entry in block[state]:
                stem, path = entry.get('backend'), entry.get('path')
                where = _where(host, state, entry)
                manifest = backends.get(stem) if stem else None
                if stem and manifest is None:
                    found.append(f'{where}: no backends/{stem}.json declares it')
                elif stem and host not in (manifest or {}).get('workers', {}):
                    found.append(f'{where}: backends/{stem}.json does not name {host} as a worker, so nothing there is prepared for it')
                if stem:
                    held.setdefault(stem, []).append(state)
                if entry.get('class') in OUT_OF_SCOPE:
                    found.append(f'{where}: {entry["class"]} is out of scope (§3.4), so it cannot be a held generation')
                if 'form' not in entry:
                    found.append(f'{where}: holds something without saying whether it is full or thin (ADR 0008)')
                if entry.get('retirement') and state == 'serving':
                    found.append(f'{where}: what serves is never a retirement candidate (§4.3)')
                if manifest is not None and state != 'quarantine':
                    want = inventory_root(facts, stem, manifest, host)
                    who = (f'backends/{stem}.json' if identities.service_root(manifest, host) else 'infra/inventory.yml')
                    if want and not path:
                        found.append(f'{where}: {who} declares {want} for it and the ledger names no root')
                    elif want and want != path:
                        found.append(f'{where}: the ledger says {path}, {who} declares {want}')
                    elif not want and path and roots:
                        found.append(f'{where}: {path} is no root infra/inventory.yml or these manifests declare '
                                     f'for {host}')
                elif path and state != 'quarantine' and roots and path not in roots:
                    found.append(f'{where}: {path} is no root infra/inventory.yml or the manifests declare for '
                                 f'{host}')
                if path and state == 'quarantine' and not any(path == s or path.startswith(s + '/') for s in staging):
                    found.append(f'{where}: {path} is not under a staging directory this file names for {host} (§5.5)')

        for entry in block['retired']:
            stem, path = entry.get('backend'), entry.get('path')
            where = _where(host, 'retired', entry)
            retired.add(stem or handle(entry))
            if 'form' in entry or entry.get('retirement'):
                found.append(f'{where}: a retired thing is neither full nor thin nor a candidate — it is gone')
            if path and path in roots:
                found.append(f'{where}: {path} is retired here and still a declared root in infra/inventory.yml')
            if stem and stem in targets:
                found.append(f'{where}: a route target still serves {stem} on {host}, which this file calls retired')

        for stem, states in sorted(held.items()):
            if len(states) > 1:
                found.append(f'infra/retention.yml {host} {stem}: held as {", ".join(states)} — one thing is one generation')
            if stem in retired:
                found.append(f'infra/retention.yml {host} {stem}: held and retired at once')

        expected = facts.get('strata_default_backend') or facts.get('strata_backend')
        if expected:
            served = [entry.get('backend') for entry in block['serving']]
            if served != [expected]:
                found.append(f'infra/retention.yml {host} serving: a claim there resolves to {expected}, the ledger '
                             f'names {", ".join(served) if served else "nothing"}')
        thinned = {handle(entry) for state in HELD for entry in block[state] if entry.get('form') == 'thin'}
        for target, spec in sorted(route['targets'].items()):
            if spec['node'] == host and 'backend' in spec and spec['backend'] not in held:
                found.append(f'gateway/local-coder.json target {target}: serves {spec["backend"]} on {host}, '
                             f'which the ledger does not hold there')
            if spec['node'] == host and spec.get('backend') in thinned:
                # F6 (T5.3): a thin root does not hold its weights and does not start, so nothing may be routed to
                # it. The §5.3 order is what makes this satisfiable: the target goes in the commit that thins.
                found.append(f'gateway/local-coder.json target {target}: names {spec["backend"]} on {host}, which '
                             'the ledger records as `form: thin`. A thin root holds no weights: remove the target '
                             'in the commit that thins it, or restore the root first')

        ours = {entry['path'] for state in AREAS for entry in block[state]
                if entry.get('path') and entry.get('class') == 'persistent'}
        theirs = {entry['path'] for state in AREAS for entry in block[state]
                  if entry.get('path') and entry.get('class') in OUT_OF_SCOPE}
        for root in roots:
            if not any(root == path or root.startswith(path + '/') for path in ours):
                found.append(f'infra/retention.yml {host}: {root} is a declared root and no `class: persistent` '
                             f'line says where the roots of this host live')
        for path in sorted(theirs):
            for other in sorted(ours | set(roots)):
                if path == other or path.startswith(other + '/') or other.startswith(path + '/'):
                    found.append(f'infra/retention.yml {host} areas {path}: out of scope, yet it overlaps {other}, '
                                 f'which is ours (§3.4)')
                    break
        for state in HELD + ('retired',) + AREAS:
            for entry in block[state]:
                if entry.get('retirement') == 'candidate' and entry.get('class') in OUT_OF_SCOPE:
                    found.append(f'{_where(host, state, entry)}: {entry["class"]} is out of scope (§3.4), '
                                 f'so nothing here may mark it goable')
                # T5.2: §3.2 is a judgement about the rollback root, and `ops thin` reads it from here. The
                # judgement itself is runtime state, so the line names where its evidence lives.
                if (entry.get('confirmed') or entry.get('evidence')) and state != 'rollback':
                    found.append(f'{_where(host, state, entry)}: §3.2 is judged about a rollback, so only a '
                                 'rollback line may record that it was')
                if entry.get('confirmed') and not entry.get('evidence'):
                    found.append(f'{_where(host, state, entry)}: a `confirmed` date with no `evidence` path is a '
                                 'judgement with nothing behind it (§4.2)')
                if entry.get('evidence') and not entry.get('confirmed'):
                    found.append(f'{_where(host, state, entry)}: an `evidence` path with no `confirmed` date says '
                                 'a file exists without saying what it decided')
    return found


def candidates(ledger):
    """What the ledger itself marks as goable, read only: `retirement: candidate` on something not serving.

    The exclusion lives here, with tests, rather than in each caller: `serving` is never a candidate (§4.3),
    `user-owned` and `unknown` never are (§3.4, PA §11), and a `declined` line is a decision already taken.
    `ops storage --candidates` prints this list and `ops retire` acts on it (T5.2) — which is why a candidate
    names a path or an image: a line with neither is a note about a thing, not something a command can point at.
    """
    found = []
    for host, block in sorted(ledger.items()):
        for state in ('quarantine', 'rollback', 'candidate') + AREAS:
            for entry in block[state]:
                if entry.get('retirement') != 'candidate' or entry.get('class') in OUT_OF_SCOPE:
                    continue
                found.append({'host': host, 'state': state, 'handle': handle(entry), 'backend': entry.get('backend'),
                              'name': entry.get('name'), 'class': entry['class'],
                              'form': entry.get('form'), 'path': entry.get('path'), 'image': entry.get('image'),
                              'reason': entry['reason']})
    return found
