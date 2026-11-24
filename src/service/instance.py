"""Cross-platform advisory single-instance lock (flock on POSIX, msvcrt on Windows).

The OS releases the lock automatically if the process dies, so a crash never leaves a stale lock.
Bot locks are keyed by a hash of the token so the same token can't run twice, without ever
writing the token itself to disk.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Optional

from .paths import lock_dir


class AlreadyRunning(Exception):
    pass


class InstanceLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    @classmethod
    def for_token(cls, token: str) -> "InstanceLock":
        key = hashlib.sha256(token.encode()).hexdigest()[:16]
        return cls(lock_dir() / f"bot-{key}.lock")

    @classmethod
    def named(cls, name: str) -> "InstanceLock":
        return cls(lock_dir() / f"{name}.lock")

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise AlreadyRunning(str(self.path))
        fh.seek(0); fh.truncate(); fh.write(str(os.getpid())); fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *a):
        self.release()
