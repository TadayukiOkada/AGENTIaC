#!/usr/bin/env python3
"""Where each node's disk stands: free space, room for one more update, what the checkout does not name, and
what the retention ledger marks goable (PA §11).

Reads only. It measures with `df` and `du` over the same ssh path the other read-only checks use, and it
deletes nothing: detection is automatic, deletion is a decision (storage-lifecycle-plan.md §3.4).

What a node declares comes from `infra/inventory.yml` (its Strata roots, its TensorFold service root) and
`backends/*.json` (the pinned image, and any weight a manifest keeps loose on the disk), so a root, an image
or a checkpoint nobody names any more shows up as unregistered rather than silently occupying the disk.
`agentiac ops storage` prints the report; `agentiac ops drift` folds the same numbers into one line per
node (F7, IP-08).

A Strata root carries its own weights, so measuring the root measures the generation. TensorFold does not: its
checkpoint is stored as loose safetensors outside the container image, so reporting only the image would
understate what the host holds.
"""
import argparse
import concurrent.futures
import datetime
import json
from pathlib import Path
import subprocess
import sys

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
sys.path.insert(0, str(CODE / 'scripts'))
import borrow  # noqa: E402
from agentiac import declared, retention  # noqa: E402
from agentiac.errors import command as cli_name  # noqa: E402

FLOOR_GIB = 40       # what the owner keeps free at all times (strata-128k-plan.md)
DEFAULT_SCRATCH_GIB = 15   # packs, MTP draft layer, build; a manifest raises it per host
SPARE_WEIGHTS_GIB = 85.6   # the weights a thin N-1 gives back and has to be able to put back
GIB = 1024**3
LOOKS_US = ('strata', declared.resource('strata', ROOT))  # a name under a Strata parent that is ours to account for

MEASURE = '''
for p in %(roots)s; do
  [ -d "$p" ] || continue
  avail=$(df -B1 --output=avail "$p" | tail -n 1 | tr -d ' ')
  target=$(df --output=target "$p" | tail -n 1 | tr -d ' ')
  size=$(du -sx --block-size=1 "$p" 2>/dev/null | cut -f1)
  last=$(find "$p" -printf '%%T@\\n' 2>/dev/null | sort -rn | head -n 1)
  printf 'ROOT\\t%%s\\t%%s\\t%%s\\t%%s\\t%%s\\n' "$p" "$target" "$avail" "${size:-0}" "${last:-0}"
done
for d in %(parents)s; do
  [ -d "$d" ] || continue
  ls -1 "$d" 2>/dev/null | while IFS= read -r entry; do printf 'ENTRY\\t%%s\\t%%s\\n' "$d" "$entry"; done
done
%(images)s
%(cache)s
'''


def manifests_for(node):
    """Backend manifests that carry worker settings for this node."""
    found = []
    for path in sorted((ROOT / 'backends').glob('strata-*.json')):
        manifest = json.loads(path.read_text())
        if node in manifest.get('workers', {}):
            found.append(manifest)
    return found


def loose_weights(name, host):
    """Weight directories a manifest pins outside any engine root (PA §11).

    Only a manifest that carries its model as a directory qualifies: the Strata manifests name a model variant
    and keep their weights inside the root, while `backends/tf-1.1.4-qfn-mxfp8-nvfp4.json` pins 32 files under the
    project root and `agentiac-spark-tensorfold.service`'s worker reads them from there. The declaration is
    the measurement's boundary here too - a checkpoint no manifest names stays a leftover to be found, not a
    held generation.
    """
    found = []
    for path in sorted((ROOT / 'backends').glob('*.json')):
        manifest = json.loads(path.read_text())
        model = manifest.get('model') if isinstance(manifest, dict) else None
        if not isinstance(model, dict) or not model.get('directory'):
            continue
        if name not in manifest.get('workers', {}):
            continue
        found.append(host['project_root'].rstrip('/') + '/' + model['directory'].strip('/'))
    return sorted(set(found))


def declared_roots(name, host):
    """The paths this checkout names for a node, and the directories a leftover would sit in."""
    paths, parents = [], set()
    if borrow.serves_strata(host):
        for key in ('strata_root',):
            if key in host:
                paths.append(host[key])
        paths.extend(host.get('strata_trial_roots', {}).values())
        parents |= {str(Path(path).parent) for path in paths}
    if host.get('spark_tensorfold'):
        paths.append(borrow.tensorfold_root(host))
    # A weights parent is deliberately not added to `parents`: that scan decides what looks like ours, and the
    # directory a TensorFold checkpoint sits in is the owner's evaluation tree (§3.4).
    paths.extend(path for path in loose_weights(name, host) if path not in paths)
    return paths, sorted(parents)


def measure(host, paths, parents, wants_images=True):
    """One read-only pass on the host: the declared paths, their parents, our images and the build cache.

    The image reading is asked of every node so an image the checkout has stopped naming is still seen. The
    pattern selects only what is ours (`agentiac*` and the NGC PyTorch base image): unrelated host images are
    not ours to name.
    """
    script = MEASURE % {
        'roots': ' '.join(f'"{path}"' for path in paths) or '"/nonexistent"',
        'parents': ' '.join(f'"{path}"' for path in parents) or '"/nonexistent"',
        'images': ("docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' 2>/dev/null | "
                   "grep -E '^(agentiac|nvcr\\.io/nvidia/pytorch)' | while read -r tag size; do "
                   "printf 'IMAGE\\t%s\\t%s\\n' \"$tag\" \"$size\"; done") if wants_images else '',
        # `docker system df` prints `Build Cache  608  0  83.33GB  60.17GB`: the name is two words, so the size is
        # field 5 and what Docker could reclaim is field 6 (the Images and Containers lines are one word each).
        'cache': ("docker system df 2>/dev/null | awk '/^Build Cache/{printf \"CACHE\\t%s\\t%s\\n\", $5, $6}'")
                 if wants_images else '',
    }
    try:
        done = borrow.on_host(host, ['bash', '-s'], input=script, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done is None or done.returncode == 255:
        return None
    rows = {'roots': {}, 'entries': [], 'images': [], 'cache': None}
    for line in done.stdout.splitlines():
        fields = line.split('\t')
        if fields[0] == 'ROOT':
            try:
                last = float(fields[5])
            except (IndexError, ValueError):
                last = 0.0
            rows['roots'][fields[1]] = {'filesystem': fields[2], 'avail_bytes': int(fields[3]),
                                        'size_bytes': int(fields[4]), 'last_used': last}
        elif fields[0] == 'ENTRY':
            rows['entries'].append((fields[1], fields[2]))
        elif fields[0] == 'IMAGE':
            rows['images'].append({'tag': fields[1], 'size': fields[2]})
        elif fields[0] == 'CACHE':
            rows['cache'] = {'size': fields[1], 'reclaimable': fields[2] if len(fields) > 2 else ''}
    return rows


def gib(bytes_):
    return round(bytes_ / GIB, 1)


UNITS = {'B': 1, 'KB': 10**3, 'MB': 10**6, 'GB': 10**9, 'TB': 10**12,
         'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3, 'TiB': 1024**4}


def human_bytes(text):
    """What `docker images` prints for a size (`24.5GB`), in bytes; 0 when it cannot be read."""
    for suffix, scale in sorted(UNITS.items(), key=lambda item: -len(item[0])):
        if text.endswith(suffix):
            try:
                return round(float(text[:-len(suffix)].strip()) * scale)
            except ValueError:
                return 0
    return 0


RECORDED_IN = ('docs', 'infra', 'backends', 'gateway', 'scripts', 'src',
               # The ledger is where a retirement decision is written down, so it must not be what makes an
               # image look accounted for: `infra/retention.yml` names the old runtime tag *as a candidate*, and
               # a check that reads that file would then never report it.
               ':(exclude)infra/retention.yml')


def tracked_names(tag):
    """Whether the committed checkout records this image tag.

    `git grep` at HEAD rather than the working tree: a tag mentioned only in an uncommitted edit is not
    declared state yet (ADR 0007), and what this check looks for is the gap between the ledger and the
    machines. Only the paths that declare things are searched — a test that mentions a tag does not
    retain it, and an older generation the records do name is not a leftover.
    """
    done = subprocess.run(['git', 'grep', '-l', '-F', '--no-color', tag, 'HEAD', '--', *RECORDED_IN],
                          cwd=ROOT, capture_output=True, text=True, check=False)
    return bool(done.stdout.strip())


def declared_images(name, host, graph):
    """The image tags the declarations name for this node (T5.10).

    What a node inherits (`all vars runtime_image`) or overrides, plus every backend manifest's own image and the
    base image its build names. `infra/retention.yml` is deliberately not among them: a candidate line must not be
    what makes the thing it names look accounted for (the same rule `tracked_names` applies to the records).
    """
    named = []
    runtime = host.get('runtime_image') or graph.vars.get('runtime_image')
    if runtime:
        named.append(str(runtime))
    for spec in graph.backends.values():
        image = spec.get('image')
        tag = (image.get('tag') if isinstance(image, dict) else image) or ''
        if tag:
            named.append(str(tag))
        build = spec.get('build_args')
        base = str((build.get('BASE_IMAGE') if isinstance(build, dict) else '') or '').split('@')[0]
        if base:
            named.append(base)
    for profile in graph.profiles.values():
        if profile.get('image'):
            named.append(str(profile['image']))
    return tuple(named)


def summary(name, host):
    """One node: free space, room for one more full root, and what the checkout does not name."""
    paths, parents = declared_roots(name, host)
    rows = measure(host, paths, parents)
    if rows is None:
        return {'host': name, 'reachable': False}
    if not rows['roots'] and not rows['images'] and not rows['cache']:
        return None                       # nothing declared here, and nothing of ours to look at
    graph = declared.load()
    workers = manifests_for(name)
    scratch = max([worker['workers'][name].get('scratch_gib', DEFAULT_SCRATCH_GIB) for worker in workers]
                  + [DEFAULT_SCRATCH_GIB])
    free = max([row['avail_bytes'] for row in rows['roots'].values()] or [0])
    # What the next update has to fit: on a Strata node that is a second root beside the one serving;
    # on Spark it is a second image beside the one running.
    weights = set(loose_weights(name, host))
    candidates = ([(row['size_bytes'], 'weights' if path in weights else 'root')
                   for path, row in rows['roots'].items()] +
                  [(human_bytes(image['size']), 'image') for image in rows['images']])
    largest, basis = max(candidates, default=(0, 'root'))
    filesystem = next(iter(rows['roots'].values()))['filesystem'] if rows['roots'] else '?'
    update_needs = gib(largest + (FLOOR_GIB + scratch) * GIB) if rows['roots'] else None
    spare_needs = gib((SPARE_WEIGHTS_GIB + FLOOR_GIB) * GIB) if workers else None
    unregistered = sorted({entry for parent, entry in rows['entries']
                           if entry.startswith(LOOKS_US) and str(Path(parent) / entry) not in paths})
    serving = json.loads((ROOT / 'gateway/local-coder.json').read_text())['targets']
    route = route_target(name, serving)
    named = declared_images(name, host, graph)
    images = [image for image in rows['images']
              if not image['tag'].startswith(named) and not tracked_names(image['tag'])]
    return {'host': name, 'reachable': True, 'filesystem': filesystem,
            'free_gib': gib(free) if rows['roots'] else None,
            'roots': [{'path': path, 'gib': gib(row['size_bytes']),
                       'kind': 'weights' if path in weights else 'root'}
                      for path, row in sorted(rows['roots'].items())],
            'missing': [path for path in paths if path not in rows['roots']],
            'scratch_gib': scratch, 'update_needs_gib': update_needs, 'basis': basis,
            'spare_needs_gib': spare_needs, 'unregistered': unregistered, 'images': images, 'route': route,
            'cache': rows['cache'], 'declared_images': len(named)}


def route_target(name, targets):
    """The local-coder target on this node, if any (runtime state, but the report should say what serves)."""
    return [target for target, spec in targets.items() if spec['node'] == name]


def references_to(entry, graph, facts):
    """What the declaration still names this candidate by, so a person can see the order §5.3 asks for.

    A candidate with references left is not yet deletable: `ops retire` refuses it for exactly this reason, and
    the value of listing them here is that the same fact is visible while reading the disk report. A *record*
    that mentions a tag (docs, a handoff) is not a reference: `ops retire --check` reads the declarations."""
    found = []
    for target, spec in sorted(graph.route['targets'].items()):
        if spec['node'] == entry['host'] and spec.get('backend') == entry.get('backend'):
            found.append(f'target {target}')
    facts = graph.nodes.get(entry['host']) or {}
    for key, path in sorted((facts.get('strata_trial_roots') or {}).items()):
        if entry.get('path') and path == entry['path']:
            found.append(f'infra/inventory.yml {key}')
    if entry.get('image') and (facts.get('runtime_image') or graph.vars.get('runtime_image')) == entry['image']:
        found.append('infra/inventory.yml runtime_image (what serve-profile.py reads)')
    for stem, spec in sorted(graph.backends.items()):
        if stem == entry.get('backend'):
            found.append(f'backends/{stem}.json')
    return found


def candidates():
    """PA §11: what the ledger marks goable, and what the disk says about each of them. Reads only.

    `retention.candidates()` is the exclusion — what serves is never here (§4.3), and neither is anything
    `user-owned` or `unknown` (§3.4) — so this function adds the measurement, not the judgement. The reclaim
    number here is `du`; the one `ops retire --check` publishes is smaller, because the bytes shared by inode
    with something outside do not come back (F8)."""
    graph = declared.load()
    inventory = borrow.hosts()          # the same inventory with the group vars merged: `runtime_image` is one
    items = retention.candidates(graph.retention)
    by_host = {}
    for item in items:
        by_host.setdefault(item['host'], []).append(item)
    out = []
    for host, entries in sorted(by_host.items()):
        paths = [entry['path'] for entry in entries if entry.get('path')]
        images = bool([entry for entry in entries if entry.get('image')])
        rows = measure(inventory[host], paths, [], images) if (paths or images) else None
        for entry in entries:
            row = (rows or {}).get('roots', {}).get(entry['path']) if entry.get('path') else None
            size = row['size_bytes'] if row else next(
                (human_bytes(image['size']) for image in (rows or {}).get('images', [])
                 if image['tag'] == entry.get('image')), 0)
            out.append(dict(entry, reachable=rows is not None, present=row is not None or bool(size),
                            filesystem=(row or {}).get('filesystem'), free_gib=gib((row or {}).get('avail_bytes', 0)),
                            size_gib=gib(size), last_used=(row or {}).get('last_used'),
                            references=references_to(entry, graph, inventory[host]),
                            next=(f'{cli_name()} ops retire ' + host +
                                  (f' --backend {entry["backend"]}' if entry.get('backend')
                                   else f' --name {entry["handle"]}') + ' --check')
                            if entry.get('path') else f'on {host}: docker rmi {entry["image"]}'))
    return out


def candidate_lines(items):
    """One block per candidate: what it is, what it is worth, what still points at it."""
    rows = []
    for item in items:
        named = item['path'] or item['image'] or item['handle']
        rows.append(f"{item['host']:<8} {item['state']:<10} {item['class']:<12} {named}")
        if not item['reachable']:
            rows.append('         not measured: the host did not answer')
        elif not item['present']:
            rows.append('         not there (an image nothing has, or a path already moved)')
        elif item['path']:
            last = (datetime.datetime.fromtimestamp(item['last_used'], datetime.timezone.utc).isoformat(timespec='seconds')
                    if item.get('last_used') else 'never read')
            rows.append(f"         {item['size_gib']} GiB on {item['filesystem']}, {item['free_gib']} GiB free "
                        f'there, last read {last}')
        else:
            rows.append(f"         {item['size_gib']} GiB of image layers, `du` apart: an image's own share is "
                        'smaller, because the layers are shared (S §2.4)')
        rows.append(f"         reason: {item['reason']}")
        if item['references']:
            rows.append('         still named by: ' + ', '.join(item['references']) + ' (§5.3: those go first)')
        rows.append(f"         next: {item['next']}")
    return rows


def basis_word(basis):
    """How to call the largest thing a node would have to fit beside what runs there."""
    return {'root': 'root', 'image': 'image', 'weights': 'weight set'}.get(basis, basis)


def lines(node):
    """Printable lines for one node."""
    if not node['reachable']:
        return [f"{node['host']:<8} not reachable; not measured"]
    out = [f"{node['host']:<8} {node['filesystem']}  " +
           (f"{node['free_gib']} GiB free" if node['free_gib'] is not None else
            'no declared root; images and build cache only')]
    for root in node['roots']:
        out.append(f"         {root['gib']:>7} GiB  {root['path']}"
                   + ('  (loose weights: not inside an image)' if root['kind'] == 'weights' else ''))
    for path in node['missing']:
        out.append(f"         declared but absent: {path}")
    if node['update_needs_gib'] is not None:
        out.append(f"         one more full {basis_word(node['basis'])} needs about {node['update_needs_gib']} GiB "
                   f"(largest {basis_word(node['basis'])} + {FLOOR_GIB} floor + {node['scratch_gib']} scratch)")
    if node['spare_needs_gib'] is not None:
        out.append(f"         a thin N-1 keeps about {node['spare_needs_gib']} GiB spare "
                   f"(shared weights + floor) for the rollback")
    if node['route']:
        out.append(f"         local-coder targets: {', '.join(node['route'])}")
    if node['unregistered']:
        out.append(f"         unregistered: {', '.join(node['unregistered'])}")
    if node['images']:
        out.append('         images the committed checkout does not name: ' + ', '.join(
            f"{image['tag']} ({image['size']})" for image in node['images']))
    if node.get('cache'):
        out.append(f"         build cache {node['cache']['size']} ({node['cache']['reclaimable']} reclaimable): "
                   'regenerable layers, and this report deletes nothing')
    return out


def notes(node):
    """What drift should say about a node: one line, `note` only when something is worth knowing."""
    if not node['reachable']:
        return 'note', f'storage {node["host"]}', 'host not reachable; disk not measured'
    problems = []
    if (node['free_gib'] is not None and node['update_needs_gib'] is not None and
            node['free_gib'] < node['update_needs_gib']):
        problems.append(f"{node['free_gib']} GiB free, one more {basis_word(node.get('basis', 'root'))} "
                        f"needs {node['update_needs_gib']} GiB")
    if node['unregistered']:
        problems.append('unregistered: ' + ', '.join(node['unregistered']))
    if node['images']:
        problems.append('unnamed image: ' + ', '.join(image['tag'] for image in node['images']))
    if node['missing']:
        problems.append('declared root absent: ' + ', '.join(node['missing']))
    if problems:
        return 'note', f'storage {node["host"]}', '; '.join(problems)
    room = (f"{node['free_gib']} GiB free, room for one more {basis_word(node.get('basis', 'root'))} "
            f"({node['update_needs_gib']} GiB)") if node['update_needs_gib'] is not None else \
           'no declared root; the image and build-cache reading is all there is here'
    if node.get('cache'):
        room += f", build cache {node['cache']['size']} ({node['cache']['reclaimable']} reclaimable)"
    return ('ok', f'storage {node["host"]}', room)


def report(hosts=None):
    """Every node the read reaches; `hosts` is the inventory dict, default: all of it.

    T5.10: the filter used to be "a declared root, or Spark", which is why an unnamed image on control was never
    reported. Every node is asked now, and a node with nothing of ours on it answers nothing and is left out."""
    inventory = borrow.hosts() if hosts is None else hosts
    wanted = dict(inventory)
    nodes = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(wanted) or 1) as pool:
        futures = {name: pool.submit(summary, name, host) for name, host in wanted.items()}
        for name, future in futures.items():
            node = future.result()
            if node is not None:
                nodes.append(node)
    return sorted(nodes, key=lambda node: node['host'])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('hosts', nargs='*', choices=sorted(borrow.hosts()),
                        help='nodes to measure (default: every node, and one with nothing of ours on it is left '
                             'out of the report)')
    parser.add_argument('--json', action='store_true', help='one JSON document instead of the lines')
    parser.add_argument('--candidates', action='store_true',
                        help='what infra/retention.yml marks goable, with its size, its last read and what still '
                             'names it (PA §11). Reads only: nothing here deletes anything')
    args = parser.parse_args()
    if args.candidates:
        items = candidates()
        if args.json:
            print(json.dumps({'schema_version': 1, 'candidates': items}))
        elif not items:
            print('infra/retention.yml marks nothing goable: every held generation is serving, held back as a '
                  'measurement, or out of scope')
        else:
            for line in candidate_lines(items):
                print(line)
        return 3 if any(not item['reachable'] for item in items) else 0
    inventory = borrow.hosts()
    nodes = report({name: inventory[name] for name in args.hosts} if args.hosts else None)
    if args.json:
        print(json.dumps({'schema_version': 1, 'nodes': nodes}))
    else:
        for node in nodes:
            for line in lines(node):
                print(line)
    return 3 if any(not node['reachable'] for node in nodes) else 0


if __name__ == '__main__':
    sys.exit(main())
