"""One lock for every operation that changes what control or a worker serves (finding F05).

`claim`, `release`, `reclaim`, `route set`, `ops deploy` and the recovery timer used to run without excluding each
other: a deploy could restart the gateway while a claim rewrote a worker's front, or the recovery timer could
reclaim a host an operator was releasing. They now take one `flock` on `artifacts/locks/mutation.lock`.

A flock, not a pid file, because the kernel drops it when the holder dies: a crashed holder leaves stale JSON in the
file but no lock, and the next `hold` succeeds. A child started by a holder (`claim --route` runs `route.py set`)
sees `AGENTIAC_MUTATION_HELD` in its environment and does not take the lock again. `ops deploy` replaces its
process with ansible, so `hold_for_exec` hands the lock over on an inherited descriptor; the journal then has a
`start` line and no `end` line, because no process is left to write one.

The journal records operation and host/target names only, never arguments that could carry a secret.

The lock, the journal and the claim records live under the checkout the timers execute, which is also where the
tests run. `state_root` lets a test run put them elsewhere (D2.3); nothing else may set its variable.
"""
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import time

LOCK = 'artifacts/locks/mutation.lock'
JOURNAL = 'artifacts/locks/journal.jsonl'
HELD = 'AGENTIAC_MUTATION_HELD'
STATE_ROOT = 'AGENTIAC_STATE_ROOT'


class Busy(Exception):
    """Another mutation holds the lock; .holder is the dict it wrote, or {}."""

    def __init__(self, holder):
        self.holder = holder
        super().__init__(f"another agentiac operation holds the lock: {holder.get('operation', 'unknown')} "
                         f"(pid {holder.get('pid', '?')}, since {holder.get('started', '?')})")


def actor(env=os.environ):
    """Who asked for the mutation. Nothing sets the variable yet; the field exists so records need no migration later."""
    return env.get('AGENTIAC_ACTOR') or 'operator'


def state_root(root, env=os.environ):
    """Where `artifacts/locks` and `artifacts/claims` live: ROOT, unless a test run redirects them.

    A test seam only (`make check` sets the variable to a temporary directory). A unit, a timer or an operator
    shell that set it would lock a different file and exclude nobody.
    """
    return Path(env.get(STATE_ROOT) or root)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _journal(root, record):
    with open(Path(root) / JOURNAL, 'a') as journal:
        journal.write(json.dumps(record) + '\n')


def _holder(fd):
    try:
        holder = json.loads(os.pread(fd, 65536, 0) or b'{}')
    except (OSError, ValueError):
        return {}
    return holder if isinstance(holder, dict) else {}


def _acquire(root, operation, wait_s, env):
    """Take the flock under ROOT, write who holds it and the journal's start line; returns the open descriptor."""
    path = root / LOCK
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy(_holder(fd)) from None
                time.sleep(0.5)
        who = actor(env)
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps({'pid': os.getpid(), 'operation': operation, 'started': _now(), 'actor': who}).encode(), 0)
        _journal(root, {'time': _now(), 'operation': operation, 'event': 'start', 'pid': os.getpid(), 'actor': who})
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def hold(root, operation, wait_s=0.0, env=os.environ):
    """Hold the mutation lock for OPERATION; raise Busy after WAIT_S seconds if another process holds it."""
    if env.get(HELD):
        yield  # a parent process holds the lock
        return
    root = state_root(root, env)
    fd = _acquire(root, operation, wait_s, env)
    try:
        os.environ[HELD] = str(os.getpid())
        started, ok = time.monotonic(), False
        try:
            yield
            ok = True
        finally:
            _journal(root, {'time': _now(), 'operation': operation, 'event': 'end', 'ok': ok,
                            'seconds': round(time.monotonic() - started, 3)})
            os.environ.pop(HELD, None)
            os.ftruncate(fd, 0)
    finally:
        os.close(fd)  # releases the flock; the file itself is never deleted


def hold_for_exec(root, operation):
    """Take the lock for a process this one is about to exec into; returns {HELD: pid} to add to its environment.

    The descriptor is made inheritable, so the flock lives until the exec'd program exits. Raises Busy.
    No-op (returns {}) when a parent already holds the lock."""
    if os.environ.get(HELD):
        return {}
    fd = _acquire(state_root(root), operation, 0.0, os.environ)
    os.set_inheritable(fd, True)  # deliberately never closed: the kernel releases the flock when the program exits
    return {HELD: str(os.getpid())}


@contextlib.contextmanager
def hold_or_exit(root, operation, wait_s=0.0):
    """`hold`, turning Busy into the SystemExit an operator reads."""
    try:
        with hold(root, operation, wait_s):
            yield
    except Busy as error:
        raise SystemExit(f'{error}; wait for it to finish, then run this command again') from None
