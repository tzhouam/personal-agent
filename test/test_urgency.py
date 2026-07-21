"""The todo urgency metric (src/assistant/urgency.py): Taskwarrior-style
polynomial — committed items age up on a longer 30→45-day leash, speculative
items decay out over 14→30 days."""

from datetime import date

from assistant.todo_store import TodoStore
from assistant.urgency import _due_ramp, going_stale, staleness, urgency

TODAY = date(2026, 7, 9)


def _todo(**over):
    base = {"title": "x", "source": "manual", "created": TODAY.isoformat(),
            "status": "open"}
    base.update(over)
    return base


def test_due_ramp_matches_taskwarrior_shape():
    assert _due_ramp(None, TODAY) == 0.0
    assert _due_ramp(date(2026, 8, 30), TODAY) == 0.2          # far out: floor
    assert abs(_due_ramp(TODAY, TODAY) - (0.2 + 0.8 * 14 / 21)) < 1e-9  # ≈0.733 at due
    assert _due_ramp(date(2026, 7, 1), TODAY) == 1.0            # 8d overdue: saturated
    assert _due_ramp(date(2026, 7, 3), TODAY) < 1.0             # 6d overdue: still ramping


def test_reference_scores():
    # red review-request due today ≈ 23 (design reference point)
    hot = _todo(priority="red", source="github", action="review_requested",
                due=TODAY.isoformat())
    assert 21 <= urgency(hot, TODAY) <= 25
    # fresh yellow manual note ≈ 3.5
    note = _todo()
    assert 3 <= urgency(note, TODAY) <= 4
    # day-25 untouched manual note ≈ 1.2 and about to expire
    old = _todo(created="2026-06-14")
    assert 0.5 <= urgency(old, TODAY) <= 2
    assert 0 < staleness(old, TODAY) < 0.5
    # ordering: hot > overdue yellow > fresh note > fading note
    overdue = _todo(source="github", due="2026-07-05", created="2026-06-20")
    assert urgency(hot, TODAY) > urgency(overdue, TODAY) \
        > urgency(note, TODAY) > urgency(old, TODAY)


def test_freshly_overdue_gets_flat_boost():
    due_today = _todo(source="github", due="2026-07-09")
    overdue_3d = _todo(source="github", due="2026-07-06")
    assert urgency(overdue_3d, TODAY) > urgency(due_today, TODAY) + 1.5


def test_staleness_exemptions_and_decay():
    # committed items get the longer 30→45 window — full at 25d, gone when ancient
    assert staleness(_todo(priority="red", created="2026-06-14"), TODAY) == 1.0   # 25d
    assert staleness(_todo(priority="red", created="2026-01-01"), TODAY) == 0.0
    assert staleness(_todo(action="review_requested", created="2026-01-01"), TODAY) == 0.0
    assert staleness(_todo(source="resume", created="2026-01-01"), TODAY) == 0.0
    mid = staleness(_todo(priority="red", created="2026-06-01"), TODAY)           # 38d
    assert 0.4 < mid < 0.55
    # future/recent due: alive; a month past due: dead — committed or not
    assert staleness(_todo(due="2026-08-01", created="2026-01-01"), TODAY) == 1.0
    assert staleness(_todo(due="2026-05-01", created="2026-01-01"), TODAY) == 0.0
    assert staleness(_todo(priority="red", due="2026-05-01", created="2026-01-01"), TODAY) == 0.0
    # undated yellow: full until day 14, gone at day 30
    assert staleness(_todo(created="2026-06-25"), TODAY) == 1.0          # 14d
    assert staleness(_todo(created="2026-06-17"), TODAY) == 0.5          # 22d
    assert staleness(_todo(created="2026-06-09"), TODAY) == 0.0          # 30d


def test_going_stale_window():
    assert not going_stale(_todo(created="2026-06-25"), TODAY)           # 14d: fine
    assert going_stale(_todo(created="2026-06-17"), TODAY)               # 22d: warn
    assert not going_stale(_todo(created="2026-06-01"), TODAY)           # 38d: dead, not warning
    # committed items warn later (35d) and die at 45
    assert not going_stale(_todo(priority="red", created="2026-06-17"), TODAY)   # 22d: fine
    assert going_stale(_todo(priority="red", created="2026-06-01"), TODAY)       # 38d: warn
    assert not going_stale(_todo(priority="red", created="2026-05-20"), TODAY)   # 50d: dead


def test_expire_stale_committed_items_get_longer_leash(tmp_path):
    store = TodoStore(tmp_path)
    store.upsert("k-red", title="Red review ask", source="github", priority="red")
    store.upsert("k-block", title="Waiting on owner", source="github",
                 action="review_requested")
    store.upsert("k-note", title="Yellow note", source="manual")
    data = store.load()
    for item in data["items"]:
        item["created"] = "2026-06-01"  # 38 days before the first cutoff
    store._save(data, "age")

    # day 38: the speculative note is dead, committed items are still fading
    expired = store.expire_stale(today=date(2026, 7, 9))
    assert [t["title"] for t in expired] == ["Yellow note"]
    assert {t["title"] for t in store.open_items()} == {"Red review ask", "Waiting on owner"}

    # day 49: even committed review asks are auto-cleared as outdated
    expired = store.expire_stale(today=date(2026, 7, 20))
    assert {t["title"] for t in expired} == {"Red review ask", "Waiting on owner"}
    assert store.open_items() == []