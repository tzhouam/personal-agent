"""Back-compat shim — `worker` moved to `assistant.platform.worker`.

Kept so existing `from .worker import …` / `from assistant.worker import …` call
sites (and `test/test_worker.py`, which imports the underscore helper) keep
working while the platform/agent split migrates; removed once every importer is
repointed. Explicit re-exports (not `import *`) so underscore names survive.
"""

from .platform.worker import (
    WorkerPool,
    CancelToken,
    Cancelled,
    _default_settings_for,
)

__all__ = ["WorkerPool", "CancelToken", "Cancelled", "_default_settings_for"]
