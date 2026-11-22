"""Local-only management maintenance and reviewed service deployment entry points."""
import argparse
import os
import sys
import importlib.util
from contextlib import closing
from getpass import getpass
import json
from pathlib import Path
import sqlite3

from .paths import ROOT, STATE_DIR, SOURCE_ROOT, require_complete
from .lifecycle import StateLock
from . import snapshots, releases, deployment


def configure(parser):
    parser.add_argument('--state-dir', type=Path, default=STATE_DIR)
    parser.add_argument('--root', type=Path, default=ROOT)
    sub = parser.add_subparsers(dest='dashboard_action', required=True)
    p = sub.add_parser('serve', help='run the loopback API with the isolated dashboard interpreter')
    p.add_argument('--host', choices=('127.0.0.1', 'localhost'), default='127.0.0.1')
    p.add_argument('--port', type=int, default=8000)
    p.add_argument('--tls-cert')
    p.add_argument('--tls-key')
    p = sub.add_parser('worker', help='run an explicitly scoped probe worker/reconciliation')
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--serve', action='store_true')
    group.add_argument('--reconcile')
    p.add_argument('--target', action='append', default=[])
    p.add_argument('--stop-probe', action='store_true')
    p = sub.add_parser('backup', help='offline, private management DB snapshot')
    p.add_argument('destination', type=Path)
    p = sub.add_parser('verify', help='verify a private management snapshot')
    p.add_argument('snapshot', type=Path)
    p = sub.add_parser('restore', help='restore only to a new state directory')
    p.add_argument('snapshot', type=Path)
    p.add_argument('destination', type=Path)
    p = sub.add_parser('owner', help='offline owner initialization/rotation/recovery')
    p.add_argument('operation', choices=('init', 'rotate', 'recover'))
    p.add_argument('--backup', type=Path, help='new private snapshot destination, required for rotation/recovery')
    p.add_argument('--confirm-owner', choices=('owner',))
    p = sub.add_parser('release', help='prepare/verify/provision a dedicated immutable code release')
    p.add_argument('operation', choices=('prepare', 'verify', 'provision'))
    p.add_argument('path', type=Path)
    p.add_argument('--source', type=Path)
    p.add_argument('--environment', action='store_true')
    p = sub.add_parser('plan', help='freeze exactly two owned user-service actions; performs no changes')
    p.add_argument('--action', choices=('activate', 'stop', 'rollback'), default='activate')
    p.add_argument('--release', type=Path)
    p.add_argument('--unit-dir', type=Path, default=Path.home() / '.config/systemd/user')
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=8000)
    p.add_argument('--target', action='append', default=[])
    p.add_argument('--tls-cert')
    p.add_argument('--tls-key')
    p.add_argument('--tls-ca')
    p.add_argument('--ssh-tunnel', action='store_true')
    p.add_argument('--enable-at-login', action='store_true')
    p.add_argument('--allow-uncommitted', action='store_true')
    p = sub.add_parser('apply', help='apply exactly the reviewed local user-service plan')
    p.add_argument('plan', type=Path)
    p.add_argument('--approve-plan', required=True)
    parser.set_defaults(func=run)
    return parser


def launch(args):
    candidates = [SOURCE_ROOT / '.venv-dashboard/bin/python']
    if (SOURCE_ROOT / 'release.json').is_file():
        candidates.insert(0, SOURCE_ROOT / '.venv/bin/python')
    python = next((str(path) for path in candidates if path.is_file()), None)
    if python is None and importlib.util.find_spec('fastapi') is not None:
        python = sys.executable
    if python is None:
        raise ValueError('provision the isolated dashboard environment first')
    if args.dashboard_action == 'serve':
        module = 'agentiac.management.main'
        flags = ['--host', args.host, '--port', str(args.port)]
        for name in ('tls_cert', 'tls_key'):
            if getattr(args, name):
                flags += ['--' + name.replace('_', '-'), getattr(args, name)]
    else:
        module = 'agentiac.operations.worker'
        flags = ['--serve'] if args.serve else ['--reconcile', args.reconcile]
        for target in args.target:
            flags += ['--target', target]
        if args.stop_probe:
            flags.append('--stop-probe')
    env = {**os.environ, 'PYTHONPATH': str(SOURCE_ROOT / 'src'), 'PYTHONNOUSERSITE': '1',
           'AGENTIAC_MANAGEMENT_ROOT': str(args.root.absolute()),
           'AGENTIAC_MANAGEMENT_STATE': str(args.state_dir.absolute())}
    os.execve(python, [python, '-m', module, *flags], env)
    return {'delegated': True}


def owner(args):
    from .session import SessionStore
    from agentiac.operations.ledger import Ledger
    if args.operation != 'init' and args.backup is None:
        raise ValueError('rotation/recovery requires --backup with a new private snapshot destination')
    if args.operation == 'recover' and args.confirm_owner != 'owner':
        raise ValueError('offline recovery requires --confirm-owner owner')
    with StateLock(args.state_dir, exclusive=True):
        require_complete(args.state_dir)
        store = SessionStore(args.state_dir / 'session.sqlite')
        old = getpass('Current owner password: ') if args.operation == 'rotate' else None
        password = getpass('New owner password (12..256 characters): ')
        if password != getpass('Confirm new password: '):
            raise ValueError('passwords do not match')
        if not 12 <= len(password) <= 256:
            raise ValueError('owner password must be 12..256 characters')
        if args.operation == 'init':
            store.initialize(password)
            ledger = Ledger(args.root, path=args.state_dir / 'operations.sqlite', log_dir=args.state_dir / 'jobs')
            ledger.close()
        else:
            snapshots.backup(args.state_dir, args.backup, locked=True, root=args.root)
            store.change_password(password, old_password=old, recover=args.operation == 'recover')
            (args.state_dir / '.credential-review-required').unlink(missing_ok=True)
            snapshots.fsync(args.state_dir)
        return {'owner': 'initialized' if args.operation == 'init' else 'rotated', 'sessions_revoked': True}


def run(args):
    try:
        action = args.dashboard_action
        if action in {'serve', 'worker'}:
            result = launch(args)
        elif action == 'backup':
            result = snapshots.backup(args.state_dir, args.destination, root=args.root)
        elif action == 'verify':
            result = snapshots.verify(args.snapshot)
        elif action == 'restore':
            result = snapshots.restore(args.snapshot, args.destination, root=args.root)
        elif action == 'owner':
            result = owner(args)
        elif action == 'release':
            if args.operation == 'prepare':
                if args.source is None:
                    raise ValueError('release prepare requires --source CHECKOUT')
                result = releases.prepare(args.source, args.path)
            elif args.operation == 'provision':
                result = releases.provision(args.path)
            else:
                result = releases.verify(args.path, environment=args.environment)
            result = {key: value for key, value in result.items() if key != 'files'}
        elif action == 'plan':
            if args.action == 'activate' and args.release is None:
                raise ValueError('an activation plan requires --release')
            plan = deployment.create_plan(args.release, args.root, args.state_dir, args.unit_dir, args.history,
                    port=args.port, targets=args.target, cert=args.tls_cert, key=args.tls_key, ca=args.tls_ca,
                    ssh_tunnel=args.ssh_tunnel, enable=args.enable_at_login,
                    allow_uncommitted=args.allow_uncommitted, action=args.action)
            if args.output.exists():
                raise ValueError('a deployment plan output is never overwritten')
            deployment.write_json(args.output, plan)
            result = {'plan': str(args.output), 'sha256': deployment.plan_hash(plan), 'action': plan['action'],
                      'units': list(releases.UNITS), 'probe_targets': args.target}
        elif action == 'apply':
            snapshots.private_file(args.plan)
            plan = json.loads(args.plan.read_text())
            result = deployment.apply(plan, args.approve_plan)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from None


def main(argv=None):
    parser = configure(argparse.ArgumentParser(description=__doc__))
    return run(parser.parse_args(argv))


if __name__ == '__main__':
    main()
