"""Transactional benchmark identities, leases and immutable attempt history."""
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid


def stable_id(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS cases (
              id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
              owner TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS attempts (
              case_id TEXT NOT NULL, number INTEGER NOT NULL, started REAL NOT NULL,
              finished REAL, status TEXT NOT NULL, result TEXT,
              PRIMARY KEY(case_id,number));
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def register(self, spec):
        key = stable_id(spec)
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO cases(id,spec) VALUES(?,?)', (key, json.dumps(spec, sort_keys=True)))
        return key

    def claim(self, key, retry_failed=False, lease_seconds=3600):
        now, owner = time.time(), uuid.uuid4().hex
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM cases WHERE id=?', (key,)).fetchone()
            if not row:
                raise KeyError(key)
            if row['status'] == 'complete' or (row['status'] == 'failed' and not retry_failed):
                return None
            if row['status'] == 'running' and row['lease_until'] > now:
                return None
            if row['status'] == 'running':
                db.execute("UPDATE attempts SET status='interrupted',finished=? WHERE case_id=? AND number=? AND status='running'", (now,key,row['attempts']))
            number = row['attempts'] + 1
            db.execute("UPDATE cases SET status='running',owner=?,lease_until=?,attempts=? WHERE id=?", (owner, now+lease_seconds, number, key))
            db.execute("INSERT INTO attempts VALUES(?,?,?,NULL,'running',NULL)", (key, number, now))
        return owner, number

    def renew(self, key, owner, number, lease_seconds=3600):
        """Extend the lease of the attempt its owner currently holds, so long work does not
        have to guess its worst case at claim time. True only while that attempt is still
        running and its lease has not expired; every other case - unknown key, wrong owner,
        wrong attempt number, a pending or finished case, an expired lease including the
        deadline itself - returns False and leaves every row untouched, which is the caller's
        signal to stop: someone else owns the case now. The lease only ever grows."""
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)) \
                or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError('lease_seconds must be a finite positive int or float')
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE cases SET lease_until=MAX(lease_until,?) WHERE id=? AND owner=?"
                                 " AND attempts=? AND status='running' AND lease_until>?",
                                 (now + lease_seconds, key, owner, number, now))
            return changed.rowcount == 1

    def finish(self, key, owner, number, passed, result):
        status = 'complete' if passed else 'failed'
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT owner,status,attempts FROM cases WHERE id=?', (key,)).fetchone()
            if not row or row['owner'] != owner or row['status'] != 'running' or row['attempts'] != number:
                raise RuntimeError('lease owner changed; result preserved only in artifact')
            db.execute('UPDATE attempts SET finished=?,status=?,result=? WHERE case_id=? AND number=?', (time.time(),status,json.dumps(result),key,number))
            db.execute('UPDATE cases SET status=?,owner=NULL,lease_until=NULL WHERE id=?', (status,key))

    def summary(self):
        with closing(self.connect()) as db, db:
            return [dict(row) for row in db.execute('SELECT id,spec,status,attempts FROM cases ORDER BY rowid')]
