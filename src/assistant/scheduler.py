"""Back-compat shim — `scheduler` moved to `assistant.platform.scheduler`.

Kept so existing `from .scheduler import …` call sites keep working while the
platform/agent split migrates; removed once every importer is repointed.
"""

from .platform.scheduler import enqueue_daily_runs, enqueue_weekly_jobs

__all__ = ["enqueue_daily_runs", "enqueue_weekly_jobs"]
