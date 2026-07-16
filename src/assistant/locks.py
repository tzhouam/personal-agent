"""Per-user mutation lock — serialize one user's store+git writes across threads
*and* processes.

A `flock` on `DATA_DIR/write.lock`, held around the **whole**
load→validate→modify→write→commit transaction (not just the write — else two
callers both `load()` the same state and lost updates remain). Wrapping at the
operation boundary is the contract. Different users' locks are independent, so
cross-user work stays parallel.

**Reentrant within a process/thread**: `flock` is per open-file-description, so a
second `open()`+`LOCK_EX` on the same file from the same thread would *self-
deadlock*; a thread-local depth counter reuses the held lock for nested store
calls. See doc/DESIGN_MULTI_USER.md §8.
"""

import contextlib
import fcntl
import os
import threading
from pathlib import Path

_local = threading.local()   # per-thread {lockpath: (fd, depth)}


@contextlib.contextmanager
def user_write_lock(settings):
    """Hold the exclusive per-user write lock for a mutation transaction.

    Blocks until acquired (cross-process, cross-thread). Reentrant in the same
    thread — nested `with user_write_lock(...)` reuses the already-held lock
    instead of deadlocking on a fresh fd."""
    path = Path(settings.data_dir) / "write.lock"
    key = str(path)
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = {}
    if key in held:                      # reentrant: already held by this thread
        fd, depth = held[key]
        held[key] = (fd, depth + 1)
        try:
            yield
        finally:
            fd2, d2 = held[key]
            held[key] = (fd2, d2 - 1)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)       # blocks — exclusive across threads+procs
    held[key] = (fd, 1)
    try:
        yield
    finally:
        _, depth = held.pop(key, (fd, 1))
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
