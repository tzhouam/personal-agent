"""Back-compat shim — `jobs` moved to `assistant.platform.jobs`.

Kept so existing `from assistant.jobs import …` / `from .jobs import …` call
sites keep working while the platform/agent split migrates. Removed once every
importer is repointed at `assistant.platform.jobs` (see the boundary refactor
plan). Explicit re-exports (not `import *`) so names are stable and discoverable.
"""

from .platform.jobs import GLOBAL_UID, JobQueue, DeliveryLedger

__all__ = ["GLOBAL_UID", "JobQueue", "DeliveryLedger"]
