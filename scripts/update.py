#!/usr/bin/env python3
"""What upstream offers for infra/components.yml (T9.2). Reads metadata, changes nothing but its own record (ADR 0009).

check    reads each component that is not frozen and keeps the answer in artifacts/components/latest.json. A failed
         read keeps the last answer, marked stale. Exit 0 when anything could be read, 3 when nothing could. It also audits
         the locks into audit.json (T9.7), which only reports. --notify tells the desktop about a new `notify` version.
observe  reads what the machines run, with read-only commands, into artifacts/components/observed.json. It loads no
         model. An unreachable host is `unknown`, not a failure. Exit 0 when anything could be read, 3 when nothing could.
plan ID  prints what updating one component would take, from those two records and PA §8.1. It uses no network and no
         subprocess and writes nothing. Exit 0 with a plan, 2 for a frozen or undeclared component, 3 if the documents
         no longer hold its row.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
sys.path.insert(0, str(CODE / 'scripts'))
from agentiac import adapters, audit, declared, evaluation_watch, updates  # noqa: E402
from agentiac.errors import explain  # noqa: E402
from agentiac.errors import command as cli_name  # noqa: E402

RECORD = ROOT / 'artifacts/components/latest.json'
OBSERVED = ROOT / 'artifacts/components/observed.json'
AUDIT = ROOT / 'artifacts/components/audit.json'
PROBE_TIMEOUT_S = 30


class Probes:
    """The only ways `observe` touches anything. `remote` refuses what updates.REMOTE_READS does not list."""

    def __init__(self):
        import borrow
        self.borrow, self.hosts = borrow, borrow.hosts()

    def remote(self, name, argv):
        if tuple(argv) not in updates.REMOTE_READS:
            raise ValueError(f'{argv[0]} is not a read this command may run on a worker')
        return self.borrow.on_host(self.hosts[name], list(argv), timeout=PROBE_TIMEOUT_S)

    def front(self, name):
        return self.borrow.ondemand_call(name, self.hosts[name], 'GET', 'status', timeout=10)

    def agent(self, name):
        return adapters.adapter(name).version()

    def personal(self, program):
        found = shutil.which(program) or shutil.which(program, path=str(Path.home() / '.local/bin'))
        if not found:
            raise ValueError(f'{program} is not on PATH')
        return subprocess.check_output([found, '--version'], text=True, timeout=PROBE_TIMEOUT_S)


def keep(path, report, code, command):
    try:
        updates.save(path, report)
    except OSError as error:
        print(explain(f'the result was not kept: {error}', ['artifacts/components is not writable'],
                      f'ls -ld {path.parent}', f'fix that, then run {cli_name()} ops update {command} again'),
              file=sys.stderr)
        return code or 3
    return code


def check(args):
    previous = updates.load_previous(RECORD)
    report = updates.check(declared.load(ROOT).components, previous)
    audited = audit.run(ROOT, audit.load(AUDIT))
    if args.json:
        print(json.dumps({**report, 'audit': audit.counts(audited)}, sort_keys=True))
    else:
        print(updates.screen(report), f'audit: {audit.line(audited)}', sep='\n')
    if args.notify:
        tell(updates.news(report, previous))
    return keep(RECORD, report, keep(AUDIT, audited, updates.exit_code(report), 'check'), 'check')


def tell(news):
    if news:
        answer = evaluation_watch.desktop('Update available', ', '.join(news) + f'\n{cli_name()} ops update plan ID', 'normal')
        print(f'notification: {answer}', file=sys.stderr)


def observe(args):
    graph = declared.load(ROOT)
    report = updates.observe(graph.components, Probes(), updates.manifests_by_id(ROOT, graph.backends),
                             updates.load_previous(OBSERVED))
    print(json.dumps(report, sort_keys=True) if args.json else updates.screen_observed(report))
    return keep(OBSERVED, report, updates.observe_exit_code(report), 'observe')


def plan(args):
    components = declared.load(ROOT).components
    try:
        shown = updates.plan(updates.find(components, args.id), updates.load_previous(RECORD),
                             updates.load_report(OBSERVED), ROOT)
    except updates.Refused as refused:
        print(refused, file=sys.stderr)
        return 2
    except updates.FAILURES as error:
        print(explain(f'no plan for {args.id}: {error}', ['the documents no longer hold the row this component uses'],
                      f'grep -n "### 8.1" {updates.PROCEDURES}', f'put the row back in {updates.PROCEDURES} or '
                      f'{updates.APPROVALS}, or change updates.ROUTES to match'), file=sys.stderr)
        return 3
    print(json.dumps(shown, sort_keys=True, ensure_ascii=False) if args.json else updates.screen_plan(shown))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    parsers = {name: sub.add_parser(name, help=text) for name, text in (
        ('check', 'read what upstream offers'), ('observe', 'read what the machines run'))}
    for parser_ in parsers.values():
        parser_.add_argument('--json', action='store_true', help='one JSON document instead of the table')
    parsers['check'].add_argument('--notify', action='store_true', help='tell the desktop about a new `notify` version')
    planning = sub.add_parser('plan', help='print what updating one component would take')
    planning.add_argument('id', metavar='ID', help='a component in infra/components.yml')
    planning.add_argument('--json', action='store_true', help='one JSON document instead of the lines')
    args = parser.parse_args()
    os.umask(0o077)
    return {'check': check, 'observe': observe, 'plan': plan}[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
