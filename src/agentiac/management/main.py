"""Launch the authenticated management API on loopback; maintenance is offline."""
import argparse
import sqlite3

from .lifecycle import StateLock
from .paths import ROOT, SESSION_PATH, require_complete, require_ready
from .tls import validate_tls


def main(argv=None):
    parser = argparse.ArgumentParser(description='agentiac management API')
    parser.add_argument('--host', default='127.0.0.1', choices=('127.0.0.1', 'localhost'))
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--init-owner', action='store_true', help='initialize the owner password offline')
    parser.add_argument('--tls-cert')
    parser.add_argument('--tls-key')
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error('use an unprivileged port between 1024 and 65535')
    from .session import SessionStore
    from agentiac.operations.ledger import Ledger
    try:
        tls = validate_tls(args.tls_cert, args.tls_key)
        with StateLock(SESSION_PATH.parent, exclusive=args.init_owner):
            (require_complete if args.init_owner else require_ready)(SESSION_PATH.parent)
            if not args.init_owner:
                from .snapshots import validate_startup
                validate_startup(SESSION_PATH.parent)
            store = SessionStore(SESSION_PATH)
            if args.init_owner:
                from getpass import getpass
                password = getpass('Owner password (12..256 characters): ')
                if password != getpass('Confirm password: '):
                    raise ValueError('passwords do not match')
                store.initialize(password)
                ledger = Ledger(ROOT)
                ledger.close()
                return
            if not store.initialized():
                raise ValueError('initialize owner credentials first with --init-owner; legacy hashes are refused')
    except (ValueError, OSError, sqlite3.Error) as error:
        raise SystemExit(str(error)) from None
    from uvicorn import run
    run('agentiac.management.app:app', host=args.host, port=args.port, log_level='info',
        proxy_headers=False, timeout_keep_alive=2, timeout_graceful_shutdown=5, **tls)


if __name__ == '__main__':
    main()
