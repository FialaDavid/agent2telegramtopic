"""Ported from agent2telegram agent2telegram/compat.py (MIT)."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

from .config import state_dir


class AlreadyRunning(RuntimeError):
    """Raised when another process holds the single-instance lock."""


@contextmanager
def single_instance_lock(path):
    """Hold a non-blocking exclusive lock for the lifetime of this context."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning(f"another instance already holds {path}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def lock_file(bot) -> Path:
    return state_dir() / f"{bot}.lock"
