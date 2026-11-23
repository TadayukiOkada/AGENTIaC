#!/usr/bin/env python3
"""`agentiac ops retire` / `ops thin` / the N-1 restore: the command side of the retention ledger (T5.2).

storage-lifecycle-plan.md §4.3, §5.5, §5.6, §5.7; ADR 0008; execution plan T5.2. `infra/retention.yml` says
what a host holds, `src/agentiac/retention.py` makes that agree with the rest of the declaration, and this
script is the only thing that acts on it — slowly, in the order the plan sets, and never on something the
ledger does not name.

  ops retire HOST --backend ID --check        read-only: every §4.3 gate, the reclaim with the shared inodes
                                              taken out (F8), and what to run next. Writes nothing. The opener
                                              scan is the unprivileged one; the stages that change a disk run the
                                              `sudo -n lsof +D` read as well, because on a shared machine a
                                              person else's process is invisible without it. On a `quarantine`
                                              line it reads the 24 h too: `wait` and `record` are gates of the
                                              state, so a rehearsal refuses rather than naming a `--purge` that
                                              would then refuse.
  ops retire HOST --backend ID --quarantine   save the root's metadata to control, then move the root into the
                                              staging directory the ledger names. Reversible (§5.5): a `mv` back.
  ops retire HOST --name NAME --purge         delete what a quarantine record says is already isolated, after
                                              the 24 hours §3.1 asks for. IRREVERSIBLE (§5.6, P8).
  ops thin HOST --backend ID                  move the pinned weights out of a rollback root, keeping the
                                              engine, packs and MTP (Phase 5). Needs §3.2 recorded (ADR 0008).
  ops restore HOST --backend ID               put them back, verified and on independent inodes. Never a link.

What it refuses is the point, and `src/agentiac/retire.py` lists it one check at a time: the ledger line,
the stage that line allows, what serves, the candidate marker, path normalisation, symlink, mount boundary,
the owner's part of a shared disk, an open process or a unit, the 24 hours, the hash match, the rollback spare.

The order of a retirement is the order §5.3 records, and it is a git order as much as a disk order:

  1. commit: the target, its aliases, the manifest and the inventory key go; the ledger's engine line becomes
     a `quarantine` line naming the staging path (that line needs no manifest and no inventory key, which is
     why it can survive the references and still authorise the deletion).
  2. `--check`, then `--quarantine` (metadata first, then `mv`), then 24 hours of `status` and `doctor`.
  3. commit the quarantine line, then `--purge`.

Every privileged command names one host and one path, written out, never selected in a loop (F11 — the first
§5.6 deletion ran through a loop that paired each host with the other's path and removed nothing). After a
`--purge` the script compares `df` before and after against the estimate it published, and says so when the
difference is more than 5 GiB, as §5.6 requires.

`docker` images are candidates in the ledger too (the runtime tag before the current one). This script does
not delete them: ADR 0008's two stages have no image analogue yet, so the report names the tag, the host and
the `docker rmi` a person runs themselves.

Exit 0 when the gates pass (or the stage ran), 1 when a gate refuses, 3 when a host did not answer — the same
reading `doctor` uses.
"""
import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
sys.path.insert(0, str(CODE / 'scripts'))
import borrow  # noqa: E402
import route  # noqa: E402
from agentiac import declared, retire, retention  # noqa: E402
from agentiac.errors import command as cli_name  # noqa: E402

WORKER = CODE / 'scripts/retire-worker.py'
METADATA = ROOT / 'artifacts/retired'
STAGES = retire.STAGES
DF_DELTA_GIB = 5          # §5.6: a difference larger than this is investigated, not accepted
KEY_PATTERNS = (r'api[_-]?key["\']?\s*[:=]\s*(?!["\']?\s*(?i:true|false|null|none|omit)\b)\S',
                r'BEGIN [A-Z ]*PRIVATE KEY', r'\bsk-[A-Za-z0-9]{8,}',
                r'Bearer [A-Za-z0-9._-]{16,}',
                r'password["\']?\s*[:=]\s*(?!["\']?\s*(?i:true|false|null|none|omit)\b)\S')
# A pattern needs a *value* on the right-hand side, not a name on the left: a Strata claim-state file carries the
# backend's own health payload, which reports `"api_key": true` (the server requires a key, not a key value). A
# name-shaped pattern would refuse every claimed root. The other three patterns already name the shape of a
# secret; these two require one too.
# §5.5: what the archive of a root's metadata does not include — the weights, the source tree, the venv, the
# root's own private state and its copy of the claim key.
METADATA_FIND = '''find . -maxdepth 2 -type f \\( -name 'prepare-*.json' -o -name 'python-freeze.txt' \\
  -o -name 'runtime-*public.json' -o -name 'start-*state.json' -o -path './watch/*.jsonl' \\) \
  -print0 | tar --null -T - -czf %s'''


def remote(*argv):
    """Arguments for a command that runs on the host, each one quoted.

    `ssh` joins its arguments into one string the remote shell parses, so a path with a space in it is a
    different command without this. `borrow.on_host` does the same quoting trick for the local host."""
    return [shlex.quote(str(argument)) for argument in argv]


def free_bytes(node, path):
    done = borrow.on_host(node, remote('df', '-B1', '--output=avail', path), timeout=60)
    lines = [line.strip() for line in (done.stdout or '').splitlines() if line.strip()]
    return int(lines[-1]) if len(lines) > 1 and lines[-1].isdigit() else None


IMAGE_READ = '''tag=__TAG__
grep -lF "$tag" /etc/systemd/system/__PREFIX__-*.service 2>/dev/null | sed 's|^|UNIT\\t|'
for file in "$HOME/agentiac/node.json" "$HOME/agentiac/active-profile.json"; do
  if [ -f "$file" ] && grep -qF "$tag" "$file"; then printf 'CONFIG\\t%s\\n' "$file"; fi
done
docker ps -a --format '{{.Image}} {{.Names}}' 2>/dev/null | grep -F "$tag" | sed 's|^|CONTAINER\\t|'
if docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -qxF "$tag"; then
  size=$(docker images --format '{{.Repository}}:{{.Tag}}\\t{{.Size}}' 2>/dev/null | grep -F "$tag" | cut -f2)
  printf 'PRESENT\\t%s\\t%s\\n' "$tag" "$size"
fi
exit 0
'''


def image_reading(facts, tag):
    """One read-only pass on the host for a tag: whether it is there, and what points at it.

    Removing a runtime image tag can leave a disabled inference unit or a `node.json` naming it; this reads unit
    files, the two node configs, every container of any state, and the tag itself before removal.
    """
    done = borrow.on_host(facts, ['bash', '-s'], input=IMAGE_READ.replace('__TAG__', shlex.quote(tag)).replace('__PREFIX__', declared.setting('resource_prefix', ROOT)), timeout=120)
    if done is None or done.returncode == 255:
        return None
    out = {'references': [], 'present': False, 'size': ''}
    for line in (done.stdout or '').splitlines():
        fields = line.split('\t')
        if fields[0] == 'UNIT':
            out['references'].append(f'{fields[1]} runs {"serve-profile.py" if "inference" in fields[1] else "it"}')
        elif fields[0] == 'CONFIG':
            out['references'].append(f'{Path(fields[1]).name} names it')
        elif fields[0] == 'CONTAINER':
            out['references'].append(f'container {" ".join(fields[1:])} runs it')
        elif fields[0] == 'PRESENT':
            out['present'], out['size'] = True, fields[2] if len(fields) > 2 else ''
    return out


def measured(node, paths, staging=None, privileged=True):
    """Ask `retire-worker.py` on the host: it is fed to `python3 -` on stdin, so a read reaches no file.

    No weight hashing here. A pinned weight is only compared by `run_worker`, which copies the manifest to the
    host first — a control-side path handed to a host read is a file that does not exist there.
    """
    argv = ['python3', '-', 'measure', '--front-unit', borrow.PROXY]
    if privileged:
        argv.append('--privileged')
    for path in paths:
        argv += ['--path', path]
    if staging:
        argv += ['--staging', staging]
    done = borrow.on_host(node, remote(*argv), input=WORKER.read_text(), timeout=900)
    if done is None or done.returncode == 255 or not (done.stdout or '').strip():
        return None
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        return None


def manifest_path(handle):
    path = ROOT / f'backends/{handle}.json'
    return path if path.exists() else None


def serving_root(ledger_host, facts, graph, host):
    """The root this host serves from, resolved the way a claim resolves it."""
    for entry in ledger_host['serving']:
        if entry.get('path'):
            return entry['path']
    backend = facts.get('strata_default_backend') or facts.get('strata_backend')
    if backend and backend in graph.backends:
        return retention.inventory_root(facts, backend, graph.backends[backend], host)
    return None


def manifest_images(tag):
    """Which backend manifests name this tag (§3.3: a TensorFold generation *is* an image, so its manifest is a
    declaration of it). A manifest names an image by repository, so the comparison is a prefix on the repository."""
    found = []
    for path in sorted((ROOT / 'backends').glob('*.json')):
        try:
            manifest = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):        # a manifest may be a list of files (a set), not one object
            continue
        image_ = manifest.get('image')
        image = ((image_.get('tag') if isinstance(image_, dict) else image_) or '') if image_ else ''
        args_ = manifest.get('build_args')
        base = str((args_.get('BASE_IMAGE') if isinstance(args_, dict) else '') or '').split('@')[0]
        if image and tag.startswith(image):
            found.append(f'backends/{path.name} image {image}')
        elif base and tag.startswith(base):
            found.append(f'backends/{path.name} BASE_IMAGE {base}')
    return found


def plan(host, handle, stage, graph):
    """Everything the gates need, read from the declaration and from one round of measuring on the host."""
    facts = graph.nodes.get(host)
    ledger_host = graph.retention.get(host)
    out = {'host': host, 'handle': handle, 'stage': stage, 'reachable': facts is not None, 'facts': facts}
    if facts is None:
        out['gates'] = [retire.check('ledger', retire.REFUSE, f'{host} is not a node in infra/inventory.yml')]
        return out
    if ledger_host is None:
        out['gates'] = [retire.check('ledger', retire.REFUSE,
                                     f'infra/retention.yml says nothing about {host}: it holds no line at all')]
        return out

    state, entry = retire.resolve(ledger_host, handle)
    target = retire.target(ledger_host, state, entry, host) if entry else \
        {'host': host, 'state': None, 'handle': handle, 'backend': None, 'path': None, 'image': None,
         'class': None, 'form': None, 'retirement': None, 'confirmed': None, 'evidence': None, 'reason': ''}
    out['target'] = target
    if entry is None:
        named = [retention.handle(line) for state in retention.STATES + retention.AREAS
                 for line in ledger_host[state] if isinstance(line, dict)]
        out['gates'] = [retire.check('ledger', retire.REFUSE,
                                     f'infra/retention.yml names no line for {handle} on {host}. The lines there '
                                     f'are: {", ".join(named) or "none"}')]
        return out
    out['roots'] = retention.host_roots(facts, host, list(graph.backends.values()))
    out['staging'] = [line['path'] for line in ledger_host['staging'] if 'path' in line]
    out['serving_root'] = serving_root(ledger_host, facts, graph, host)

    if target['image']:
        out['image_reading'] = image_reading(facts, target['image'])
        out['gates'] = retire.image_gates(stage, target, graph.nodes, out['image_reading'],
                                          declared_in=manifest_images(target['image']), graph_vars=graph.vars)
        return out

    want = target['backend'] or handle
    refs = retire.references(graph.nodes, graph.route, route.current(), host, want)
    out['ledger'], out['references'] = ledger_host, refs
    paths = [target['path']] if target['path'] else []
    staging = out['staging'][0] if out['staging'] else None
    manifest = manifest_path(target['backend'] or handle)
    if stage in ('thin', 'restore') and manifest is None:
        out['gates'] = [retire.check('ledger', retire.REFUSE,
                                     f'{handle} has no backends/{handle}.json to pin its weights against, so '
                                     f'--{stage} cannot say what a complete root holds')]
        return out
    reading = measured(facts, paths, staging) if paths else None
    out['manifest'] = str(manifest) if manifest else None
    out['measured'] = (reading or {}).get(target['path']) if target['path'] else None
    out['weights'] = (out['measured'] or {}).get('weights') or {}
    out['record'] = record_for(host, target['path']) if target['path'] else None
    out['evidence'] = bool(target['evidence'] and (ROOT / target['evidence']).exists())
    out['reclaim_bytes'] = retire.reclaim_bytes(target, out['measured'])
    out['gates'] = retire.gates(stage, target, ledger_host, refs, out['measured'], out['record'],
                                out['evidence'], out['roots'])
    if not retire.refusals(out['gates']):
        out['rehearsal'] = rehearsal(out)
        if out['rehearsal'] and out['measured'] is not None:
            # The gate for a thin or a restore reads the per-weight states, so they are folded in and the gate is
            # asked again: a §3.2 claim in the ledger is checked against the bytes, not taken on trust.
            out['measured']['weights'] = out['rehearsal'].get('weights') or {}
            if out['rehearsal'].get('error'):
                out['measured']['weights_error'] = out['rehearsal']['error']
            out['gates'] = retire.gates(stage, target, ledger_host, refs, out['measured'], out['record'],
                                        out['evidence'], out['roots'])
    return out


def following(found):
    """The stage --check points at for a target whose gates pass, which is also the one worth rehearsing.

    A rollback recorded as §3.2 met points at `thin`; one not recorded points at `quarantine`, because a thin is
    not a way to avoid the judgement (§3.2). The evidence file itself is the `confirmed` gate's to check."""
    target = found['target']
    if target['state'] == 'quarantine':
        return 'purge'
    if target['state'] == 'rollback' and target.get('form') == 'full':
        return 'thin' if target.get('confirmed') else 'quarantine'
    if target['state'] == 'rollback':
        return 'restore'
    return 'quarantine'


def rehearsal(found):
    """Ask the host for the plan of the stage --check points at, or the stage being asked to run, and change nothing.

    The ledger says a root is thin-eligible; only the host says whether every pinned weight is the pinned one
    (§3.2's "hash matches", per file). `retire-worker.py` hashes both roots for that, which is the slow part of a
    thin, a restore and a --check, and it is what makes the §3.2 line in the ledger checkable rather than a
    sentence. The manifest is copied to the host beside the staging directory and removed again.
    """
    stage = found['stage'] if found['stage'] in ('thin', 'restore') else following(found)
    if stage not in ('thin', 'restore') or not found['manifest'] or not found['measured']:
        return None
    staging = found['staging'][0] if found['staging'] else None
    if not staging or found['measured'].get('staging_device') is None:
        # The rehearsal needs somewhere to put the manifest beside the root, and creating that directory is
        # --quarantine's or --thin's work, not a read's.
        return {'stage': stage, 'skipped': f'{staging or "no staging line"} is not on {found["host"]} yet: '
                                           f'--{stage} creates it, and a read does not'}
    outcome, refusal = run_worker(found, stage, 3600, check=True)
    if refusal:
        return {'stage': stage, 'error': refusal}
    own = stage == found['stage']
    merged = dict(found['measured'], weights=outcome.get('weights') or {})
    return {'stage': stage, 'bytes': outcome.get('bytes', 0), 'staging': outcome.get('staging'),
            'weights': outcome.get('weights') or {},
            'gates': [] if own else retire.gates(stage, found['target'], found['ledger'], found['references'],
                                                 merged, found['record'], found['evidence'], found['roots'])}


def lines(found):
    """One screen: what is being acted on, the numbers, then one line per gate."""
    target, out = found['target'], found
    head = f'{out["host"]:<8} {out["handle"]}  {target["state"] or "nothing declared"}  --{out["stage"]}'
    rows = [head + (f'  {target["path"]}' if target.get('path') else '')]
    if target.get('reason'):
        rows.append(f'         ledger reason: {target["reason"]}')
    measured_ = out.get('measured')
    if measured_ and measured_.get('exists'):
        rows.append(f'         {retire.gib(measured_["size_bytes"])} GiB on {measured_["filesystem"]}, '
                    f'{measured_["entries"]} entries, {retire.gib(measured_.get("shared_bytes", 0))} GiB shared '
                    f'by inode; reclaim {retire.gib(out["reclaim_bytes"])} GiB')
    if out.get('serving_root'):
        rows.append(f'         serving root: {out["serving_root"]}')
    for gate in out['gates']:
        rows.append(f'         {gate["status"]:<7} {gate["id"]:<14} {gate["evidence"]}')
    rows += rehearsal_lines(found)
    rows.append('         ' + next_step(found))
    return rows


def rehearsal_lines(found):
    """What the host said about the stage --check points at, when there is one to ask about."""
    found_ = found.get('rehearsal')
    if not found_:
        return []
    if found_.get('skipped'):
        return [f'         note    rehearsal      {found_["skipped"]}']
    if found_.get('error'):
        return [f'         refuse  rehearsal      --{found_["stage"]} on the host: {found_["error"]}']
    rows = [f'         {"--check rehearses" if found_["stage"] != found["stage"] else "the host\'s plan for"} '
            f'--{found_["stage"]}: {len(found_["weights"])} pinned weights, {retire.gib(found_["bytes"])} GiB, '
            f'the copy apart being {found_["staging"]}']
    for gate in found_['gates']:
        rows.append(f'         {gate["status"]:<7} {gate["id"]:<14} {gate["evidence"]}')
    for name, entry in sorted(found_['weights'].items()):
        rows.append(f'           {entry["state"]:<24} {retire.gib(entry["size_bytes"]):>7} GiB  {name}')
    return rows


def next_step(found):
    refusals, unknowns = retire.refusals(found['gates']), retire.unknowns(found['gates'])
    rehearsal_ = found.get('rehearsal') or {}
    if rehearsal_.get('error'):
        refusals = refusals + [retire.check('rehearsal', retire.REFUSE, rehearsal_['error'])]
    elif rehearsal_.get('gates'):
        refusals = refusals + retire.refusals(rehearsal_['gates'])
    if refusals:
        named = ', '.join(row['id'] for row in refusals)
        return f'refused: {named} ({len(refusals)} refusal{"s" if len(refusals) > 1 else ""})'
    if unknowns:
        return f'not decided: {unknowns[0]["id"]} — {unknowns[0]["evidence"]}'
    host, handle, target = found['host'], found['handle'], found['target']
    if target.get('image'):
        return ('gates pass; the removal is one command that this script does not run: '
                f'{borrow.on_host_text(found["facts"], f"docker rmi {handle}")} — then the ledger line moves to '
                '`retired` in the same commit (§5.6)')
    named = f'--backend {handle}' if target.get('backend') else f'--name {handle}'
    if found['stage'] != 'check':
        return f'gates pass; running --{found["stage"]} now, then the ledger line it names is what a commit updates'
    following_ = following(found)
    if following_ == 'purge':
        return f'gates pass; what follows is: {cli_name()} ops retire {host} {named} --purge'
    if following_ == 'quarantine':
        return (f'gates pass; what follows is: {cli_name()} ops retire {host} {named} --quarantine '
                '(§5.5: the metadata first, then the mv)')
    return (f'gates pass; the rehearsal above is --{following_}, and running it is: '
            f'{cli_name()} ops {following_} {host} {named}')


def exit_code(found):
    rehearsal_ = found.get('rehearsal') or {}
    if rehearsal_.get('error') or retire.refusals(found['gates']) or retire.refusals(rehearsal_.get('gates') or []):
        return 1
    if retire.unknowns(found['gates']) or retire.unknowns(rehearsal_.get('gates') or []):
        return 3
    return 0


# --- the record of a retirement, under control's artifacts/retired/ -------------------------------------

def record_dir(host):
    return METADATA / host


def record_for(host, path):
    """The record whose `to` (or `from`, if the move never ran) is PATH. One record per retirement."""
    if not path:
        return None
    for file in sorted(record_dir(host).glob('*.json')):
        try:
            record = json.loads(file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if record.get('to') == path or record.get('from') == path:
            return record
    return None


def write_record(record):
    directory = record_dir(record['host'])
    directory.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r'[^a-z0-9._-]', '-', Path(record['to']).name)
    path = directory / f'{stamp}.json'
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
    path.chmod(0o600)
    return path


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# --- metadata: archive it, scan it, then move the root (§5.5) ------------------------------------------

def archive_metadata(node, host, path, staging):
    """`tar` the root's preparation records beside it in the staging directory, bring them to control, scan them.

    Returns (control path, credential hits). It never archives the weights, the source tree, the venv, `home/`
    or the root's copy of the claim key — §5.5 — and the scan is the safety net for that claim, not a
    substitute for it. A hit is reported by member name; no value is ever printed.
    """
    name = Path(path).name
    on_host = f'{staging.rstrip("/")}/{name}.metadata.tgz'
    script = f'cd {shlex.quote(path)} && ' + (METADATA_FIND % shlex.quote(on_host))
    done = borrow.on_host(node, remote('bash', '-s'), input=script, timeout=600)
    if done.returncode:
        return None, [f'the metadata archive failed on {host}: {done.stderr.strip()[:200]}']
    record_dir(host).mkdir(parents=True, exist_ok=True)
    local = record_dir(host) / f'{name}.tgz'
    if borrow.get(node, on_host, str(local)) or not local.exists():
        return None, [f'the metadata archive did not come back from {host} to control ({on_host} -> {local})']
    local.chmod(0o600)
    borrow.on_host(node, remote('rm', '-f', on_host), timeout=60)
    return local, scan_credentials(local)


def scan_credentials(archive):
    hits = []
    with tarfile.open(archive, 'r:gz') as tar:
        for member in tar.getmembers():
            if not member.isfile() or member.size > 2 * 1024**2:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            text = handle.read().decode('utf-8', 'replace')
            if any(re.search(pattern, text) for pattern in KEY_PATTERNS):
                hits.append(member.name)
    return hits


def ensure_staging(node, staging, user=None):
    """§5.5's `install -d`: the staging directory exists, belongs to the login user, and is 700.

    The login user is the service account NODE declares (D8.2) unless USER names another.

    On the RTX 4080 host the project root belongs to root, so this needs the same privilege the `mv` does."""
    user = user or declared.account(node, ROOT)
    done = borrow.on_host(node, remote('sudo', '-n', 'install', '-d', '-o', user, '-g', user, '-m', '700',
                                       staging), timeout=120)
    return done.returncode == 0, done.stderr.strip()[:200]


# --- the stages that change something -------------------------------------------------------------------

def quarantine(found):
    host, target, node = found['host'], found['target'], found['facts']
    staging = found['staging'][0]
    source, destination = target['path'], f'{staging.rstrip("/")}/{Path(target["path"]).name}'
    ok, error = ensure_staging(node, staging)
    if not ok:
        print(f'         refused: {staging} could not be prepared on {host}: {error}')
        return 1
    archive, hits = archive_metadata(node, host, source, staging)
    if archive is None:
        print(f'         refused: {hits[0]}. Nothing was moved.')
        return 1
    if hits:
        archive.unlink(missing_ok=True)          # §5.5: control does not keep what the scan refused
        print(f'         refused: the metadata archive holds credential-shaped text in '
              f'{", ".join(hits[:4])} (§5.5). The archive was removed from control; nothing was moved.')
        return 1
    print(f'         on {host}: sudo -n mv -- {source} {destination}')          # F11: one host, one path
    before = free_bytes(node, source)
    moved = borrow.on_host(node, remote('sudo', '-n', 'mv', '--', source, destination), timeout=600)
    if moved.returncode:
        print(f'         refused: the mv on {host} exited {moved.returncode}: {moved.stderr.strip()[:200]}')
        return 1
    record = {'host': host, 'handle': target['handle'], 'backend': target.get('backend'), 'from': source,
              'to': destination, 'quarantined_at': now(), 'size_bytes': found['reclaim_bytes'],
              'metadata': str(archive), 'filesystem': (found['measured'] or {}).get('filesystem'),
              'free_bytes_before': before}
    saved = write_record(record)
    print(f'         metadata: {archive} (0600, credential scan clean)')
    print(f'         record:   {saved}')
    print(f'         the ledger line to commit now (the disk moved; git has not):')
    print(f'           - name: {target["handle"]}-isolated')
    print(f'             path: {destination}')
    print(f'             class: {target["class"]}')
    print(f'             form: {target.get("form", "full")}')
    print(f'             reason: isolated {record["quarantined_at"]}; the engine line, its target, its manifest')
    print(f'               and its inventory key went in the same commit (§5.3). {QUARANTINE_NOTE}')
    return 0


QUARANTINE_NOTE = (f'After 24 h of `{cli_name()} status` and `{cli_name()} doctor` with nothing wrong, the same '
                   'file\'s quarantine line authorises `--purge`. To put it back: mv it back and commit nothing.')


def purge(found, shorten):
    host, target, node = found['host'], found['target'], found['facts']
    path = target['path']
    print(f'         on {host}: sudo -n rm -rf -- {path}')                       # F11: one host, one path
    before = free_bytes(node, str(Path(path).parent))
    removed = borrow.on_host(node, remote('sudo', '-n', 'rm', '-rf', '--', path), timeout=1800)
    if removed.returncode:
        print(f'         refused: the rm on {host} exited {removed.returncode}: {removed.stderr.strip()[:200]}')
        return 1
    after = free_bytes(node, str(Path(path).parent))
    record = found['record'] or {'host': host, 'handle': target['handle'], 'from': None, 'to': path}
    if shorten:
        record['wait_shortened'] = shorten
        record['purge_approved_at'] = now()
    record['purged_at'] = now()
    record['free_bytes_before'], record['free_bytes_after'] = before, after
    saved = write_record(record)
    print(f'         record: {saved}')
    if before is not None and after is not None:
        delta = retire.gib(after - before)
        expected = retire.gib(found['reclaim_bytes'])
        note = ('as expected' if abs(delta - expected) <= DF_DELTA_GIB else
                f'off by {retire.gib(abs(delta - expected))} GiB against the {expected} GiB estimate — '
                'investigate before moving on (§5.6), the usual cause is a shared inode (F8)')
        print(f'         df: {retire.gib(before)} -> {retire.gib(after)} GiB free (+{delta} GiB), {note}')
    print(f'         the ledger line is now a `retired` line with this reason, and `infra/inventory.yml` must '
          f'not name {path} any more')
    return 0


def staged_manifest(found):
    """Where a manifest is put on the host for one run: in the staging directory, never inside a root."""
    staging = found['staging'][0]
    return f'{staging.rstrip("/")}/{Path(found["target"]["path"]).name}.manifest.json'


def run_worker(found, action, timeout, check=False):
    """Run `retire-worker.py` on the host for one root, with the pinned manifest beside the staging directory.

    Returns (outcome, refusal). The manifest is copied out and removed again: a thin or a restore writes weights
    into its root and nothing else there. `--roots` is every root the inventory declares for this host, because
    §3.2's copy apart is any of the others and a restore looks for one there too. `check` asks the worker for its
    plan and changes nothing."""
    target, node = found['target'], found['facts']
    staged = staged_manifest(found)
    if borrow.put(node, found['manifest'], staged):
        return None, f"the manifest would not copy to {found['host']}"
    argv = ['python3', '-', action, '--root', target['path'], '--staging', found['staging'][0],
            '--manifest', staged]
    for place in found['roots']:
        argv += ['--roots', place]
    if check:
        argv.append('--check')
    done = borrow.on_host(node, remote(*argv), input=WORKER.read_text(), timeout=timeout)
    borrow.on_host(node, remote('rm', '-f', staged), timeout=60)
    if done.returncode:
        return None, (done.stderr or done.stdout or '').strip()[:400]
    return json.loads(done.stdout or '{}'), None


def thin(found, dry=False):
    host, target = found['host'], found['target']
    ok, error = ensure_staging(found['facts'], found['staging'][0])
    if not ok and not dry:
        print(f'         refused: {found["staging"][0]} could not be prepared on {host}: {error}')
        return 1
    if dry:
        return print_plan(found)
    outcome, refusal = run_worker(found, 'thin', 3600)
    if refusal:
        print(f'         refused: {refusal}')
        return 1
    moved = outcome.get('moved', [])
    print(f'         {len(moved)} weight files moved out of {target["path"]} into {outcome.get("staging")}:')
    for entry in moved:
        print(f'           {retire.gib(entry["size_bytes"]):>7} GiB  {entry["to"]}')
    print(f'         {target["path"]} is thin now: the engine, the packs and the MTP layer are still there.')
    print(f'         the ledger line to commit: `form: thin` on {host} rollback {target["handle"]}, keeping the')
    print(f'         `confirmed` date and the `evidence` path that let this run (§4.2). Then 24 h of watching,')
    print(f'         then the weights themselves go through --quarantine and --purge (Phase 5).')
    return 0


def restore(found, dry=False):
    host, target = found['host'], found['target']
    if dry:
        return print_plan(found)
    outcome, refusal = run_worker(found, 'restore', 7200)
    if refusal:
        print(f'         refused: {refusal}')
        return 1
    if dry:
        states = outcome.get('weights', {})
        print(f'         --dry-run: {retire.gib(outcome.get("bytes", 0))} GiB would be copied back into '
              f'{target["path"]}:')
        for name, entry in sorted(states.items()):
            print(f'           {entry["state"]:<22} {retire.gib(entry["size_bytes"]):>7} GiB  {entry["path"]}')
        return 0
    put = outcome.get('restored', [])
    print(f'         {len(put)} weight files copied back into {target["path"]}, verified and on their own inodes:')
    for entry in put:
        print(f'           {retire.gib(entry["size_bytes"]):>7} GiB  from {entry["from"]}')
    print(f'         the ledger line to commit: `form: full` on {host} rollback {target["handle"]}. Then start it')
    print(f'         once to prove it: `{cli_name()} claim {host} --backend {target["handle"]} --context N`, then')
    print(f'         `{cli_name()} release {host}` (F5 is the reason a rollback is read aloud, not assumed).')
    return 0


def print_plan(found):
    """What --dry-run prints: the plan the host already gave the gate, printed rather than asked for twice."""
    plan_ = found.get('rehearsal') or {}
    if plan_.get('error'):
        print(f'         refused: {plan_["error"]}')
        return 1
    if plan_.get('skipped'):
        print(f'         note: {plan_["skipped"]}')
        return 1
    states = plan_.get('weights', {})
    apart = plan_.get('apart') or []
    places = f'{len(apart)} other root' + ('s' if len(apart) != 1 else '')
    print(f'         --dry-run: {len(states)} pinned weights, measured here and against {places} on {host} '
          f'({", ".join(apart) or "none declared"}); {retire.gib(plan_.get("bytes", 0))} GiB would move to '
          f'{plan_.get("staging")}:')
    for name, entry in sorted(states.items()):
        print(f'           {entry["state"]:<24} {retire.gib(entry["size_bytes"]):>7} GiB  {name}')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('stage', choices=STAGES, help='check (read-only), quarantine, purge, thin or restore')
    parser.add_argument('host', help='the node the path is on')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--backend', help='the ledger line naming a backend manifest')
    group.add_argument('--name', help='the ledger line naming a thing with no manifest (an isolated path)')
    group.add_argument('--image', help='the ledger line naming an image (T5.9): the gate reads what points at the '
                                       'tag and reports the `docker rmi`. This command never runs it')
    parser.add_argument('--json', action='store_true', help='one JSON document instead of the lines')
    parser.add_argument('--dry-run', action='store_true', help='for thin and restore: run every gate, ask the '
                                                                'host for the plan, change nothing')
    parser.add_argument('--shorten-wait', metavar='REASON', help='record that the 24 h isolation was shortened, '
                                                                 'with the reason, and purge (§5.6: the shortening '
                                                                 'is written down, not skipped)')
    args = parser.parse_args(argv)
    handle = args.backend or args.name or args.image
    graph = declared.load()
    found = plan(args.host, handle, args.stage, graph)
    if args.json:
        print(json.dumps({'schema_version': 1, 'generated': now(), **found}, default=str))
    else:
        for line in lines(found):
            print(line)
    if retire.refusals(found['gates']) or retire.unknowns(found['gates']) or args.stage == 'check':
        return exit_code(found)
    if args.dry_run:
        return {'thin': lambda: thin(found, True), 'restore': lambda: restore(found, True)}[args.stage]()
    stages = {'quarantine': lambda: quarantine(found), 'purge': lambda: purge(found, args.shorten_wait),
              'thin': lambda: thin(found), 'restore': lambda: restore(found)}
    return stages[args.stage]()


if __name__ == '__main__':
    sys.exit(main())
