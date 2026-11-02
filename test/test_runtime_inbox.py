"""Real durable inbox journeys: restart ambiguity, explicit retry and session FIFO."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

from assistant.platform.runtime.contracts import ChannelEvent
from assistant.platform.runtime.inbox import Inbox


def event(ident="event-1", *, room="room-a", account="account-a"):
    """Create trusted fixture events without transport credentials or network calls."""
    return ChannelEvent(channel="slack", account_id=account, event_id=ident,
                        sender_id="owner", room_id=room, text="do this task",
                        workspace_id="team", thread_id="thread")


def test_inbox_dedup_and_envelope_survive_restart(tmp_path):
    """ACKed inputs remain durable, scoped by account, with private transport data omitted."""
    inbox = Inbox(tmp_path)
    original = replace(event(), metadata={"signed_url": "must-not-persist"})
    assert inbox.put(original)
    assert not Inbox(tmp_path).put(original)
    assert inbox.put(replace(original, account_id="another-account"))
    claimed = Inbox(tmp_path).claim()
    payload = json.loads(claimed["envelope"])
    assert payload["text"] == original.text
    assert payload["metadata"] == {}
    assert payload["owner_id"] == "default"
    assert inbox.path.stat().st_mode & 0o777 == 0o600


def test_inbox_atomic_dedup_and_claims_across_threads(tmp_path):
    """Repeated delivery and competing workers cannot execute one event twice."""
    inbox = Inbox(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(lambda _: inbox.put(event()), range(16)))
    assert accepted.count(True) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: inbox.claim(), range(16)))
    assert sum(item is not None for item in claimed) == 1


def test_inbox_restart_never_replays_running_turns_or_uncertain_sends(tmp_path):
    """Recovery exposes interrupted work and permits only an explicit saved-reply retry."""
    inbox = Inbox(tmp_path)
    active, delivering, queued = event("active"), event("delivery", room="b"), event("queued", room="c")
    for item in (active, delivering, queued):
        assert inbox.put(item)
    assert inbox.claim()["id"] == active.dedup_key
    pending = inbox.claim()
    assert pending["id"] == delivering.dedup_key
    inbox.finish(pending["id"], "ready", reply="already computed answer")
    assert inbox.claim(delivery=True)["id"] == delivering.dedup_key

    restarted = Inbox(tmp_path)
    assert restarted.recover() == 2
    states = {row["id"]: row["state"] for row in restarted.list()}
    assert states == {active.dedup_key: "interrupted", delivering.dedup_key: "unknown",
                      queued.dedup_key: "queued"}
    assert restarted.recover() == 0
    assert restarted.claim(delivery=True) is None
    assert restarted.claim()["id"] == queued.dedup_key
    assert restarted.claim() is None
    assert not restarted.retry_delivery(active.dedup_key)
    assert restarted.retry_delivery(delivering.dedup_key)
    retry = restarted.claim(delivery=True)
    assert retry["id"] == delivering.dedup_key
    assert retry["reply"] == "already computed answer"
    restarted.finish(retry["id"], "sent", receipt={"message_ids": ["receipt-1"]})
    assert restarted.claim(delivery=True) is None
    assert not restarted.retry_delivery(delivering.dedup_key)


def test_ready_reply_blocks_next_turn_in_the_same_session(tmp_path):
    """A second worker cannot compute later context before the prior reply settles."""
    inbox = Inbox(tmp_path)
    first, second = event("first"), event("second")
    assert first.session_key == second.session_key
    assert inbox.put(first) and inbox.put(second)
    assert inbox.claim()["id"] == first.dedup_key
    inbox.finish(first.dedup_key, "ready", reply="first response")
    assert inbox.claim() is None, "queued same-session work overtook a saved ready reply"
    assert inbox.claim(delivery=True)["id"] == first.dedup_key
    assert inbox.claim() is None
    inbox.finish(first.dedup_key, "sent")
    assert inbox.claim()["id"] == second.dedup_key


def test_queued_turn_blocks_a_later_completion_reply_in_the_same_session(tmp_path):
    """Background results do not jump ahead of an earlier accepted user message."""
    inbox = Inbox(tmp_path)
    original = event("question")
    assert inbox.put(original)
    assert inbox.put_reply(original.session_key, "project-1", "finished project")
    assert inbox.claim(delivery=True) is None, "completion jumped ahead of earlier queued turn"
    assert inbox.claim()["id"] == original.dedup_key
    inbox.finish(original.dedup_key, "ready", reply="answer to question")
    assert inbox.claim(delivery=True)["id"] == original.dedup_key
    inbox.finish(original.dedup_key, "sent")
    completion = inbox.claim(delivery=True)
    assert completion["reply"] == "finished project"


def test_inbox_session_fifo_uses_stable_order_when_timestamps_tie(tmp_path, monkeypatch):
    """Clock resolution or a frozen fixture cannot make equal-timestamp messages overtake."""
    from assistant.platform.runtime import inbox as module
    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    inbox = Inbox(tmp_path)
    first, second = event("tie-first"), event("tie-second")
    inbox.put(first)
    inbox.put(second)
    assert inbox.claim()["id"] == first.dedup_key
    inbox.finish(first.dedup_key, "ready", reply="first")
    assert inbox.claim() is None
    delivery = inbox.claim(delivery=True)
    assert delivery["id"] == first.dedup_key
    inbox.finish(delivery["id"], "sent")
    assert inbox.claim()["id"] == second.dedup_key


def test_project_completion_reply_deduplicates_after_restart(tmp_path):
    """Reconciliation can retry safely without redoing project work or duplicating replies."""
    inbox = Inbox(tmp_path)
    original = event()
    inbox.put(original)
    claimed = inbox.claim()
    inbox.finish(claimed["id"], "sent", reply="task accepted")
    assert inbox.put_reply(original.session_key, "project-123", "project result")
    assert Inbox(tmp_path).put_reply(original.session_key, "project-123", "project result")
    assert len(inbox.list()) == 2
    result = inbox.claim(delivery=True)
    envelope = json.loads(result["envelope"])
    assert result["reply"] == "project result"
    assert envelope["thread_id"] == original.thread_id
    assert envelope["room_id"] == original.room_id
    assert not Inbox(tmp_path).put_reply("unknown-session", "project-999", "unroutable")
