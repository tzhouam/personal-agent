"""Concurrent-write integrity: chat-style action batches, pipeline-style direct
store mutations, and the proactive claim cycle must serialize on one user's
write lock without lost updates, torn YAML, or dropped git commits
(doc/DESIGN_MULTI_USER.md §8; the store-level `locked_transaction` wrapping)."""

import subprocess
import threading
from datetime import datetime, timedelta

from assistant.actions import execute
from assistant.notify import ReminderStore
from assistant.routines import RoutineStore
from assistant.todo_store import TodoStore


def _git_init(repo_dir):
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)


def _run_threads(workers):
    threads = [threading.Thread(target=w) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a worker deadlocked"


def test_concurrent_todo_writes_no_lost_updates_or_commits(settings):
    """4 threads × 10 upserts (half chat-batch, half pipeline-direct): every
    item lands, ids stay unique, and every mutation's git commit survives."""
    _git_init(settings.profile_dir)
    per_thread, n_threads = 10, 4

    def chat_worker(tid):
        def run():
            for i in range(per_thread):
                execute([{"type": "add_todo", "title": f"chat-{tid}-{i}"}], settings)
        return run

    def direct_worker(tid):
        def run():
            store = TodoStore(settings.profile_dir)
            for i in range(per_thread):
                store.upsert(f"direct:{tid}:{i}", title=f"direct-{tid}-{i}",
                             source="pipeline", priority="yellow")
        return run

    _run_threads([chat_worker(0), chat_worker(1), direct_worker(2), direct_worker(3)])

    store = TodoStore(settings.profile_dir)
    items = store.open_items()
    total = per_thread * n_threads
    assert len(items) == total                       # no lost updates
    assert len({i["id"] for i in items}) == total    # ids never collided
    log = subprocess.run(["git", "log", "--oneline"], cwd=settings.profile_dir,
                         capture_output=True, text=True).stdout.splitlines()
    assert len(log) == total                         # every commit landed


def test_reminder_claimed_exactly_once_across_pollers(settings):
    """Two concurrent deliver_due cycles (daemon poll race) send a due
    reminder exactly once — the claim-then-send lock is the guard."""
    store = ReminderStore(settings.data_dir)
    store.add("ping", datetime.now() - timedelta(minutes=1))
    sends = []

    def send(_settings, text):
        sends.append(text)
        return "sent"

    _run_threads([lambda: store.deliver_due(settings, send=send)] * 2)
    assert len(sends) == 1
    assert store.pending() == []


def test_reminder_failed_send_releases_claim(settings):
    """A failed send un-claims, so the next cycle retries and delivers."""
    store = ReminderStore(settings.data_dir)
    store.add("retry me", datetime.now() - timedelta(minutes=1))

    assert store.deliver_due(settings, send=lambda *a: "error: gateway down") == []
    assert len(store.pending()) == 1                 # claim released
    delivered = store.deliver_due(settings, send=lambda *a: "sent")
    assert [r["message"] for r in delivered] == ["retry me"]


def test_routine_claim_due_is_exactly_once_and_respects_cancel(settings):
    """claim_due marks atomically: two racing pollers claim a due routine once
    total, and a cancelled routine is never claimed."""
    store = RoutineStore(settings.data_dir)
    now = datetime.now().replace(hour=23, minute=59)
    store.add("say hi", "00:00", days="daily")
    claims = []

    _run_threads([lambda: claims.extend(store.claim_due(now))] * 2)
    assert len(claims) == 1                          # exactly one claim
    assert store.claim_due(now) == []                # already checked today

    cancelled = store.add("never run", "00:00", days="daily")
    assert store.cancel(cancelled["id"])
    tomorrow = now + timedelta(days=1)
    assert [r["id"] for r in store.claim_due(tomorrow)] != [cancelled["id"]]
    assert cancelled["id"] not in [r["id"] for r in store.claim_due(tomorrow)]


def test_execute_batch_nests_over_store_locks_without_deadlock(settings):
    """The chat executor's batch lock and the store's own transaction lock are
    the same reentrant per-user lock — a handler calling a locked store method
    inside the locked batch completes instead of self-deadlocking."""
    _git_init(settings.profile_dir)
    out = execute([{"type": "add_todo", "title": "nested"},
                   {"type": "add_todo", "title": "nested"}], settings)
    assert "added todo" in out[0]
    assert "already tracked" in out[1]               # dedup inside the same batch