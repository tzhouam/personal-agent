"""Per-user mutation lock — serialize one user's store+git writes across threads
*and* processes.

A `flock` on `DATA_DIR/write.lock`, held around the **whole**
load→validate→modify→write→commit transaction (not just the write — else two
callers both `load()` the same state and lost updates remain). Wrapping at the
operation boundary is the contract. Different users' locks are independent, so
cross-user work stays parallel.

Three entry points share one reentrant core keyed on the lock-file path:

- ``user_write_lock(settings)`` — the chat executor's batch lock (unchanged).
- ``repo_write_lock(repo_dir)`` — for stores living in the profile git repo;
  ``data_dir/profile`` resolves to the SAME ``data_dir/write.lock``, so nesting
  under the batch lock is reentrant, never a deadlock. One shared lock also
  serializes git commits — two stores committing different files in the same
  repo would otherwise race on ``.git/index``.
- ``data_write_lock(data_dir)`` — for stores living directly in the data dir
  (reminders/routines), same lock file again.

``locked_transaction`` decorates a store's mutating method — its complete
load→mutate→save(+git commit) transaction — with the lock the store bound at
construction (``self._lock_file``). Store methods hold the lock only for the
YAML write + git commit (milliseconds), never across LLM or network calls.

**Reentrant within a process/thread**: `flock` is per open-file-description, so a
second `open()`+`LOCK_EX` on the same file from the same thread would *self-
deadlock*; a thread-local depth counter reuses the held lock for nested store
calls. See doc/DESIGN_MULTI_USER.md §8.
"""

import contextlib
import fcntl
import functools
import os
import threading
from pathlib import Path

_local = threading.local()   # per-thread {lockpath: (fd, depth)}


@contextlib.contextmanager
def _path_lock(path: Path):
    """Hold the exclusive flock on lock-file ``path`` for one transaction.

    Blocks until acquired (cross-process, cross-thread). Reentrant in the same
    thread — a nested acquisition of the same path reuses the already-held lock
    instead of deadlocking on a fresh fd."""
    path = Path(path)
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


def user_write_lock(settings):
    """The per-user mutation lock, resolved from ``settings.data_dir`` — hold it
    for a whole mutation transaction (the chat executor wraps each action
    batch in it)."""
    return _path_lock(Path(settings.data_dir) / "write.lock")


def repo_write_lock(repo_dir):
    """The same per-user lock, resolved from a profile-repo store's directory
    (``data_dir/profile`` → ``data_dir/write.lock``)."""
    return _path_lock(Path(repo_dir).parent / "write.lock")


def data_write_lock(data_dir):
    """The same per-user lock, resolved from the data dir itself (stores that
    live directly under it: reminders, routines, task records)."""
    return _path_lock(Path(data_dir) / "write.lock")


def locked_transaction(method):
    """Decorator: hold the store's write lock (``self._lock_file``, bound in
    ``__init__``) for the whole method — its complete load→mutate→save(+commit)
    transaction (DESIGN_MULTI_USER.md §8)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with _path_lock(self._lock_file):
            return method(self, *args, **kwargs)
    return wrapper
