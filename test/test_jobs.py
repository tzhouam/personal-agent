"""Durable job queue + delivery ledger — durability, recovery, dedupe, per-user
fairness, cooperative cancellation (multi-user §6)."""
import pytest

from assistant.platform.jobs import DeliveryLedger, JobQueue


@pytest.fixture
def q(tmp_path):
    return JobQueue(tmp_path / "shared")


def test_enqueue_and_claim_roundtrip(q):
    jid = q.enqueue("alice1", "run", {"resume": True})
    assert jid is not None
    job = q.claim()
    assert job["id"] == jid and job["uid"] == "alice1" and job["kind"] == "run"
    assert job["args"] == {"resume": True} and job["state"] == "running"
    assert job["attempts"] == 1
    assert q.claim() is None            # nothing else queued
    q.mark(jid, "done")
    assert q.counts() == {"done": 1}


def test_enqueue_dedupe_blocks_duplicate_in_any_state(q):
    a = q.enqueue("alice1", "run", {}, dedupe_key="alice1:run:2026-07-15")
    b = q.enqueue("alice1", "run", {}, dedupe_key="alice1:run:2026-07-15")
    assert a is not None and b is None          # duplicate enqueue is idempotent
    # a COMPLETED job still blocks its period key — the poll loop keeps ticking
    # past the daily gate, and re-enqueueing a finished run looped the pipeline
    # all day (live incident 2026-07-16)
    q.claim()
    q.mark(a, "done")
    assert q.enqueue("alice1", "run", {}, dedupe_key="alice1:run:2026-07-15") is None
    # a different period (or no key at all — manual trigger) still enqueues
    assert q.enqueue("alice1", "run", {}, dedupe_key="alice1:run:2026-07-16") is not None
    assert q.enqueue("alice1", "run", {}) is not None


def test_recover_requeues_orphaned_running_jobs(tmp_path):
    q = JobQueue(tmp_path / "shared")
    jid = q.enqueue("alice1", "run", {})
    q.claim()                                    # now 'running'
    assert q.counts() == {"running": 1}
    # simulate a fresh process over the same db (worker died mid-job)
    q2 = JobQueue(tmp_path / "shared")
    assert q2.recover() == 1
    assert q2.counts() == {"queued": 1}
    assert q2.claim()["id"] == jid               # picked back up


def test_durable_across_reopen(tmp_path):
    JobQueue(tmp_path / "shared").enqueue("bob123", "task", {"request": "x"})
    # a brand-new instance sees the persisted job
    assert JobQueue(tmp_path / "shared").counts() == {"queued": 1}


def test_per_user_fairness_one_pipeline_per_uid(q):
    q.enqueue("alice1", "run", {})
    q.enqueue("alice1", "run", {})               # second run for same user
    q.enqueue("bob123", "run", {})
    first = q.claim()
    assert first["uid"] == "alice1"
    # alice already has a running pipeline → next claim skips her, serves bob
    second = q.claim()
    assert second["uid"] == "bob123"
    # nothing else runnable until alice's finishes
    assert q.claim() is None
    q.mark(first["id"], "done")
    assert q.claim()["uid"] == "alice1"          # now her queued second runs


def test_fail_or_retry_then_give_up(tmp_path):
    q = JobQueue(tmp_path / "shared", max_attempts=2)
    jid = q.enqueue("alice1", "task", {})
    q.claim()                                    # attempt 1
    assert q.fail_or_retry(jid) == "queued"      # retry available
    q.claim()                                    # attempt 2
    assert q.fail_or_retry(jid) == "failed"      # exhausted
    assert q.counts() == {"failed": 1}


def test_cancel_queued_job_immediately(q):
    jid = q.enqueue("alice1", "run", {})
    q.request_cancel(jid)
    assert q.get(jid)["state"] == "cancelled"
    assert q.claim() is None                     # never handed to a worker


def test_cancel_running_job_is_cooperative(q):
    jid = q.enqueue("alice1", "run", {})
    q.claim()                                    # running
    q.request_cancel(jid)
    # still 'running' on the row — only the flag is set; the worker must yield
    assert q.get(jid)["state"] == "running"
    assert q.is_cancelled(jid) is True


def test_cancel_user_flags_all_active_jobs(q):
    a = q.enqueue("alice1", "run", {})
    b = q.enqueue("alice1", "task", {})
    q.enqueue("bob123", "run", {})
    q.claim()                                    # alice's run → running
    flagged = q.cancel_user("alice1")
    assert flagged == 2                          # both of alice's, not bob's
    assert q.is_cancelled(a) and q.is_cancelled(b)
    # bob untouched
    assert q.counts().get("cancelled") == 1      # alice's still-queued task


def test_claim_rotates_across_users_for_all_kinds(q):
    """Fairness is not pipeline-only: a user with a deep `task` queue must not
    monopolize the workers — a user with an idle slot is served first."""
    q.enqueue("alice1", "task", {"request": "a1"})
    q.enqueue("alice1", "task", {"request": "a2"})
    q.enqueue("bob123", "task", {"request": "b1"})
    first = q.claim()
    assert first["uid"] == "alice1"              # all idle → oldest wins
    second = q.claim()
    assert second["uid"] == "bob123"             # alice busy → bob's turn
    third = q.claim()
    assert third["uid"] == "alice1" and third["args"] == {"request": "a2"}


# ── delivery ledger ──────────────────────────────────────────────────
def test_delivery_ledger_is_idempotent(tmp_path):
    led = DeliveryLedger(tmp_path / "user")
    assert led.was_delivered("digest", "2026-07-15") is False
    assert led.mark_delivered("digest", "2026-07-15") is True    # first claim wins
    assert led.was_delivered("digest", "2026-07-15") is True
    assert led.mark_delivered("digest", "2026-07-15") is False   # replay → skip send
    # a different day is independent
    assert led.mark_delivered("digest", "2026-07-16") is True


def test_delivery_ledger_survives_reopen(tmp_path):
    DeliveryLedger(tmp_path / "user").mark_delivered("digest", "2026-07-15")
    assert DeliveryLedger(tmp_path / "user").was_delivered("digest", "2026-07-15")


def test_delivery_ledger_unmark_releases_claim(tmp_path):
    # claim-before-send: a FAILED send unmarks so the retry/resume can re-claim
    led = DeliveryLedger(tmp_path / "user")
    assert led.mark_delivered("digest", "2026-07-15") is True
    led.unmark("digest", "2026-07-15")
    assert led.was_delivered("digest", "2026-07-15") is False
    assert led.mark_delivered("digest", "2026-07-15") is True   # retry claims again


def test_singleton_blocked_job_stays_queued_until_run_finishes(q):
    """A weekly run_phase queued while the same uid's daily run executes is
    deferred (skipped by claim), NOT lost — and claimable once the run ends."""
    run = q.enqueue("alice1", "run", {})
    q.claim()                                            # daily run → running
    phase = q.enqueue("alice1", "run_phase", {"phase": "consolidate"})
    assert q.claim() is None                             # deferred, not claimed
    assert q.get(phase)["state"] == "queued"             # still queued
    q.mark(run, "done")
    nxt = q.claim()
    assert nxt["id"] == phase and nxt["args"] == {"phase": "consolidate"}
