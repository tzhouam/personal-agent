"""Daily fan-out scheduler — one deduped `run` per active user (multi-user §12)."""
import pytest

from assistant.config import Settings
from assistant.jobs import JobQueue
from assistant.registry import UserRegistry
from assistant.scheduler import enqueue_daily_runs


@pytest.fixture
def root(tmp_path, monkeypatch):
    """The deployment-root Settings + a registry with two active + one disabled user."""
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    reg = UserRegistry(tmp_path)
    reg.add_user("alice1")
    reg.add_user("bob123")
    reg.add_user("carol1")
    reg.set_status("carol1", "disabled")
    return Settings(_env_file=None), tmp_path


def test_fan_out_enqueues_one_run_per_active_user(root):
    settings, data_dir = root
    enqueued = enqueue_daily_runs(settings, day="2026-07-15")
    assert sorted(enqueued) == ["alice1", "bob123"]     # disabled carol skipped
    q = JobQueue(settings.shared_dir)
    assert q.counts() == {"queued": 2}
    assert (data_dir / "shared" / "jobs.db").exists()   # one queue under the root


def test_fan_out_is_idempotent_per_day(root):
    settings, _ = root
    enqueue_daily_runs(settings, day="2026-07-15")
    again = enqueue_daily_runs(settings, day="2026-07-15")   # same day → no dupes
    assert again == []
    assert JobQueue(settings.shared_dir).counts() == {"queued": 2}
    # a new day enqueues fresh runs
    nxt = enqueue_daily_runs(settings, day="2026-07-16")
    assert sorted(nxt) == ["alice1", "bob123"]


def test_single_user_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "single_user")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert enqueue_daily_runs(Settings(_env_file=None)) == []


def test_weekly_fan_out_full_set(root):
    from assistant.jobs import GLOBAL_UID
    from assistant.scheduler import enqueue_weekly_jobs

    settings, _ = root
    labels = enqueue_weekly_jobs(settings, week="2026-W29")
    assert sorted(labels) == ["alice1:consolidate", "alice1:evolve",
                              "bob123:consolidate", "bob123:evolve",
                              "global:evolve", "global:self_improve"]
    q = JobQueue(settings.shared_dir)
    assert q.counts() == {"queued": 6}
    # the two global jobs carry the sentinel uid
    kinds = {}
    while (job := q.claim()) is not None:
        kinds[job["kind"]] = job["uid"]
        q.mark(job["id"], "done")
    assert kinds["global_evolve"] == GLOBAL_UID
    assert kinds["self_improve"] == GLOBAL_UID
    assert kinds["run_phase"] in ("alice1", "bob123")
    # idempotent within the week; fresh next week
    assert enqueue_weekly_jobs(settings, week="2026-W29") == []
    assert len(enqueue_weekly_jobs(settings, week="2026-W30")) == 6


def test_weekly_single_user_is_a_noop(tmp_path, monkeypatch):
    from assistant.scheduler import enqueue_weekly_jobs

    monkeypatch.setenv("DEPLOYMENT_MODE", "single_user")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert enqueue_weekly_jobs(Settings(_env_file=None)) == []
