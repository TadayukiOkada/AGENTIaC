"""Cooperative process-lifetime exclusion between services and offline maintenance."""
import fcntl
import os
from pathlib import Path


class StateLock:
    def __init__(self, state, exclusive=False):
        self.state, self.exclusive, self.fd = Path(state), exclusive, None

    def __enter__(self):
        self.state.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.state.is_symlink() or self.state.stat().st_uid != os.getuid():
            raise ValueError('management state must be an owned directory, not a symlink')
        self.state.chmod(0o700)
        self.fd = os.open(self.state / 'lifecycle.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise ValueError('management state is busy; stop its API and probe worker before maintenance') from None
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
