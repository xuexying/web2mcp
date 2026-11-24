"""Cross-platform advisory file locks (POSIX ``fcntl`` is unavailable on Windows).

Built on the ``filelock`` library. The lock for a path is a sibling ``<path>.lock``
file; every writer in this codebase goes through these helpers, so the convention
holds. ``LockHeld`` is raised by non-blocking acquisition when another process (or
another handle in this process) already holds the lock.
"""

from filelock import FileLock
from filelock import Timeout as LockHeld

__all__ = ["LockHeld", "acquire_nonblocking", "file_lock"]


def file_lock(path):
    """Advisory lock guarding ``path``; the lock file itself is ``<path>.lock``."""
    return FileLock(str(path) + ".lock")


def acquire_nonblocking(path):
    """Acquire the lock for ``path`` without waiting; raise ``LockHeld`` if taken.

    Returns the held lock; the caller must ``release()`` it (or use it as a
    context manager via the returned object's ``release`` in ``finally``).
    """
    lock = file_lock(path)
    lock.acquire(blocking=False)
    return lock
