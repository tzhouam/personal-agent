"""The job-dispatch contract — the seam between the platform runtime and agent work.

`WorkerPool` (platform) drains the durable queue and looks up a handler by job
`kind`; the handlers themselves live in `agent.dispatch` and do the per-owner
work. This module holds only the *types* both sides agree on, so the platform
never imports agent code — dependency inversion at the runtime boundary.
"""

from typing import Callable

# A handler runs one job in-process: (settings, args, cancel_token) -> None.
# `settings` is the per-user Settings the job runs under; `token` is a
# CancelToken whose `.check()` raises to yield at a cooperative checkpoint.
JobHandler = Callable[[object, dict, object], None]

# kind -> handler. `agent.dispatch.build_dispatch()` produces one of these; the
# composition root passes it into `WorkerPool`.
Dispatch = dict[str, JobHandler]
