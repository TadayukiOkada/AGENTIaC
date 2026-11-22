#!/usr/bin/env python3
"""`agentiac ops upgrade plan|reuse`: read an engine upgrade before a single file is declared (T5.8, S §9 F12).

`src/agentiac/upgrade.py` holds the rules; this is the command side. It writes nothing, changes nothing and
contacts no host: every fact comes from `backends/`, `infra/inventory.yml`, `infra/components.yml`,
`infra/retention.yml` and `gateway/local-coder.json`, so an operator can see which declarations have to move
together and whether the host already holds the pinned weights before downloading tens of GiB again.

  ops upgrade plan HOST --backend ID   the ordered reading: what is declared and what is not, the blockers, the
                                       notes, and the steps with the exact command for each. A preparation with
                                       blockers is not a slow preparation; it is a wrong one.
  ops upgrade reuse HOST --backend ID  which full root on HOST pins the identical (path, size, sha256) set this
                                       manifest pins, from the retention ledger, and what a preparation without a
                                       reuse source would download. `--reuse-declared` on `ops prepare` takes the
                                       answer when there is exactly one.

Exit 0 when the reading is complete, 1 when a blocker makes the upgrade impossible as declared, 2 when the backend
or host is not declared at all. A `0` never means "prepared": the host side is read by `ops storage`, `ops prepare
--check`, and `plan` prints those commands rather than running them.
"""
import argparse
import json
from pathlib import Path
import sys

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
from agentiac import declared, upgrade  # noqa: E402

BLOCKED, UNDECLARED = 1, 2


def show(plan):
    """The reading as lines a person can act on, blockers first."""
    engine = plan['engine']
    print(f"{plan['backend']} on {plan['host']}: engine {engine['version']} at {engine['revision'][:12]}, "
          f"ggml {engine['ggml_revision'][:12] if engine['ggml_revision'] else 'unknown'}, "
          f"{plan['weights']['files']} weight file(s) totalling "
          f"{round(plan['weights']['bytes'] / upgrade.GIB, 2)} GiB")
    current = plan['current']
    print(f"  serves now     {current['backend'] or 'unknown'} "
          f"(pinned by {current['component'] or 'nothing'}"
          f"{', ' + current['pin']['version'] if current['pin'] else ''}; "
          f"ledger: {current['current_ledger_state'] or 'no line'})")
    print(f"  root declared  {plan['root']['path'] or 'NO'} (key {plan['root']['key'] or 'none'})")
    for candidate in plan['reuse']['candidates']:
        print(f"  weights from   {candidate['root']} — {candidate['backend']} ({candidate['state']} line, full), "
              f"{candidate['files']} file(s), {round(candidate['bytes'] / upgrade.GIB, 2)} GiB, byte for byte")
    if not plan['weights']['files']:
        print('  weights from   this manifest pins no weight file: there is nothing to copy or download')
    elif not plan['reuse']['candidates']:
        print(f"  weights from   nothing on this host: {round(plan['reuse']['download_bytes'] / upgrade.GIB, 2)} "
              f"GiB downloads")
    for reason in plan['reuse']['why_not']:
        print(f"  not reusable   {reason}")
    for note in plan['notes']:
        print(f"  note           {note}")
    for blocker in plan['blockers']:
        print(f"  BLOCKER        {blocker}")
    for entry in plan['steps']:
        mark = 'declared' if entry['state'] == 'done' else entry['state']
        print(f"  {mark:<8}  {entry['step']}: {entry['where']} — {entry['what']}")
    return BLOCKED if plan['blockers'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='action', required=True)
    for name, text in (('plan', 'the ordered reading of an upgrade on HOST'),
                       ('reuse', 'which full root on HOST already pins these bytes')):
        step = sub.add_parser(name, help=text)
        step.add_argument('host', help='the host the upgrade is for')
        step.add_argument('--backend', required=True, metavar='ID', help='backends/ID.json')
        step.add_argument('--json', action='store_true', help='one JSON document instead of the lines')
    args = parser.parse_args()

    graph = declared.load(ROOT)
    if args.host not in graph.nodes:
        raise SystemExit(f'{args.host} is not a host in infra/inventory.yml')
    try:
        manifest = upgrade.manifest_for(graph, args.backend)
    except SystemExit as missing:
        print(missing, file=sys.stderr)
        return UNDECLARED
    if args.action == 'reuse':
        found = upgrade.reuse_candidates(graph, args.host, manifest)
        if args.json:
            print(json.dumps({'host': args.host, 'backend': args.backend, **found}, indent=2))
        elif found['candidates']:
            for entry in found['candidates']:
                print(f"{entry['root']}  {entry['backend']} ({entry['state']}, full, "
                      f"{round(entry['bytes'] / upgrade.GIB, 2)} GiB)")
            print(f"download without a reuse source: {round(found['download_bytes'] / upgrade.GIB, 2)} GiB")
        else:
            print(f'nothing on {args.host} pins these bytes: '
                  f'{round(found["download_bytes"] / upgrade.GIB, 2)} GiB downloads')
        for entry in found['packs']:
            print(f"drafter pack, fetched whatever the weights do: {entry['path']} {entry['drafter']} "
                  f"{entry['size']} bytes {entry['sha256'][:16]}…")
        for reason in found['why_not']:
            print(f'not reusable: {reason}', file=sys.stderr)
        return 0
    reading = upgrade.plan(graph, args.host, args.backend)
    if args.json:
        print(json.dumps(reading, indent=2))
        return BLOCKED if reading['blockers'] else 0
    return show(reading)


if __name__ == '__main__':
    sys.exit(main())
