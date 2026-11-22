"""Explicit, allowlisted minimal-probe worker. A lease alone never permits recovery."""
import argparse
from contextlib import nullcontext
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from agentiac.management.errors import ApiError
from .ledger import Ledger, ROOT
from agentiac.management.paths import STATE_DIR, require_ready
from agentiac.management.lifecycle import StateLock
from .probe import LiveProbeExecutor, endpoint_for


class WorkerLock:
    def __init__(self, ledger):
        self.path = ledger.path.parent / 'probe-worker.lock'
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise ApiError(409, 'worker_busy', 'another probe worker still holds the OS lock') from None
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)


def run_once(ledger, executor, owner, lease_s=60, now=None, targets=None, locked=False, publish=False):
    with (nullcontext() if locked else StateLock(ledger.path.parent)), (nullcontext() if locked else WorkerLock(ledger)):
        require_ready(ledger.path.parent)
        job = ledger.dequeue(owner, lease_s=lease_s, now=now, targets=targets)
        if job is None:
            return None
        last_heartbeat = 0

        def tick():
            nonlocal last_heartbeat
            if time.monotonic() - last_heartbeat >= 1:
                if not ledger.renew(job['id'], owner, job['attempt'], lease_s=lease_s):
                    raise ApiError(409, 'lease', 'probe worker lost its execution lease')
                if publish:
                    ledger.publish_worker(owner, targets)
                last_heartbeat = time.monotonic()

        if isinstance(executor, LiveProbeExecutor):
            executor.ledger, executor.tick = ledger, tick
        try:
            outcome = executor.run(job)
            for line in outcome.get('logs', []):
                ledger.append_log(job['id'], line, now=now)
            for event in outcome.get('events', []):
                ledger.append_event(job['id'], event['kind'], event['message'], now=now)
            ledger.finish(job['id'], owner, job['attempt'], outcome['status'], outcome.get('result'), now=now)
        except ValueError:
            # A preflight rejection before durable intent never launched a process.
            if ledger.execution(job['id'], job['attempt']) is None:
                ledger.finish(job['id'], owner, job['attempt'], 'failed', {'reason': 'probe_preflight_rejected'}, now=now)
            else:
                ledger.attention(job)
        except (RuntimeError, OSError, ApiError, sqlite3.Error):
            # If the disk is full, this write may also fail. The existing running intent remains blocking.
            ledger.attention(job)
        return job


def reconcile(ledger, executor, job_id=None, stop=False, targets=None, locked=False):
    with (nullcontext() if locked else StateLock(ledger.path.parent)), (nullcontext() if locked else WorkerLock(ledger)):
        require_ready(ledger.path.parent)
        results = []
        for job in ledger.unresolved():
            if job_id is not None and job['id'] != job_id:
                continue
            if targets is not None and job['target'] not in targets:
                continue
            execution = ledger.execution(job['id'], job['attempt'])
            if ((execution is None and ledger.prelaunch_proven(job))
                    or (execution is not None and executor.reconcile(execution, stop=stop))):
                results.append({'job_id': job['id'], 'status': ledger.resolve_stopped(job)})
            else:
                if job['status'] in {'running', 'cancel_requested'}:
                    ledger.attention(job)
                results.append({'job_id': job['id'], 'status': 'needs_attention'})
        if job_id is not None and not results:
            row = ledger.get(job_id)
            if row is None:
                raise ApiError(404, 'unknown_job', 'job is not in the probe ledger')
            results.append({'job_id': job_id, 'status': row['status']})
        return results


def live_once(root, owner, lease_s=60, now=None, targets=None):
    state = STATE_DIR if Path(root) == ROOT else Path(root) / 'private/management'
    with StateLock(state):
        require_ready(state)
        ledger = Ledger(root)
        try:
            if not targets:
                raise ValueError('an explicit probe target allowlist is required')
            return run_once(ledger, LiveProbeExecutor(root), owner, lease_s, now, targets=targets)
        finally:
            ledger.close()


def _main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit minimal OS/CPU/memory probe worker')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--serve', action='store_true', help='publish readiness and process allowlisted jobs')
    group.add_argument('--reconcile', metavar='JOB_ID', help='verify one interrupted attempt; never retry it')
    parser.add_argument('--target', action='append', default=[], help='declared node allowed for this worker')
    parser.add_argument('--stop-probe', action='store_true', help='stop only the named owned probe during reconciliation')
    args = parser.parse_args(argv)
    if args.stop_probe and args.reconcile is None:
        parser.error('--stop-probe requires --reconcile JOB_ID')
    if args.serve and not args.target:
        parser.error('--serve requires at least one --target NODE')
    if args.reconcile and args.target:
        parser.error('--target is only used with --serve; reconciliation uses the saved endpoint')
    for target in args.target:
        try:
            endpoint_for(ROOT, target)
        except ValueError as error:
            parser.error(str(error))
    gate = StateLock(STATE_DIR)
    gate.__enter__()
    try:
        require_ready(STATE_DIR)
        ledger = Ledger(ROOT)
    except BaseException:
        gate.__exit__()
        raise
    owner = uuid.uuid4().hex
    executor = LiveProbeExecutor(ROOT)
    try:
        with WorkerLock(ledger):
            if args.reconcile:
                print(json.dumps(reconcile(ledger, executor, args.reconcile, args.stop_probe, locked=True)))
                return
            reconcile(ledger, executor, targets=args.target, locked=True)
            if ledger.unresolved():
                raise SystemExit('unresolved probe attempts remain; reconcile their named job IDs first')
            published = 0.0
            while True:
                # readiness lives ttl_s=10; publishing every 3 s instead of every 0.5 s idle tick keeps it fresh
                # without a SQLite commit twice a second (F14, D5.2)
                if time.monotonic() - published >= 3:
                    ledger.publish_worker(owner, args.target)
                    published = time.monotonic()
                job = run_once(ledger, executor, owner, targets=args.target, locked=True, publish=True)
                if ledger.unresolved():
                    raise SystemExit('probe needs reconciliation; worker stopped accepting new jobs')
                if job is None:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    except ApiError as error:
        raise SystemExit(error.message) from None
    finally:
        try:
            ledger.unpublish_worker(owner)
        finally:
            ledger.close()
            gate.__exit__()


def main(argv=None):
    return _main(argv)


if __name__ == '__main__':
    main()
