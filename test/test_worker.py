"""In-process worker pool — jobs run under their own uid, cancellation is
cooperative, and a bad job never kills the pool (multi-user §6)."""
import threading
import time

import pytest

from assistant.jobs import JobQueue
from assistant.worker import Cancelled, WorkerPool


@pytest.fixture
def q(tmp_path):
    return JobQueue(tmp_path / "shared")


def _drain(q, pool, want_terminal, timeout=5.0):
    """Spin the pool until `want_terminal` jobs have reached a terminal state."""
    deadline = time.time() + timeout
    terminal = ("done", "failed", "cancelled")
    while time.time() < deadline:
        counts = q.counts()
        if sum(counts.get(s, 0) for s in terminal) >= want_terminal:
            return counts
        time.sleep(0.05)
    return q.counts()


def test_worker_runs_job_under_its_own_uid(q):
    seen = []
    dispatch = {"run": lambda settings, args, token: seen.append((settings, args))}
    q.enqueue("alice1", "run", {"resume": True})
    pool = WorkerPool(q, settings_for=lambda uid: f"settings::{uid}",
                      dispatch=dispatch, poll_interval=0.02).start()
    try:
        _drain(q, pool, 1)
    finally:
        pool.stop()
    assert seen == [("settings::alice1", {"resume": True})]
    assert q.counts() == {"done": 1}


def test_unknown_kind_fails_cleanly(q):
    q.enqueue("alice1", "bogus", {})
    pool = WorkerPool(q, settings_for=lambda uid: uid, dispatch={},
                      poll_interval=0.02).start()
    try:
        _drain(q, pool, 1)
    finally:
        pool.stop()
    assert q.counts() == {"failed": 1}


def test_erroring_job_retries_then_fails_without_killing_pool(q):
    calls = {"n": 0}

    def boom(settings, args, token):
        calls["n"] += 1
        raise RuntimeError("nope")

    q.enqueue("alice1", "run", {})
    pool = WorkerPool(JobQueue(q.dir, max_attempts=2), settings_for=lambda u: u,
                      dispatch={"run": boom, "task": lambda s, a, t: None},
                      poll_interval=0.02).start()
    try:
        _drain(q, pool, 1)
        # a following good job still runs → the pool survived the error
        q.enqueue("alice1", "task", {})
        deadline = time.time() + 5
        while time.time() < deadline and q.counts().get("done", 0) < 1:
            time.sleep(0.05)
    finally:
        pool.stop()
    assert calls["n"] == 2                       # retried up to max_attempts
    assert q.counts().get("failed") == 1
    assert q.counts().get("done") == 1


def test_cooperative_cancellation_yields(q):
    started = threading.Event()
    release = threading.Event()

    def long_job(settings, args, token):
        started.set()
        release.wait(5)
        token.check()                            # checkpoint AFTER work → sees the flag

    jid = q.enqueue("alice1", "run", {})
    pool = WorkerPool(q, settings_for=lambda u: u,
                      dispatch={"run": long_job}, poll_interval=0.02).start()
    try:
        assert started.wait(5)
        q.request_cancel(jid)                    # flag mid-flight
        release.set()
        _drain(q, pool, 1)
    finally:
        pool.stop()
    assert q.get(jid)["state"] == "cancelled"


def test_jobs_run_in_isolated_contexts(q):
    """A ContextVar set by one job (e.g. the tracer installed by `run()`) must
    NOT survive into the next job on the same reused worker thread — that would
    write user B's LLM spans into user A's trace file (§3)."""
    import contextvars

    leak = contextvars.ContextVar("leak", default=None)
    seen = []
    order = threading.Event()

    def polluter(settings, args, token):
        leak.set("user-a-tracer")          # what tracing.init() does
        order.set()

    def observer(settings, args, token):
        seen.append(leak.get())            # must see a CLEAN context

    q.enqueue("alice1", "pollute", {})
    q.enqueue("bob123", "observe", {})
    pool = WorkerPool(q, settings_for=lambda u: u,
                      dispatch={"pollute": polluter, "observe": observer},
                      max_workers=1, poll_interval=0.02).start()   # same thread
    try:
        assert order.wait(5)
        _drain(q, pool, 2)
    finally:
        pool.stop()
    assert seen == [None]                  # no cross-job (cross-tenant) bleed


def test_cancel_before_dispatch_never_runs_body(q):
    ran = []

    def body(settings, args, token):
        ran.append(1)

    jid = q.enqueue("alice1", "run", {})
    q.request_cancel(jid)                         # queued-cancel → already 'cancelled'
    pool = WorkerPool(q, settings_for=lambda u: u,
                      dispatch={"run": body}, poll_interval=0.02).start()
    try:
        time.sleep(0.3)
    finally:
        pool.stop()
    assert ran == []                             # cancelled job was never claimed/run


def test_default_settings_for_global_sentinel(tmp_path, monkeypatch):
    from assistant.config import Settings
    from assistant.jobs import GLOBAL_UID
    from assistant.worker import _default_settings_for

    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    root = _default_settings_for(GLOBAL_UID)
    assert root.data_dir == tmp_path                     # deployment ROOT
    assert not (tmp_path / "users" / "__global__").exists()
    user = _default_settings_for("alice1")
    assert user.data_dir == tmp_path / "users" / "alice1"


def test_global_job_dispatches_with_root_settings(q, tmp_path, monkeypatch):
    from assistant.jobs import GLOBAL_UID

    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seen = []
    q.enqueue(GLOBAL_UID, "global_evolve", {})
    pool = WorkerPool(q, dispatch={"global_evolve":
                                   lambda s, a, t: seen.append(s.data_dir)},
                      poll_interval=0.02).start()        # default settings_for
    try:
        _drain(q, pool, 1)
    finally:
        pool.stop()
    assert seen == [tmp_path]                            # root, not users/<uid>
    assert q.counts() == {"done": 1}
