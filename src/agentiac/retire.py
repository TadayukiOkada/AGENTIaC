"""The gate every retirement, thinning and restore passes through (T5.2; ADR 0008;
storage-lifecycle-plan.md §3.1, §4.1, §4.3; platform-architecture.md §11).

`src/agentiac/retention.py` is what *declares* what a host holds. This module is what refuses to delete
something on the strength of that declaration: the §4.3 gate, one check per refusal, each one named in the
report so a person reads why a command stopped rather than guessing. Nothing here touches a disk; the checks
are given what `scripts/retire-worker.py` measured on the host and what the declaration says, and they answer
`ok`, `refuse` or `unknown`.

The refusals, in the order a person should read them:

  ledger    the command names something `infra/retention.yml` says nothing about, or a state that stage cannot
            act on. A retirement starts as a line in git, not as a path on the command line.
  serving   what serves is never a target (§4.3), and "serves" is read four ways: the ledger's `serving` line,
            what a claim with no `--backend` resolves to, a local-coder target on that host — including the one
            `local-coder` routes through right now — and the front the host actually runs, read from the
            `agentiac-ondemand` unit's own `ExecStart`.
  candidate `--quarantine` acts on a `retirement: candidate` line only. An absence from the ledger is not a
            licence; §4.2 says so, and `retention.candidates()` is the same read.
  path      normalisation before anything else: absolute, no `..`, no symlink, `realpath` equal to the name,
            inside a root the inventory declares or under the staging this file names.
  area      inside a `class: persistent` area of the ledger and outside every `user-owned` / `unknown` one
            (§3.4). A shared disk is not ours to clean.
  mount     not a mount point, and — for a move — on the same filesystem as its destination (§5.5: an isolated
            thing stays on the file system it came from, so it can be moved back).
  present   the path is there. A missing one is a ledger correction, not a deletion.
  in-use    no process has it open, no unit names it, and the on-demand front this host RUNS does not start from
            it or keep its key or manifest inside it. That is read off the unit's `ExecStart`, not off the
            `ondemand/config.json` sitting in the root: the file says what a claim would run there, not what is
            running (the same reading `enable_ondemand` uses). A root a front
            is installed in is serving even unloaded; the residue of a claim that moved elsewhere is reported on
            the `serving` line rather than refused, because refusing it would claim the host serves a root it does
            not serve.
  stage     `--purge` deletes only a path the ledger calls `quarantine` and that the quarantine record names.
  wait      24 hours have passed since that record (§3.1, §5.5), or a person wrote down why they shortened it.
            `--check` reads it for a `quarantine` line as well: the isolation age is a fact about the state, not
            about the stage being run, so a rehearsal cannot end by pointing at a `--purge` its own record forbids.
  hashes    `ops thin` removes only a weight another root on this host holds identically — §3.2's "a copy on
            that host apart from the root it thins" — verified against the manifest pin on both sides, and
            refuses a weight shared by inode with anything else.
  confirmed `ops thin` needs §3.2 judged: the ledger's rollback line carries `confirmed` and names the evidence
            under `artifacts/lifecycle/`.
  spare     after thinning, the host still has the rollback spare — the shared weights plus the owner's floor —
            free, or it is not a host that can be rolled back (ADR 0008).
  image     `--image TAG` is the gate for a ledger line that names an image rather than a path. It refuses unless
            the line carries `retirement: candidate`, unless a declaration names the tag (an inventory
            `runtime_image`, a backend manifest's image), or unless something on the host points at it — a unit
            file, `node.json`, `active-profile.json` or a container of any state. When it passes it reports the
            `docker rmi` and does not run it: this command deletes paths, and a tag is not a path (§3.3, §5.6).

`ops retire` reads the ledger marker, so the §5.3 order (the references go first, in a commit; the directory
after) is representable: the engine's line becomes a `quarantine` line naming the staging path, which needs no
manifest and no inventory key, and that line is what authorises `--purge`.
"""
import datetime

from agentiac import retention
from agentiac.errors import command as cli_name

FLOOR_GIB = 40                      # the owner's floor, the same number prepare holds (S §2.2)
SPARE_WEIGHTS_GIB = 85.6            # ADR 0008: what a thin N-1 must be able to put back
QUARANTINE_HOURS = 24               # §3.1, §5.5: isolated and watched, then deleted
STAGES = ('check', 'quarantine', 'purge', 'thin', 'restore')
GIB = 1024**3

# What each ledger state allows a stage to do with it (§3.1's arrows). `check` is always allowed: it is the
# rehearsal, and the stage that a state allows is what the report then names.
ALLOWED = {'serving': (), 'rollback': ('quarantine', 'thin', 'restore'), 'candidate': ('quarantine',),
           'quarantine': ('purge',), 'retired': (), 'staging': (), 'areas': ()}

OK, REFUSE, UNKNOWN = 'ok', 'refuse', 'unknown'


def check(check_id, status, evidence):
    return {'id': check_id, 'status': status, 'evidence': evidence}


def gib(bytes_):
    return round(bytes_ / GIB, 1)


def under(path, parent):
    """`path` is parent itself or below it, on the names rather than on a prefix match."""
    return path == parent or path.startswith(parent.rstrip('/') + '/')


def inside_any(path, parents):
    return any(under(path, parent) for parent in parents)


def areas(ledger_host):
    """(ours, not ours) — the paths this file says where each host's objects live, and the ones it puts out of
    scope. §3.4: what cannot be told from the owner's data is never a target and never a candidate."""
    ours, theirs = [], []
    for entry in ledger_host['staging'] + ledger_host['areas']:
        if 'path' not in entry:
            continue
        (ours if entry['class'] in ('persistent', 'managed-cache') else theirs).append(entry['path'])
    return ours, theirs


def resolve(ledger_host, handle):
    """(state, entry) of the ledger line HANDLE names, or (None, None) when the file says nothing about it."""
    for state in retention.STATES + retention.AREAS:
        for entry in ledger_host[state]:
            if retention.handle(entry) == handle:
                return state, entry
    return None, None


def target(ledger_host, state, entry, host):
    """What a stage will act on: the ledger line plus the path it names, as one dict."""
    return {'host': host, 'state': state, 'handle': retention.handle(entry), 'backend': entry.get('backend'),
            'path': entry.get('path'), 'image': entry.get('image'), 'class': entry.get('class'),
            'form': entry.get('form'), 'retirement': entry.get('retirement'),
            'confirmed': entry.get('confirmed'), 'evidence': entry.get('evidence'), 'reason': entry['reason']}


def references(nodes, route, current, host, want):
    """Every declared and runtime reference that still names this engine: the route table's targets on this
    host, what a claim with no --backend resolves to there, and the target `local-coder` routes through now.

    §4.3's second condition is "the current route, a claim and the front's configuration do not refer to it".
    A path is reached through the root the inventory resolves the backend to, so this returns the backend names
    and the caller compares the roots (a retired manifest resolves to nothing, which is the answer we want).
    """
    found = []
    for name, spec in sorted(route['targets'].items()):
        if spec['node'] == host and 'backend' in spec:
            found.append({'kind': 'target', 'name': name, 'backend': spec['backend'],
                          'current': name == current})
    facts = nodes.get(host) or {}
    for key in ('strata_default_backend', 'strata_backend'):
        if facts.get(key):
            found.append({'kind': 'claim default', 'name': key, 'backend': facts[key], 'current': False})
    return [entry for entry in found if entry['backend'] == want]


def image_references(nodes, tag, group_vars=None):
    """Declarations that still name this tag (§5.3: these go first, in a commit).

    `runtime_image` is declared under `all.vars`, so every worker inherits it unless a host overrides it: a tag
    named anywhere is not removable anywhere, and the refusal has to say which nodes name it. Backend manifests
    name their own images (a TensorFold generation), and those are declarations too — read by the caller, which
    can see the manifest graph."""
    found = [f'infra/inventory.yml {name} runtime_image' for name, facts in sorted(nodes.items())
             if facts.get('runtime_image') == tag]
    if not found and (group_vars or {}).get('runtime_image') == tag:
        found.append('infra/inventory.yml all vars runtime_image (every worker inherits it)')
    return found


IMAGE_STAGES = ('check',)


def image_gates(stage, target, nodes, reading, declared_in=(), graph_vars=None):
    """§4.3's checks for a tag rather than a path.

    The two stages of §5.5 — save the metadata, move the directory aside, watch it for 24 h — have no image
    equivalent, and §3.3 says so ("a TensorFold rollback is an image, and the two stages of ADR 0008 do not fit
    one yet"). What an image gate can do is decide *whether*, and report the one command that removes it. This
    command never runs that command.
    """
    handle, host = target['handle'], target['host']
    if target['state'] is None:
        return [check('ledger', REFUSE, f'infra/retention.yml says nothing about {handle} on {host}: a retirement '
                                        'starts as a line in the ledger, not as a tag (§4.2)')]
    found = []
    if stage not in IMAGE_STAGES:
        return [check('stage', REFUSE, f'{handle} is an image, not a path: --{stage} moves or deletes a '
                                       'directory. An image retirement has one stage, --check, which reads what '
                                       'points at the tag and reports the `docker rmi` (§3.3)')]
    if target['retirement'] == 'candidate':
        found.append(check('ledger', OK, f'infra/retention.yml {host} {target["state"]} marks {handle} '
                                         'retirement: candidate'))
    else:
        found.append(check('ledger', REFUSE, f'infra/retention.yml {host} {target["state"]} {handle} carries no '
                                             '`retirement: candidate`: a line has to say a thing may go, and '
                                             f'{target["state"]} is not that (§4.2)'))
    declarations = image_references(nodes, handle, graph_vars) + [where for where in declared_in if where]
    found.append(check('declaration', REFUSE if declarations else OK,
                       ('still named by: ' + ', '.join(declarations)) if declarations else
                       'no declaration names this tag (infra/inventory.yml runtime_image, the backend manifests '
                       'and their image pins, and the profile files were read)'))
    if reading is None:
        found.append(check('host', UNKNOWN, f'{host} did not answer: nothing about {handle} was read on it'))
        return found
    pointers = reading.get('references') or []
    found.append(check('references', REFUSE if pointers else OK,
                       (f'on {host}: ' + '; '.join(pointers)) if pointers else
                       f'nothing on {host} points at it: no agentiac unit file names it, no node.json or '
                       'active-profile.json names it, no container of any state runs it'))
    if reading.get('present'):
        found.append(check('present', OK, f'the tag is on {host}' + (f' ({reading["size"]})' if reading.get('size')
                                                                      else '')))
    else:
        found.append(check('present', UNKNOWN if target['state'] == 'retired' else REFUSE,
                           f'the tag is not on {host}: ' +
                           ('a `retired` line records a removal and this reading confirms it' if
                            target['state'] == 'retired' else
                            'the ledger line is a correction, not a deletion (§4.3: a missing thing is not a '
                            'thing to delete)')))
    return found


def thin_ledger(ledger, host, handle_):
    """F6 (T5.3): a root the ledger records as `form: thin` holds no weights, and a root without its weights does
    not start. `route set`, `claim` and `reclaim` all ask this before anything begins, and the answer names the way
    out rather than only the refusal.

    The ledger is the right place for the fact: `ops thin` is what makes a root thin and it updates the line in the
    same action, and a route switch is a decision taken on control from declared state. What the ledger cannot know
    — weights that went missing some other way — is the measured half, `weights_missing`.
    """
    ledger_host = (ledger or {}).get(host)
    if ledger_host is None or not handle_:
        return None
    state, entry = resolve(ledger_host, handle_)
    if entry is None or entry.get('form') != 'thin':
        return None
    return (f'infra/retention.yml {host} {state} {handle_} is `form: thin`: its pinned weights are not in the '
            f'root, and a thin {state} does not start until they are back (F6). Restore it first: '
            f'{cli_name()} ops restore {host} --backend {handle_}')


def weights_missing(manifest, present):
    """The measured half of F6: the manifest's weight entries the host says are not there — missing, or not the
    size the manifest pins. PRESENT is what the host answered, keyed by the manifest's own relative path."""
    return [entry for entry in (manifest.get('files') or []) if not present.get(entry['path'], True)]


def serving(check_id, target, refs, measured):
    """§4.3: what serves is never a retirement target — read from the ledger, from the claim default, from the
    route table and from the front the host runs, because those are four names for one fact and they have to agree.

    The front is read as the host runs it (`front_running`, the unit's own `ExecStart`), not as the files inside
    the root remember it: a claim that has moved on leaves its `ondemand/config.json` naming the root it used, and
    that residue is said on the line rather than made into a refusal. A root the running front's proxy lives in is
    serving even when its model is unloaded, which is the refusal this check exists for."""
    if target['state'] == 'serving':
        return check(check_id, REFUSE, f'infra/retention.yml {target["host"]} serving {target["handle"]}: '
                                       'what serves is never a retirement target (§4.3)')
    served = [f'{entry["kind"]} {entry["name"]}' for entry in refs]
    if served:
        return check(check_id, REFUSE, f'{", ".join(served)} on {target["host"]} still name '
                                       f'{target["handle"]}: remove the reference in a commit first (§5.3)')
    residue = (measured or {}).get('front_residue') or []
    if measured and measured.get('front_names_path'):
        running = measured.get('front_running') or f'{target["path"]}/ondemand'
        return check(check_id, REFUSE, f'the on-demand front {target["host"]} runs ({running}) starts from, keys '
                                       f'or manifests {target["path"]}: the claim that serves it has to be released '
                                       'first')
    if residue and measured and measured.get('front_error'):
        return check(check_id, UNKNOWN, f'{len(residue)} on-demand configuration in {target["path"]} names it, and '
                                        f'{target["host"]} could not say which front it runs: {measured["front_error"]}')
    if residue:
        return check(check_id, OK, f'no target, claim default or running front names it; the residue of an earlier '
                                   f'claim is in the root ({", ".join(residue)}), and the front {target["host"]} runs '
                                   f'is {measured.get("front_running") or "no front installed"}')
    return check(check_id, OK, 'no target, claim default or front configuration names it')


def gates(stage, target, ledger_host, refs, measured, record, evidence_present, roots):
    """Every §4.3 check for one stage, as one row each. Empty `refusals()` means the stage may run."""
    ours, theirs = areas(ledger_host)
    staging = [entry['path'] for entry in ledger_host['staging'] if 'path' in entry]
    path, handle, host = target['path'], target['handle'], target['host']
    found = []

    if target['state'] is None:
        found.append(check('ledger', REFUSE, f'infra/retention.yml says nothing about {handle} on {host}: a '
                                             'retirement starts as a line in the ledger, not as a path (§4.2)'))
        return found
    if stage not in ALLOWED[target['state']] + ('check',):
        others = ', '.join(ALLOWED[target['state']] + ('check',)) or 'nothing'
        return [check('stage', REFUSE, f'infra/retention.yml {host} {target["state"]} {handle}: --{stage} is not '
                                       f'a stage that state allows (it allows: {others})')]

    found.append(serving('serving', target, refs, measured))

    if path is None:
        found.append(check('path', REFUSE, f'the ledger line names no path on disk for {host} {handle}'))
        return found

    # path normalisation (§4.3, T5.2's completion condition).
    if not path.startswith('/') or '..' in path.split('/'):
        found.append(check('path', REFUSE, f'{path} is not an absolute path without a .. component'))
    elif stage == 'purge' and not inside_any(path, staging):
        found.append(check('stage', REFUSE, f'{path} is not under a staging directory infra/retention.yml names '
                                            f'for {host} ({", ".join(staging) or "none"}): --purge deletes an '
                                            'isolated thing, never a live one (§5.5)'))
    elif stage != 'purge' and not (inside_any(path, roots) or inside_any(path, staging)):
        found.append(check('path', REFUSE, f'{path} is neither a root infra/inventory.yml declares for {host} '
                                           f'({", ".join(roots) or "none"}) nor under its staging directory'))
    else:
        found.append(check('path', OK, path))

    if not ours:
        found.append(check('area', REFUSE, f'infra/retention.yml {host} names no `class: persistent` area, so '
                                           'nothing here is known to be ours (§3.4)'))
    elif inside_any(path, theirs):
        found.append(check('area', REFUSE, f'{path} sits inside a `user-owned` or `unknown` path of {host}: '
                                           'out of scope (§3.4)'))
    elif not inside_any(path, ours):
        found.append(check('area', REFUSE, f'{path} is not inside a path infra/retention.yml calls ours on {host} '
                                           '(§3.4)'))
    else:
        found.append(check('area', OK, f'inside {next(p for p in ours if under(path, p))}'))

    if target['class'] in ('user-owned', 'unknown'):
        found.append(check('class', REFUSE, f'the ledger calls {handle} {target["class"]}: out of scope (§3.4)'))
    else:
        found.append(check('class', OK, target['class']))

    if stage in ('quarantine', 'thin') and not staging:
        found.append(check('staging', REFUSE, f'infra/retention.yml {host} staging: names no directory to '
                                              'isolate into (§5.5)'))
    elif stage in ('quarantine', 'thin'):
        found.append(check('staging', OK, f'{path} -> {staging[0]}'))

    if measured is None:
        found.append(check('host', UNKNOWN, f'{host} did not answer: nothing about {path} was read'))
        return found

    if measured.get('symlink'):
        found.append(check('symlink', REFUSE, f'{path} is a symlink ({measured["resolved"]}): a retirement '
                                              'follows no link (§4.3)'))
    elif measured.get('resolved') not in (None, path):
        found.append(check('symlink', REFUSE, f'{path} resolves to {measured["resolved"]}: the name on the '
                                              'ledger is not the name on the disk'))
    else:
        found.append(check('symlink', OK, 'not a symlink'))

    if measured.get('mount_point'):
        found.append(check('mount', REFUSE, f'{path} is itself a mount point: deleting it would delete whatever '
                                            'is mounted there, which this checkout does not own'))
    elif stage in ('quarantine', 'thin') and measured.get('staging_device') not in (None, measured.get('device')):
        found.append(check('mount', REFUSE, f'{path} (device {measured["device"]}) and the staging directory '
                                            f'{measured["staging_path"]} (device {measured["staging_device"]}) '
                                            'are not on one filesystem: an isolation has to be reversible (§5.5)'))
    elif stage == 'restore' and measured.get('staging_device') not in (None, measured.get('device')):
        found.append(check('mount', REFUSE, f'the staging directory {measured["staging_path"]} (device '
                                            f'{measured["staging_device"]}) is not on {path}\'s filesystem '
                                            f'(device {measured["device"]}): the copy a thin left there is not '
                                            'one this command can copy back'))
    else:
        found.append(check('mount', OK, f'{measured["filesystem"]} (device {measured.get("device")})'))

    if not measured.get('exists'):
        found.append(check('present', REFUSE, f'{path} is not there on {host}: the ledger is what needs '
                                              'correcting, not the disk'))
    else:
        found.append(check('present', OK, f'{gib(measured["size_bytes"])} GiB, '
                                          f'{measured["entries"]} entries'))

    openers = measured.get('openers') or {}
    units = measured.get('units') or openers.get('units') or []
    if openers.get('unknown'):
        found.append(check('in-use', UNKNOWN, openers['unknown']))
    elif openers.get('processes') or units:
        found.append(check('in-use', REFUSE, f'{openers.get("processes", 0)} processes and {len(units)} unit '
                                             f'files name {path} on {host} ({", ".join(units) or "none named"}): '
                                             'release the claim or stop the unit first'))
    elif measured.get('exists'):
        found.append(check('in-use', OK, f'no process and no unit names it (read: {openers.get("method", "proc")}); '
                                         'nothing of ours is loaded from it'))

    # The 24 h is read by the rehearsal too. `--check` on a `quarantine` line asks about the same state `--purge`
    # acts on, and reading only the disk gates made it end "gates pass; what follows is: … --purge" while the
    # record still refused the deletion when the wait was the only refusal.
    if stage == 'purge' or (stage == 'check' and target['state'] == 'quarantine'):
        found.append(purge_gate(record, target))
    if stage == 'quarantine':
        found.append(check('candidate', OK, 'retirement: candidate') if target['retirement'] == 'candidate' else
                         check('candidate', REFUSE, f'infra/retention.yml {host} {target["state"]} {handle} '
                                                    'carries no `retirement: candidate`: --quarantine acts on a '
                                                    'declared candidate only (§4.2, §4.3)'))
        if measured.get('shared_files'):
            # Not a refusal: F8 is the reason the reported reclaim is smaller than `du`.
            found.append(check('shared-inodes', OK, f'{measured["shared_files"]} files share an inode with '
                                                    f'something outside ({gib(measured["shared_bytes"])} GiB), '
                                                    'excluded from the reclaim (F8)'))
    if stage in ('thin', 'restore'):
        found += thinning(stage, target, measured, evidence_present)
    return found


def purge_gate(record, target):
    """§3.1 and §5.5: isolated, watched for 24 hours, then deleted with the reason written down."""
    if record is None:
        return check('record', REFUSE, f'no quarantine record for {target["host"]} {target["handle"]} under '
                                       'artifacts/retired/: --quarantine writes it, and --purge deletes what it '
                                       'recorded')
    if record.get('to') != target['path']:
        return check('record', REFUSE, f'the quarantine record says {record.get("to")}, the ledger says '
                                       f'{target["path"]}: one path, named once (F11)')
    if record.get('purged_at'):
        return check('record', REFUSE, f'{target["path"]} was purged at {record["purged_at"]}: the ledger line '
                                       'is what is left to retire, in a commit')
    waited = hours_since(record.get('quarantined_at'))
    if record.get('wait_shortened'):
        return check('wait', OK, f'the 24 h wait was shortened by a person at {record["purge_approved_at"]}: '
                                 f'{record["wait_shortened"]}')
    if waited is None:
        return check('wait', UNKNOWN, f'the quarantine record has no readable time ({record.get("quarantined_at")!r})')
    if waited < QUARANTINE_HOURS:
        return check('wait', REFUSE, f'{target["path"]} has been isolated {waited:.1f} h of the {QUARANTINE_HOURS} '
                                     'h §3.1 asks for. Re-run with --shorten-wait REASON to record the decision '
                                     'instead of skipping it')
    return check('wait', OK, f'isolated {waited:.1f} h, at least {QUARANTINE_HOURS} h')


def thinning(stage, target, measured, evidence_present):
    """§4.1 and §4.3: a thin N-1 holds everything but a weight another root on this host holds identically, and
    the host keeps the spare to put it back."""
    found = []
    want = 'full' if stage == 'thin' else 'thin'
    other = 'thin' if stage == 'thin' else 'full'
    if target['form'] != want:
        found.append(check('form', REFUSE, f'infra/retention.yml {target["host"]} rollback {target["handle"]} '
                                           f'is `form: {target["form"]}`; --{stage} acts on a `form: {want}` one'))
    else:
        found.append(check('form', OK, want))
    if stage == 'restore':
        found.append(check('confirmed', OK, target['confirmed'] or 'a thin N-1 is restorable without a new judgement'))
        return found
    if not target['confirmed']:
        found.append(check('confirmed', REFUSE, '§3.2 has not been recorded for this rollback: the ledger line '
                                                 'carries no `confirmed` date, so nothing here judges it met'))
    elif not target['evidence']:
        found.append(check('confirmed', REFUSE, 'the `confirmed` line names no `evidence` file (§4.2: the '
                                                 'judgement is evidence, and evidence is runtime state)'))
    elif not evidence_present:
        found.append(check('confirmed', REFUSE, f'{target["evidence"]} is not under artifacts/lifecycle/ on '
                                                 'control: the judgement has to be readable to be counted'))
    else:
        found.append(check('confirmed', OK, f'{target["confirmed"]}, {target["evidence"]}'))

    weights = measured.get('weights') or {}
    if measured.get('weights_error'):
        # The host refused the plan itself. That is a refusal of this stage, not a missing measurement.
        found.append(check('hashes', REFUSE, f'the host refused its own --{stage} plan: {measured["weights_error"]}'))
        return found
    if not weights:
        if any(row['status'] == REFUSE for row in found):
            # Nothing further is asked of the host once a gate has refused: a weight plan reads every pinned
            # weight on both sides, which is the slow part of a thin, and an already-refused stage does not pay
            # for one. The refusal above is the reason, so it is named here rather than a failed read.
            found.append(check('hashes', UNKNOWN, 'the host was not asked for its weight plan: this stage is '
                                                  'already refused above, and a plan hashes every pinned weight '
                                                  'on both sides'))
        else:
            found.append(check('hashes', UNKNOWN, 'no prepare state or manifest weight list was readable in '
                                                  f'{target["path"]}'))
        return found
    identical = [name for name, entry in weights.items() if entry['state'] == 'identical']
    if sorted(identical) == sorted(weights):
        shared = sum(entry['size_bytes'] for entry in weights.values())
        apart = sorted({entry.get('apart_path') for entry in weights.values() if entry.get('apart_path')})
        found.append(check('hashes', OK, f'{len(weights)} weight files, {gib(shared)} GiB, identical to the '
                                         f'manifest pin here and to a copy apart'
                                         f'{" (" + ", ".join(apart) + ")" if apart else ""} (§3.2, ADR 0008)'))
    else:
        found.append(check('hashes', REFUSE, '; '.join(f'{name}: {entry["state"]}'
                                                       for name, entry in sorted(weights.items())
                                                       if entry['state'] != 'identical')
                           + ' — a thin removes only a weight another root here holds identically (§3.2, '
                             'ADR 0008)'))

    bad = [name for name, entry in weights.items() if entry.get('nlink', 1) != 1]
    if bad:
        found.append(check('inode', REFUSE, f'{", ".join(sorted(bad))} is not on its own inode: ADR 0008 '
                                            'forbids a weight shared across generations, and thinning one '
                                            'would move what the other root is running'))
    else:
        found.append(check('inode', OK, 'every pinned weight is on its own inode (nlink 1), so a thin moves a '
                                        'copy and not another root\'s file'))

    spare = SPARE_WEIGHTS_GIB + FLOOR_GIB
    after = gib(measured['free_bytes'] + sum(entry['size_bytes'] for entry in weights.values()))
    found.append(check('spare', OK, f'{after} GiB free after thinning, the rollback spare needs {spare} GiB')
                   if after >= spare else
                   check('spare', REFUSE, f'{after} GiB free after thinning, but a host that can be rolled back '
                                          f'needs {spare} GiB spare (ADR 0008: the shared weights plus the '
                                          f'{FLOOR_GIB} GiB floor) before it can be thinned'))
    return found


def hours_since(stamp, now=None):
    """Hours between an ISO timestamp and now; None when the stamp is not a timestamp."""
    try:
        then = datetime.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.timezone.utc)
    return (now - then).total_seconds() / 3600


def refusals(found):
    return [entry for entry in found if entry['status'] == REFUSE]


def unknowns(found):
    return [entry for entry in found if entry['status'] == UNKNOWN]


def reclaim_bytes(target, measured):
    """What deleting this path actually frees: `du` minus the bytes shared by inode with something outside
    (F8 — the RTX 4080 host's Q2_0 shard 2 was 26.8 GiB of the 71 GiB `du` reported for Q2_0 alone)."""
    if measured is None or not measured.get('exists'):
        return 0
    return max(measured['size_bytes'] - measured.get('shared_bytes', 0), 0)
