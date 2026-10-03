from datetime import datetime, timedelta, timezone

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


def test_parse_when_accepts_iso8601():
    """The chat model emits ISO-8601 for an absolute time; rejecting it cost a
    repair round on every such reminder (2026-07-24)."""
    assert parse_when("2026-07-24T20:55:00", NOW) == datetime(2026, 7, 24, 20, 55)
    assert parse_when("2026-07-24", NOW) == datetime(2026, 7, 24, 0, 0)
    # offset-aware → converted to system local, stored naive (reminders fire
    # against the system clock)
    aware = parse_when("2026-07-24T20:55:00+08:00", NOW)
    assert aware is not None and aware.tzinfo is None


def test_parse_when_aware_future_uses_target_dates_dst_rule():
    """Do not reuse today's fixed EDT/EST offset for a future target date."""
    import os
    import time

    if not hasattr(time, "tzset"):
        return
    prior = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        assert parse_when("2027-01-15T12:00:00+00:00", NOW) == \
            datetime(2027, 1, 15, 7, 0)
    finally:
        if prior is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prior
        time.tzset()


def test_reminder_delivery_gives_up_after_max_attempts(settings):
    """An always-failing send must dead-letter instead of retrying forever: one
    broken send path produced 752 identical failures in a day while the owner
    saw nothing (2026-07-24)."""
    from assistant.platform.notify import _MAX_DELIVERY_ATTEMPTS

    store = ReminderStore(settings.data_dir)
    store.add("interview at 11:00", datetime(2026, 7, 9, 21, 0))
    for _ in range(_MAX_DELIVERY_ATTEMPTS):
        assert store.deliver_due(settings, now=NOW,
                                 send=lambda s, t: "failed: no such file") == []
    assert store.pending() == []                      # no longer retried
    failed = store.failed()
    assert [r["id"] for r in failed] == ["m1"]
    assert failed[0]["attempts"] == _MAX_DELIVERY_ATTEMPTS
    assert "no such file" in failed[0]["last_error"]


def test_reminder_recovers_before_giving_up(settings):
    """A transient failure still retries — give-up is only at the cap."""
    store = ReminderStore(settings.data_dir)
    store.add("ping", datetime(2026, 7, 9, 21, 0))
    assert store.deliver_due(settings, now=NOW, send=lambda s, t: "failed: blip") == []
    assert [r["id"] for r in store.pending()] == ["m1"]
    delivered = store.deliver_due(settings, now=NOW, send=lambda s, t: "sent")
    assert [r["id"] for r in delivered] == ["m1"]
    assert store.failed() == [] and store.pending() == []


def test_weixin_context_classifier_requires_zero_parts_sent():
    from assistant.platform.delivery import is_weixin_context_closed

    exact = ("failed: rc=1 OutboundDeliveryError: sendMessage ret=-2 "
             "errmsg=prepare failed (sent 0/1)")
    assert is_weixin_context_closed(exact)
    assert not is_weixin_context_closed(exact.replace("ret=-2", "ret=-1"))
    assert not is_weixin_context_closed(exact.replace("sent 0/1", "sent 1/2"))
    assert not is_weixin_context_closed("failed: prepare failed")
    assert not is_weixin_context_closed("disabled")


def test_weixin_context_failure_rearms_reminder_for_one_retry(settings):
    """A fresh inbound makes the durable reminder pending again while keeping
    the exhausted attempt count, so another rejection immediately re-deadletters."""
    from assistant.platform.notify import _MAX_DELIVERY_ATTEMPTS

    store = ReminderStore(settings.data_dir)
    store.add("interview", datetime(2026, 7, 9, 21, 0))
    error = ("failed: rc=1 OutboundDeliveryError: sendMessage ret=-2 "
             "errmsg=prepare failed (sent 0/1)")
    for _ in range(_MAX_DELIVERY_ATTEMPTS):
        store.deliver_due(settings, now=NOW, send=lambda *a: error)
    assert store.pending() == [] and [r["id"] for r in store.failed()] == ["m1"]

    assert store.rearm_weixin_context_failures() == 1
    assert store.pending() == []                    # remains terminal/clearable
    [queued] = store.failed()
    assert queued["attempts"] == _MAX_DELIVERY_ATTEMPTS
    assert queued["wechat_retry_queued"] is True
    delivered = store.deliver_due(settings, now=NOW, send=lambda *a: "sent")
    assert [r["id"] for r in delivered] == ["m1"]
    assert store.pending() == [] and store.failed() == []


def test_acknowledging_queued_weixin_reminder_fences_send(settings):
    from assistant.platform.notify import _MAX_DELIVERY_ATTEMPTS

    store = ReminderStore(settings.data_dir)
    reminder = store.add("interview", datetime(2026, 7, 9, 21, 0))
    error = ("failed: rc=1 OutboundDeliveryError: sendMessage ret=-2 "
             "errmsg=prepare failed (sent 0/1)")
    for _ in range(_MAX_DELIVERY_ATTEMPTS):
        store.deliver_due(settings, now=NOW, send=lambda *a: error)
    assert store.rearm_weixin_context_failures() == 1
    assert store.acknowledge_failed(reminder["id"])
    sends = []
    assert store.deliver_due(
        settings, now=NOW,
        send=lambda *a: sends.append(1) or "sent") == []
    assert sends == []


def test_weixin_reminder_rearm_excludes_acknowledged_and_expired(settings):
    from assistant.platform.notify import _MAX_DELIVERY_ATTEMPTS

    store = ReminderStore(settings.data_dir)
    exact = ("failed: rc=1 OutboundDeliveryError: sendMessage ret=-2 "
             "errmsg=prepare failed (sent 0/1)")
    first = store.add("acked", datetime(2026, 7, 9, 21, 0))
    second = store.add("expired", datetime(2026, 7, 9, 21, 0))
    for _ in range(_MAX_DELIVERY_ATTEMPTS):
        store.deliver_due(settings, now=NOW, send=lambda *a: exact)
    assert store.acknowledge_failed(first["id"])
    data = store._load()
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    for row in data["reminders"]:
        if row["id"] == second["id"]:
            row["surfaced_at"] = old
    store._save(data)

    assert store.rearm_weixin_context_failures() == 0
    assert {r["id"] for r in store.failed()} == {"m1", "m2"}


# ── Weixin push-context freshness gate ──────────────────────────────────────
#
# `channel=weixin` on a request is a caller assertion, not proof that an
# inbound refreshed the push token. Re-arming on the flag alone burned the
# one-shot retry against a token Weixin still rejects (2026-09-08/09).

def _token_file(home, account, age_hours=0.0):
    """Write a context-token file whose mtime is `age_hours` in the past."""
    import os
    import time

    d = home / "openclaw-weixin" / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{account}.context-tokens.json"
    path.write_text("{}")
    when = time.time() - age_hours * 3600
    os.utime(path, (when, when))
    return path


def test_weixin_context_fresh_tracks_token_age(tmp_path):
    from assistant.platform.config import Settings
    from assistant.platform.notify import weixin_context_fresh

    settings = Settings(_env_file=None, openclaw_home=str(tmp_path))
    acct = "7763402847f5-im-bot"
    _token_file(tmp_path, acct, age_hours=0.1)
    assert weixin_context_fresh(settings, acct) is True
    # the observed failure: token untouched for days, so Weixin rejects the
    # push and the retry would be spent for nothing
    _token_file(tmp_path, acct, age_hours=72)
    assert weixin_context_fresh(settings, acct) is False


def test_weixin_context_fresh_degrades_open(tmp_path):
    """Unknown state must not silently disable recovery."""
    from assistant.platform.config import Settings
    from assistant.platform.notify import weixin_context_fresh

    settings = Settings(_env_file=None, openclaw_home=str(tmp_path))
    assert weixin_context_fresh(settings, "7763402847f5-im-bot") is True  # no file
    assert weixin_context_fresh(settings, "") is True                     # no account


def test_weixin_context_fresh_rejects_path_traversal(tmp_path):
    """A hostile account id must never be interpolated into a path."""
    from assistant.platform.config import Settings
    from assistant.platform.notify import weixin_context_fresh

    settings = Settings(_env_file=None, openclaw_home=str(tmp_path))
    _token_file(tmp_path, "victim", age_hours=72)
    for hostile in ("../../etc/passwd", "a/../../b", "/abs/path", "UPPER",
                    "sp ace", "x"):
        # rejected by shape → never reaches the filesystem, and never gates
        assert weixin_context_fresh(settings, hostile) is True
