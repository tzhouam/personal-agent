from datetime import datetime

from assistant.agent.actions import run_action
from assistant.platform.notify import ReminderStore, parse_when

NOW = datetime(2026, 7, 9, 22, 0)


def test_parse_when_forms():
    assert parse_when("+30m", NOW) == datetime(2026, 7, 9, 22, 30)
    assert parse_when("+2h", NOW) == datetime(2026, 7, 10, 0, 0)
    assert parse_when("1d", NOW) == datetime(2026, 7, 10, 22, 0)
    assert parse_when("23:15", NOW) == datetime(2026, 7, 9, 23, 15)
    assert parse_when("08:00", NOW) == datetime(2026, 7, 10, 8, 0)   # past → tomorrow
    assert parse_when("2026-08-01 09:30", NOW) == datetime(2026, 8, 1, 9, 30)
    assert parse_when("whenever", NOW) is None


def test_reminder_store_lifecycle(settings):
    store = ReminderStore(settings.data_dir)
    r1 = store.add("ping Gaohan", datetime(2026, 7, 9, 21, 0))   # due
    r2 = store.add("water plants", datetime(2026, 7, 20, 9, 0))  # future
    assert [r["id"] for r in store.pending()] == ["m1", "m2"]

    sent = []
    delivered = store.deliver_due(settings, now=NOW,
                                  send=lambda s, text: sent.append(text) or "sent")
    assert [r["id"] for r in delivered] == ["m1"]
    assert sent == ["⏰ Reminder: ping Gaohan"]
    assert [r["id"] for r in store.pending()] == ["m2"]
    # failed send → stays pending for the next cycle
    store.add("flaky", datetime(2026, 7, 9, 21, 30))
    assert store.deliver_due(settings, now=NOW, send=lambda s, t: "failed: down") == []
    assert {r["id"] for r in store.pending()} == {"m2", "m3"}
    # cancel works only on pending
    assert store.cancel("m3") and not store.cancel("m1")
    assert [r["id"] for r in store.pending()] == ["m2"]
    assert r1["id"] == "m1" and r2["id"] == "m2"


def test_reminder_actions(settings):
    result = run_action("set_reminder", {"message": "check CI", "when": "+1h"}, settings)
    assert result.startswith("reminder m1 set for ")
    assert "check CI" in run_action("list_reminders", {}, settings)
    assert run_action("cancel_reminder", {"id": "m1"}, settings) == "reminder m1 cancelled"
    assert run_action("list_reminders", {}, settings) == "(no pending reminders)"
    assert "couldn't parse" in run_action(
        "set_reminder", {"message": "x", "when": "someday"}, settings)


def test_send_wechat_disabled_without_target(settings):
    from assistant.platform.notify import send_wechat

    assert send_wechat(settings, "hi").startswith("disabled")
