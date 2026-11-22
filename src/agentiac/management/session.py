"""Explicitly initialized owner credentials and short-lived server-side sessions."""
import datetime
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from contextlib import closing
from urllib.parse import quote

from .errors import ApiError

SESSION_TTL_S = 30 * 60
LOGIN_WINDOW_S = 10 * 60
LOGIN_LIMIT = 5


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(value):
    return value.isoformat()


def _derive(password, salt):
    return hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt),
                          n=16384, r=8, p=1).hex()


class SessionStore:
    def __init__(self, path, create=True):
        """`create=False` opens a store whose schema this process already prepared.

        It runs no mkdir/chmod/DDL, and a missing file is an error instead of a new empty store.
        """
        self.path = path
        self.create = create
        if not create:
            if not self.path.exists():
                raise ApiError(503, 'state_missing', 'session store is missing; restore it offline')
            return
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        # Create private files without changing the process-wide umask in API threads.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with closing(self._connect()) as conn, conn:
            conn.execute('CREATE TABLE IF NOT EXISTS credentials (actor TEXT PRIMARY KEY, password_hash TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, actor TEXT NOT NULL, '
                         'permission TEXT NOT NULL, csrf_token TEXT NOT NULL, created_at TEXT NOT NULL, '
                         'expires_at TEXT NOT NULL, ip TEXT)')
            conn.execute('CREATE TABLE IF NOT EXISTS attempts (actor TEXT NOT NULL, at TEXT NOT NULL)')

    def _connect(self):
        if self.create:
            conn = sqlite3.connect(self.path, timeout=5)
        else:
            try:
                conn = sqlite3.connect(f'file:{quote(str(self.path))}?mode=rw', uri=True, timeout=5)
            except sqlite3.OperationalError:
                raise ApiError(503, 'state_missing', 'session store is missing; restore it offline') from None
        conn.execute('PRAGMA trusted_schema=OFF')
        return conn

    def initialized(self):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT password_hash FROM credentials WHERE actor='owner'").fetchone()
        # Legacy unsalted prototype credentials must be explicitly replaced offline.
        return bool(row and re.fullmatch(r'scrypt\$[0-9a-f]{32}\$[0-9a-f]{128}', row[0]))

    def initialize(self, password):
        if not 12 <= len(password) <= 256:
            raise ValueError('owner password must be 12..256 characters')
        salt = secrets.token_hex(16)
        digest = f'scrypt${salt}${_derive(password, salt)}'
        with closing(self._connect()) as conn, conn:
            try:
                conn.execute('INSERT INTO credentials VALUES (?, ?)', ('owner', digest))
            except sqlite3.IntegrityError:
                raise ValueError('owner credential already exists; initialization cannot overwrite it') from None

    def login(self, password, ip='127.0.0.1'):
        actor = 'owner'
        created = _now()
        with closing(self._connect()) as conn:
            # Serialize the rate-limit check and reserve an attempt before the slow password check,
            # so concurrent guesses are counted. A successful login clears the owner's attempts.
            conn.execute('BEGIN IMMEDIATE')
            cutoff = _iso(created - datetime.timedelta(seconds=LOGIN_WINDOW_S))
            conn.execute('DELETE FROM attempts WHERE at < ?', (cutoff,))
            conn.execute('DELETE FROM sessions WHERE expires_at <= ?', (_iso(created),))
            count = conn.execute('SELECT COUNT(*) FROM attempts WHERE actor=?', (actor,)).fetchone()[0]
            if count >= LOGIN_LIMIT:
                conn.rollback()
                raise ApiError(429, 'rate_limit', 'too many login attempts in the window', retryable=True)
            row = conn.execute('SELECT password_hash FROM credentials WHERE actor=?', (actor,)).fetchone()
            if row is None or not row[0].startswith('scrypt$'):
                conn.rollback()
                raise ApiError(503, 'owner_uninitialized', 'initialize owner credentials with the local CLI')
            conn.execute('INSERT INTO attempts VALUES (?, ?)', (actor, _iso(created)))
            conn.commit()
            try:
                _, salt, expected = row[0].split('$')
                valid = hmac.compare_digest(_derive(password, salt), expected)
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise ApiError(401, 'bad_credential', 'credential does not match the initialized operator')
            expires = created + datetime.timedelta(seconds=SESSION_TTL_S)
            session_id = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            conn.execute('DELETE FROM attempts WHERE actor=?', (actor,))
            conn.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (session_id, actor, 'admin', csrf, _iso(created), _iso(expires), ip))
            conn.commit()
        return {'session_id': session_id, 'actor': actor, 'permission': 'admin', 'csrf_token': csrf,
                'created_at': _iso(created), 'expires_at': _iso(expires)}

    def read(self, session_id):
        with closing(self._connect()) as conn:
            row = conn.execute('SELECT session_id, actor, permission, csrf_token, created_at, expires_at '
                               'FROM sessions WHERE session_id=?', (session_id,)).fetchone()
        if row is None or _now() >= datetime.datetime.fromisoformat(row[5]):
            return None
        return dict(zip(('session_id', 'actor', 'permission', 'csrf_token', 'created_at', 'expires_at'), row))

    def delete(self, session_id):
        with closing(self._connect()) as conn, conn:
            conn.execute('DELETE FROM sessions WHERE session_id=?', (session_id,))


    def change_password(self, new_password, old_password=None, recover=False):
        if not 12 <= len(new_password) <= 256:
            raise ValueError('owner password must be 12..256 characters')
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT password_hash FROM credentials WHERE actor='owner'").fetchone()
            if row is None:
                raise ValueError('owner is not initialized')
            if not recover:
                try:
                    _, salt, expected = row[0].split('$')
                    valid = hmac.compare_digest(_derive(old_password or '', salt), expected)
                except (ValueError, TypeError):
                    valid = False
                if not valid:
                    raise ValueError('old owner credential does not match')
            salt = secrets.token_hex(16)
            digest = f'scrypt${salt}${_derive(new_password, salt)}'
            conn.execute("UPDATE credentials SET password_hash=? WHERE actor='owner'", (digest,))
            conn.execute('DELETE FROM sessions')
            conn.execute('DELETE FROM attempts')
