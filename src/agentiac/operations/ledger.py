"""SQLite operation ledger: explicit acceptance, idempotency, leases, attempts and bounded logs.

The ledger stores stable job IDs and sanitised log rows. It does not run a live SSH probe,
does not turn a GET into a mutation, and does not expose a filesystem path to the browser.
"""
import base64
import math
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from agentiac.management.errors import ApiError
from agentiac.services.inventory import nodes

from agentiac.management.paths import ROOT, STATE_DIR

DB_PATH = STATE_DIR / 'operations.sqlite'
LOG_DIR = STATE_DIR / 'jobs'

LEASE_S = 60
LOG_MAX_BYTES = 1_048_576
ANSI = re.compile(r'\x1B\[[0-9;]*[A-Za-z]')
CONTROL = re.compile(r'[\x00-\x08\x0B-\x1F\x7F]')


def sanitize(text):
    text = ANSI.sub('', str(text))
    text = CONTROL.sub('', text)
    return text.replace('\r', '')


def args_hash(args):
    payload = json.dumps(args, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts is not None else None


def _job(row):
    data = dict(row)
    for key in ('created_at', 'started_at', 'finished_at', 'lease_until'):
        data[key] = iso(data[key])
    if data['result'] is not None:
        try:
            data['result'] = json.loads(data['result'])
        except ValueError:
            data['result'] = {}
    return data


class Ledger:
    def __init__(self, root=ROOT, path=None, log_dir=None, create=True):
        """`create=False` opens a ledger whose schema this process already prepared.

        It runs no mkdir/chmod/DDL and refuses a missing file instead of recreating it empty.
        """
        self.root = Path(root)
        self.path = Path(path or (DB_PATH if self.root == ROOT else self.root / 'private/management/operations.sqlite'))
        self.log_dir = Path(log_dir or (LOG_DIR if self.root == ROOT else self.root / 'private/management/jobs'))
        if not create:
            try:
                self.conn = sqlite3.connect(f'file:{quote(str(self.path))}?mode=rw', uri=True,
                                            timeout=10, check_same_thread=False)
            except sqlite3.OperationalError:
                raise ApiError(503, 'state_missing', 'operation ledger is missing; restore it offline') from None
            self.conn.execute('PRAGMA trusted_schema=OFF')
            self.conn.row_factory = sqlite3.Row
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
            self.conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            self.conn.execute('PRAGMA trusted_schema=OFF')
            self.conn.row_factory = sqlite3.Row
            self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.executescript(
                '''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    target TEXT NOT NULL,
                    args_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    lease_until REAL,
                    owner TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    timeout_s REAL NOT NULL,
                    cancelability TEXT NOT NULL,
                    result TEXT,
                    reason TEXT
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    job_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    started REAL NOT NULL,
                    finished REAL,
                    status TEXT NOT NULL,
                    result TEXT,
                    owner TEXT,
                    PRIMARY KEY (job_id, number)
                );
                CREATE TABLE IF NOT EXISTS events (
                    job_id TEXT NOT NULL,
                    event_id INTEGER NOT NULL,
                    at REAL NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    PRIMARY KEY (job_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS logs (
                    job_id TEXT NOT NULL,
                    line_id INTEGER NOT NULL,
                    at REAL NOT NULL,
                    text TEXT NOT NULL,
                    PRIMARY KEY (job_id, line_id)
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    key TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    target TEXT NOT NULL,
                    args_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS probe_attempt_receipts (
                    job_id TEXT NOT NULL, attempt INTEGER NOT NULL, protocol TEXT NOT NULL,
                    PRIMARY KEY(job_id, attempt)
                );
                CREATE TABLE IF NOT EXISTS executions (
                    job_id TEXT NOT NULL, attempt INTEGER NOT NULL, token TEXT NOT NULL UNIQUE,
                    endpoint TEXT NOT NULL, argv TEXT NOT NULL, code_sha256 TEXT NOT NULL,
                    local_handle TEXT, remote_handle TEXT, authorized INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, resolved_at REAL,
                    PRIMARY KEY(job_id, attempt)
                );
                CREATE TABLE IF NOT EXISTS probe_workers (
                    owner TEXT PRIMARY KEY, identity TEXT NOT NULL, targets TEXT NOT NULL,
                    implementation TEXT NOT NULL, ready_until REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS probe_snapshots (
                    target TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                    data TEXT NOT NULL, recorded_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    target TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resource_locks (
                    resource TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    lease_until REAL NOT NULL
                );
                '''
            )
            self.conn.commit()
            os.chmod(self.path, 0o600)
            os.chmod(self.path.parent, 0o700)
            os.chmod(self.log_dir, 0o700)
        except Exception:
            if hasattr(self, "conn"):
                self.conn.close()
            raise

    def close(self):
        self.conn.close()

    def _begin(self):
        self.conn.execute('BEGIN IMMEDIATE')

    def accept(self, request, actor, permission, timeout_s=None, now=None, require_worker=False):
        timeout_s = request.timeout_s if timeout_s is None else timeout_s
        if request.operation not in {'probe-node'}:
            raise ApiError(403, 'operation', 'operation is not an accepted read-only job', retryable=False)
        if permission != 'operator' and permission != 'admin':
            raise ApiError(403, 'permission', 'operator permission is required', retryable=False)
        if not math.isfinite(timeout_s) or timeout_s < 1 or timeout_s > 30:
            raise ApiError(422, 'schema_error', 'timeout_s must be between 1 and 30 seconds', retryable=False)
        if request.operation == 'probe-node':
            declared = nodes(self.root)
            if request.target not in declared:
                raise ApiError(404, 'unknown_target', 'target node is not declared in inventory', retryable=False)
        if set(request.args) - {'probe'}:
            raise ApiError(422, 'schema_error', 'args contains an unsupported key', retryable=False)
        if 'probe' in request.args and request.args['probe'] is not True:
            raise ApiError(422, 'schema_error', 'probe must be true', retryable=False)
        digest = args_hash({'args': request.args, 'timeout_s': float(timeout_s)})
        now = time.time() if now is None else now
        self._begin()
        try:
            row = self.conn.execute(
                'SELECT * FROM idempotency WHERE key = ?', (request.idempotency_key,)
            ).fetchone()
            if row is not None:
                if (row['actor'] != actor or row['operation'] != request.operation
                        or row['target'] != request.target or row['args_hash'] != digest):
                    raise ApiError(409, 'conflict', 'idempotency key was reused for different arguments', retryable=False)
                job = self.conn.execute('SELECT * FROM jobs WHERE id = ?', (row['job_id'],)).fetchone()
                self.conn.commit()
                return {
                    'job_id': job['id'],
                    'status': job['status'],
                    'location': f'/api/v1/jobs/{job["id"]}',
                    'as_of': iso(now),
                }
            if require_worker:
                targets = self.available_targets(now)
                if not targets:
                    raise ApiError(503, 'probe_unavailable', 'no ready minimal-probe worker')
                if request.target not in targets:
                    raise ApiError(403, 'probe_target', 'target is outside the worker allowlist')
                recent = self.conn.execute('SELECT MAX(created_at) FROM jobs WHERE target=?',
                                           (request.target,)).fetchone()[0]
                if recent is not None and now - recent < 5:
                    raise ApiError(429, 'probe_cooldown', 'wait at least five seconds between probes', retryable=True)
            job_id = uuid.uuid4().hex
            self.conn.execute(
                '''INSERT INTO jobs
                   (id, idempotency_key, actor, operation, target, args_hash, status,
                    created_at, attempt, timeout_s, cancelability, reason)
                   VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, 1, ?, 'checkpoint_only', 'queued')''',
                (job_id, request.idempotency_key, actor, request.operation, request.target,
                 digest, now, timeout_s),
            )
            self.conn.execute(
                'INSERT INTO idempotency (key, job_id, actor, operation, target, args_hash) VALUES (?, ?, ?, ?, ?, ?)',
                (request.idempotency_key, job_id, actor, request.operation, request.target, digest),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {
            'job_id': job_id,
            'status': 'queued',
            'location': f'/api/v1/jobs/{job_id}',
            'as_of': iso(now),
        }

    def dequeue(self, owner, lease_s=LEASE_S, now=None, targets=None):
        now = time.time() if now is None else now
        self._begin()
        try:
            if self.conn.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('running', 'cancel_requested', 'needs_attention') LIMIT 1"
            ).fetchone():
                self.conn.commit()
                return None
            if targets is None:
                row = self.conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1").fetchone()
            elif not targets:
                row = None
            else:
                marks = ','.join('?' for _ in targets)
                row = self.conn.execute(
                    f"SELECT * FROM jobs AS j WHERE status='queued' AND target IN ({marks}) "
                    "AND NOT EXISTS (SELECT 1 FROM attempts AS a JOIN jobs AS prior ON prior.id=a.job_id "
                    "WHERE prior.target=j.target AND a.started>?) ORDER BY created_at, id LIMIT 1",
                    (*targets, now - 5)).fetchone()
            if row is None:
                self.conn.commit()
                return None
            number = row['attempt']
            lease_until = now + lease_s
            self.conn.execute(
                '''UPDATE jobs SET status = 'running', started_at = ?, owner = ?, lease_until = ?
                   WHERE id = ? AND status = 'queued' AND attempt = ?''',
                (now, owner, lease_until, row['id'], number),
            )
            self.conn.execute(
                '''INSERT INTO attempts (job_id, number, started, status, owner)
                   VALUES (?, ?, ?, 'running', ?)''',
                (row['id'], number, now, owner),
            )
            self.conn.execute('INSERT INTO probe_attempt_receipts VALUES (?, ?, ?)',
                              (row['id'], number, 'minimal-probe-v1'))
            self.conn.commit()
            return {
                'id': row['id'],
                'operation': row['operation'],
                'target': row['target'],
                'timeout_s': row['timeout_s'],
                'attempt': number,
                'status': 'running',
                'owner': owner,
                'lease_until': iso(lease_until),
            }
        except Exception:
            self.conn.rollback()
            raise

    def renew(self, job_id, owner, number, lease_s=LEASE_S, now=None):
        now = time.time() if now is None else now
        self._begin()
        try:
            cursor = self.conn.execute(
                '''UPDATE jobs SET lease_until = ?
                   WHERE id = ? AND owner = ? AND status IN ('running', 'cancel_requested') AND attempt = ?
                     AND lease_until IS NOT NULL AND lease_until > ?''',
                (now + lease_s, job_id, owner, number, now),
            )
            self.conn.commit()
            return cursor.rowcount == 1
        except Exception:
            self.conn.rollback()
            raise

    def finish(self, job_id, owner, number, status, result=None, now=None):
        if status not in {'succeeded', 'failed', 'partial', 'interrupted', 'needs_attention', 'cancelled'}:
            raise ApiError(422, 'schema_error', 'invalid terminal status')
        now = time.time() if now is None else now
        self._begin()
        try:
            row = self.conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
            if row is None or row['owner'] != owner or row['status'] not in {'running', 'cancel_requested'} or row['attempt'] != number:
                raise ApiError(409, 'lease', 'job is not owned by this worker at this attempt', retryable=False)
            if row['lease_until'] is None or row['lease_until'] <= now:
                raise ApiError(409, 'lease_expired', 'lease expired; do not re-permit execution', retryable=False)
            result_text = json.dumps(result or {}, sort_keys=True, separators=(',', ':'))
            if status == 'succeeded' and result and 'probe' in result:
                from .probe import valid_data
                if not valid_data(result['probe']):
                    raise ApiError(422, 'schema_error', 'invalid probe observation')
                self.conn.execute('INSERT OR REPLACE INTO probe_snapshots VALUES (?, ?, ?, ?, ?)',
                                  (row['target'], job_id, number, json.dumps(result['probe']), now))
            self.conn.execute(
                '''UPDATE attempts SET finished = ?, status = ?, result = ?
                   WHERE job_id = ? AND number = ?''',
                (now, status, result_text, job_id, number),
            )
            self.conn.execute(
                '''UPDATE jobs SET status = ?, finished_at = ?, owner = NULL,
                    lease_until = NULL, result = ?, reason = ?
                   WHERE id = ? AND owner = ? AND attempt = ?''',
                (status, now, result_text, 'cancel_requested' if row['status'] == 'cancel_requested'
                 and status == 'needs_attention' else status, job_id, owner, number),
            )
            if result and result.get('stopped') and status != 'needs_attention':
                self.conn.execute('UPDATE executions SET resolved_at=? WHERE job_id=? AND attempt=?',
                                  (now, job_id, number))
            self.conn.commit()
            return {'job_id': job_id, 'status': status, 'finished_at': iso(now)}
        except Exception:
            self.conn.rollback()
            raise

    def cancel(self, job_id, permission, now=None):
        if permission not in {'operator', 'admin'}:
            raise ApiError(403, 'permission', 'operator permission is required', retryable=False)
        now = time.time() if now is None else now
        self._begin()
        try:
            row = self.conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
            if row is None:
                raise ApiError(404, 'unknown_job', 'job is not in the ledger', retryable=False)
            if row['status'] not in {'queued', 'running', 'cancel_requested'}:
                raise ApiError(409, 'conflict', 'job is already finished', retryable=False)
            if row['cancelability'] == 'unsupported':
                raise ApiError(409, 'not_cancelable', 'job type cannot be cancelled', retryable=False)
            new_status = 'cancelled' if row['status'] == 'queued' else 'cancel_requested'
            self.conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, reason = ? WHERE id = ?",
                (new_status, now if new_status == 'cancelled' else None, new_status, job_id),
            )
            self._event(job_id, 'cancel', new_status, now)
            self.conn.commit()
            return {'job_id': job_id, 'status': new_status}
        except Exception:
            self.conn.rollback()
            raise

    def retry(self, job_id, permission, now=None, require_worker=False):
        if permission not in {'operator', 'admin'}:
            raise ApiError(403, 'permission', 'operator permission is required', retryable=False)
        now = time.time() if now is None else now
        self._begin()
        try:
            row = self.conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
            if row is None:
                raise ApiError(404, 'unknown_job', 'job is not in the ledger', retryable=False)
            if row['status'] not in {'failed', 'interrupted'}:
                raise ApiError(409, 'conflict', 'job is not retryable', retryable=False)
            execution = self.execution(row['id'], row['attempt'])
            result = json.loads(row['result']) if row['result'] else {}
            if ((execution is not None and not result.get('stopped'))
                    or (execution is None and not self.prelaunch_proven(row))):
                raise ApiError(409, 'reconciliation_required', 'prior process absence has not been proven')
            if row['target'] not in nodes(self.root):
                raise ApiError(404, 'unknown_target', 'target is no longer declared')
            if require_worker and row['target'] not in self.available_targets(now):
                raise ApiError(503, 'probe_unavailable', 'no ready allowlisted worker for this target')
            attempt = row['attempt'] + 1
            self.conn.execute(
                '''UPDATE jobs SET status = 'queued', attempt = ?, owner = NULL,
                    lease_until = NULL, started_at = NULL, finished_at = NULL, result = NULL,
                    reason = 'retry queued' WHERE id = ?''',
                (attempt, job_id),
            )
            self._event(job_id, 'retry', f'attempt {attempt}', now)
            self.conn.commit()
            return {'job_id': job_id, 'status': 'queued', 'attempt': attempt}
        except Exception:
            self.conn.rollback()
            raise

    def append_event(self, job_id, kind, message, now=None):
        now = time.time() if now is None else now
        self._begin()
        try:
            next_id = self._event(job_id, kind, message, now)
            self.conn.commit()
            return next_id
        except Exception:
            self.conn.rollback()
            raise

    def _event(self, job_id, kind, message, now):
        next_id = self.conn.execute(
            'SELECT COALESCE(MAX(event_id), 0) + 1 FROM events WHERE job_id = ?', (job_id,)
        ).fetchone()[0]
        self.conn.execute(
            'INSERT INTO events (job_id, event_id, at, kind, message) VALUES (?, ?, ?, ?, ?)',
            (job_id, next_id, now, sanitize(kind)[:64], sanitize(message)[:4096]),
        )
        return next_id

    def append_log(self, job_id, text, now=None):
        now = time.time() if now is None else now
        line = sanitize(text)
        total = self.conn.execute(
            'SELECT COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0) FROM logs WHERE job_id = ?', (job_id,)
        ).fetchone()[0]
        if len(line.encode('utf-8')) > 65536 or total + len(line.encode('utf-8')) > LOG_MAX_BYTES:
            raise ApiError(503, 'log_full', 'bounded log capacity reached', retryable=False)
        next_id = self.conn.execute(
            'SELECT COALESCE(MAX(line_id), 0) + 1 FROM logs WHERE job_id = ?', (job_id,)
        ).fetchone()[0]
        self.conn.execute(
            'INSERT INTO logs (job_id, line_id, at, text) VALUES (?, ?, ?, ?)',
            (job_id, next_id, now, line),
        )
        self.conn.commit()
        return next_id

    def read_logs(self, job_id, cursor=None, limit=50, byte_limit=65536, now=None):
        self._limit(limit)
        now = time.time() if now is None else now
        row = self.conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if row is None:
            raise ApiError(404, 'unknown_job', 'job is not in the ledger', retryable=False)
        try:
            c = int(cursor) if cursor is not None else None
            if c is not None and c < 0:
                raise ValueError('negative cursor')
        except (TypeError, ValueError):
            raise ApiError(422, 'schema_error', 'cursor must be an integer', retryable=False)
        if c is not None:
            oldest = self.conn.execute(
                'SELECT MIN(line_id) FROM logs WHERE job_id = ?', (job_id,)
            ).fetchone()[0]
            if oldest is not None and c < oldest - 1:
                raise ApiError(410, 'log_retention', 'log cursor is outside the retained window', retryable=False)
        rows = self.conn.execute(
            '''SELECT line_id, text FROM logs
               WHERE job_id = ? AND line_id > ?
               ORDER BY line_id LIMIT ?''', (job_id, c or 0, limit)
        ).fetchall()
        lines = []
        used = 0
        next_cursor = None
        for r in rows:
            encoded = len(r['text'].encode('utf-8'))
            if used + encoded > byte_limit:
                break
            lines.append(r['text'])
            used += encoded
            next_cursor = str(r['line_id'])
        return {
            'schema_version': 1,
            'as_of': iso(now),
            'next_cursor': next_cursor,
            'lines': lines,
            'reason': None if rows else 'no log lines in retained window',
        }

    def read_events(self, job_id, cursor=None, limit=50, now=None):
        self._limit(limit)
        now = time.time() if now is None else now
        row = self.conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if row is None:
            raise ApiError(404, 'unknown_job', 'job is not in the ledger', retryable=False)
        try:
            c = int(cursor) if cursor is not None else None
            if c is not None and c < 0:
                raise ValueError('negative cursor')
        except (TypeError, ValueError):
            raise ApiError(422, 'schema_error', 'cursor must be an integer', retryable=False)
        oldest = self.conn.execute(
            'SELECT MIN(event_id) FROM events WHERE job_id = ?', (job_id,)
        ).fetchone()[0]
        if c is not None and oldest is not None and c < oldest - 1:
            raise ApiError(410, 'event_retention', 'event cursor is outside the retained window', retryable=False)
        rows = self.conn.execute(
            '''SELECT event_id, at, kind, message FROM events
               WHERE job_id = ? AND event_id > ?
               ORDER BY event_id LIMIT ?''', (job_id, c or 0, limit)
        ).fetchall()
        events = [{
            'event_id': r['event_id'],
            'at': iso(r['at']),
            'kind': r['kind'],
            'message': r['message'],
        } for r in rows]
        return {
            'schema_version': 1,
            'as_of': iso(now),
            'next_cursor': str(events[-1]['event_id']) if events else None,
            'events': events,
            'reason': None if events else 'no events in retained window',
        }

    def get(self, job_id):
        row = self.conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        return None if row is None else _job(row)

    @staticmethod
    def _limit(limit):
        if not 1 <= limit <= 200:
            raise ApiError(422, 'schema_error', 'limit must be 1..200')

    def list(self, cursor=None, limit=50, now=None, newest_first=False):
        """One page of jobs. `newest_first` pages backwards from the most recent job."""
        self._limit(limit)
        now = time.time() if now is None else now
        order = 'DESC' if newest_first else 'ASC'
        if cursor is None:
            where, params = '1', ()
        else:
            try:
                timestamp, job_id = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or not isinstance(job_id, str):
                    raise ValueError('invalid cursor')
            except (ValueError, TypeError, UnicodeError):
                raise ApiError(422, 'schema_error', 'invalid job cursor') from None
            after = '<' if newest_first else '>'
            where = f'created_at {after} ? OR (created_at = ? AND id {after} ?)'
            params = (timestamp, timestamp, job_id)
        rows = self.conn.execute(
            f"SELECT * FROM jobs WHERE {where} ORDER BY created_at {order}, id {order} LIMIT ?",
            (*params, limit + 1)
        ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if more:
            last = rows[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last['created_at'], last['id']]).encode()).decode()
        return {'schema_version': 1, 'as_of': iso(now), 'next_cursor': next_cursor,
                'jobs': [_job(row) for row in rows]}

    def recent_for_target(self, target, limit=10):
        """The newest jobs for one target, newest first (the dashboard's per-node relations)."""
        self._limit(limit)
        rows = self.conn.execute('SELECT * FROM jobs WHERE target=? ORDER BY created_at DESC, id DESC LIMIT ?',
                                 (target, limit)).fetchall()
        return [_job(row) for row in rows]


    def _owned(self, job, authorize=False):
        row = self.conn.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
        if (row is None or row['owner'] != job['owner'] or row['attempt'] != job['attempt']
                or row['status'] not in {'running', 'cancel_requested'}
                or row['lease_until'] is None or row['lease_until'] <= time.time()
                or (authorize and row['status'] != 'running')):
            raise ApiError(409, 'lease', 'probe execution is no longer owned')
        return row

    @staticmethod
    def execution_token(job):
        return uuid.uuid4().hex

    def prepare_execution(self, job, endpoint, argv, token, code_sha256):
        self._begin()
        try:
            self._owned(job)
            self.conn.execute('INSERT INTO executions '
                              '(job_id, attempt, token, endpoint, argv, code_sha256, created_at) '
                              'VALUES (?, ?, ?, ?, ?, ?, ?)',
                              (job['id'], job['attempt'], token, json.dumps(endpoint),
                               json.dumps(argv), code_sha256, time.time()))
            self._event(job['id'], 'intent', 'Minimal probe launch intent committed.', time.time())
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def record_execution(self, job, local_handle=None, remote_handle=None, authorized=False):
        self._begin()
        try:
            self._owned(job, authorize=authorized)
            if self.conn.execute('SELECT 1 FROM executions WHERE job_id=? AND attempt=?',
                                 (job['id'], job['attempt'])).fetchone() is None:
                raise ApiError(409, 'intent_missing', 'cannot authorize an unrecorded probe')
            if local_handle is not None:
                self.conn.execute('UPDATE executions SET local_handle=? WHERE job_id=? AND attempt=?',
                                  (json.dumps(local_handle), job['id'], job['attempt']))
            if remote_handle is not None:
                self.conn.execute('UPDATE executions SET remote_handle=?, authorized=? '
                                  'WHERE job_id=? AND attempt=?',
                                  (json.dumps(remote_handle), int(authorized), job['id'], job['attempt']))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def execution(self, job_id, attempt):
        row = self.conn.execute('SELECT * FROM executions WHERE job_id=? AND attempt=?',
                                (job_id, attempt)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ('endpoint', 'argv', 'local_handle', 'remote_handle'):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        return result

    def attention(self, job, reason='ownership_reconciliation_required'):
        self._begin()
        try:
            row = self.conn.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
            if (row is None or row['owner'] != job['owner'] or row['attempt'] != job['attempt']
                    or row['status'] not in {'running', 'cancel_requested'}):
                raise ApiError(409, 'lease', 'stale worker cannot change this attempt')
            if row['status'] == 'cancel_requested':
                reason = 'cancel_requested'
            self.conn.execute("UPDATE jobs SET status='needs_attention', reason=? WHERE id=?", (reason, job['id']))
            self.conn.execute("UPDATE attempts SET status='needs_attention' WHERE job_id=? AND number=?",
                              (job['id'], job['attempt']))
            self._event(job['id'], 'recovery', 'Probe requires process ownership reconciliation.', time.time())
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def unresolved(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT id, attempt, status, owner, target FROM jobs "
            "WHERE status IN ('running', 'cancel_requested', 'needs_attention') ORDER BY created_at, id")]

    def resolve_stopped(self, job, actor='local-operator'):
        # Only the locked reconciliation worker calls this after checking process absence.
        self._begin()
        try:
            row = self.conn.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
            if (row is None or row['attempt'] != job['attempt']
                    or row['status'] not in {'running', 'cancel_requested', 'needs_attention'}):
                raise ApiError(409, 'conflict', 'attempt changed during reconciliation')
            status = 'cancelled' if row['status'] == 'cancel_requested' or row['reason'] == 'cancel_requested' else 'interrupted'
            now = time.time()
            result = json.dumps({'reason': 'reconciled_stopped', 'stopped': True})
            self.conn.execute('UPDATE jobs SET status=?, finished_at=?, owner=NULL, lease_until=NULL, '
                              'result=?, reason=? WHERE id=?', (status, now, result, status, row['id']))
            self.conn.execute('UPDATE attempts SET status=?, finished=?, result=? WHERE job_id=? AND number=?',
                              (status, now, result, row['id'], row['attempt']))
            self.conn.execute('UPDATE executions SET resolved_at=? WHERE job_id=? AND attempt=?',
                              (now, row['id'], row['attempt']))
            self._event(row['id'], 'reconciled', f'{actor}: owned probe stopped; no automatic retry.', now)
            self.conn.commit()
            return status
        except Exception:
            self.conn.rollback()
            raise

    def publish_worker(self, owner, targets, ttl_s=10, now=None):
        from .probe import local_identity, implementation_id
        now = time.time() if now is None else now
        self.conn.execute('INSERT OR REPLACE INTO probe_workers VALUES (?, ?, ?, ?, ?)',
                          (owner, json.dumps(local_identity(os.getpid())), json.dumps(sorted(set(targets))),
                           implementation_id(), now + ttl_s))
        self.conn.commit()

    def unpublish_worker(self, owner):
        self.conn.execute('DELETE FROM probe_workers WHERE owner=?', (owner,))
        self.conn.commit()

    def available_targets(self, now=None):
        from .probe import local_identity, implementation_id
        now = time.time() if now is None else now
        targets = set()
        for row in self.conn.execute('SELECT * FROM probe_workers WHERE ready_until>?', (now,)):
            try:
                handle = json.loads(row['identity'])
                if row['implementation'] == implementation_id() and local_identity(handle['pid']) == handle:
                    targets.update(json.loads(row['targets']))
            except (OSError, ValueError, KeyError):
                continue
        return sorted(targets)

    def probe_snapshot(self, target):
        row = self.conn.execute('SELECT * FROM probe_snapshots WHERE target=?', (target,)).fetchone()
        if row is None:
            return None
        age = time.time() - row['recorded_at']
        return {'data': json.loads(row['data']), 'job_id': row['job_id'], 'attempt': row['attempt'],
                'recorded_at': iso(row['recorded_at']), 'quality': 'fresh' if 0 <= age <= 60 else 'stale'}


    def prelaunch_proven(self, job):
        row = self.conn.execute('SELECT protocol FROM probe_attempt_receipts WHERE job_id=? AND attempt=?',
                                (job['id'], job['attempt'])).fetchone()
        return bool(row and row[0] == 'minimal-probe-v1')
