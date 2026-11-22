"""Snapshot cache and keyset cursors for the read-only resource lists.

A list or detail GET reads a per-process snapshot of one resource kind. A snapshot is rebuilt
from the declaration files once it is older than `TTL_S`; concurrent requests wait for the same
rebuild instead of each reading the files. A failed rebuild keeps serving the last good
snapshot, marked stale with the error class, and is retried after another TTL; with no earlier
snapshot the request fails as unavailable. Builders only read files, as the read layer does.

Rows are kept sorted by their unique ID. A cursor is an opaque, versioned token holding the
resource kind and the last ID returned, so a page continues after that ID even when the snapshot
was rebuilt in between: removed rows are skipped and rows added after the cursor appear.
"""
import base64
import binascii
import bisect
import datetime
import json
import logging
import threading
import time
from dataclasses import dataclass, replace

TTL_S = 30
MAX_LIMIT = 200
MAX_CURSOR = 1024
log = logging.getLogger(__name__)


class SnapshotUnavailable(Exception):
    """The first build of a resource kind failed; there is no earlier snapshot to serve."""


@dataclass(frozen=True)
class Snapshot:
    kind: str
    rows: tuple
    ids: tuple
    extra: dict
    generated_at: str
    built: float
    checked: float
    error: str | None = None

    def by_id(self, row_id):
        index = bisect.bisect_left(self.ids, row_id)
        return self.rows[index] if index < len(self.ids) and self.ids[index] == row_id else None


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class SnapshotCache:
    def __init__(self, ttl_s=TTL_S, clock=time.monotonic):
        self.ttl_s = ttl_s
        self._clock = clock
        self._entries = {}
        self._locks = {}
        self._guard = threading.Lock()

    def clear(self):
        with self._guard:
            self._entries.clear()

    def _due(self, entry):
        return entry is None or self._clock() - entry.checked >= self.ttl_s

    def get(self, kind, scope, build, row_id):
        """The snapshot of `kind` for `scope` (the declaration root). `build()` returns (rows, extra)."""
        slot = (kind, scope)
        with self._guard:
            lock = self._locks.setdefault(slot, threading.Lock())
        entry = self._entries.get(slot)
        if not self._due(entry):
            return entry
        with lock:
            entry = self._entries.get(slot)
            if not self._due(entry):
                return entry
            now = self._clock()
            try:
                rows, extra = build()
                ordered = sorted(rows, key=row_id)
                ids = tuple(row_id(row) for row in ordered)
                if len(set(ids)) != len(ids):
                    raise ValueError(f'{kind} rows have duplicate IDs')
                entry = Snapshot(kind=kind, rows=tuple(ordered), ids=ids, extra=extra,
                                 generated_at=_now_iso(), built=now, checked=now)
            except Exception as error:
                log.exception('%s snapshot rebuild failed', kind)
                if entry is None:
                    raise SnapshotUnavailable(kind) from error
                entry = replace(entry, checked=now, error=type(error).__name__)
            with self._guard:
                self._entries[slot] = entry
            return entry

    def describe(self, snapshot):
        return {'generated_at': snapshot.generated_at, 'age_s': round(self._clock() - snapshot.built, 1),
                'ttl_s': self.ttl_s, 'stale': snapshot.error is not None, 'error': snapshot.error}

    def freshness(self, scope):
        """One line per kind already built for `scope`, without building anything."""
        with self._guard:
            entries = {kind: entry for (kind, entry_scope), entry in self._entries.items() if entry_scope == scope}
        out = {}
        for kind, entry in sorted(entries.items()):
            age = round(self._clock() - entry.built)
            out[kind] = (f'stale snapshot {age}s old (rebuild failed: {entry.error})' if entry.error
                         else f'snapshot {age}s old, refreshed after {self.ttl_s}s')
        return out


def encode_cursor(kind, after):
    raw = json.dumps({'v': 1, 'kind': kind, 'after': after}, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def decode_cursor(kind, cursor):
    if len(cursor) > MAX_CURSOR:
        raise ValueError('cursor is too long')
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ValueError('cursor is not valid') from None
    if not isinstance(data, dict) or data.get('v') != 1 or not isinstance(data.get('after'), str):
        raise ValueError('cursor is not valid')
    if data.get('kind') != kind:
        raise ValueError('cursor belongs to another resource list')
    return data['after']


def page(snapshot, limit=50, cursor=None):
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f'limit must be 1..{MAX_LIMIT}')
    start = 0 if cursor in (None, '') else bisect.bisect_right(snapshot.ids, decode_cursor(snapshot.kind, cursor))
    end = min(start + limit, len(snapshot.rows))
    next_cursor = encode_cursor(snapshot.kind, snapshot.ids[end - 1]) if end < len(snapshot.rows) else None
    return list(snapshot.rows[start:end]), next_cursor
