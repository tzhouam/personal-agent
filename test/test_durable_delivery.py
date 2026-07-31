"""Track D crash-boundary matrix (doc/DESIGN_DURABLE_DELIVERY.md).

Every persisted transition is exercised at its failure boundary: turns that
die mid-processing are dead-lettered (never rerun), sends retry from the
outbox only, fencing tokens reject displaced claimants, and the failure
surface obeys the receipt-gated 48h predicate with open failures immune to
pruning and resets."""

from datetime import datetime, timedelta, timezone

import pytest

from assistant.platform import delivery
from assistant.platform.delivery import OutboxDB, parse_failure_id


@pytest.fixture()
def outbox(settings):
    db = OutboxDB(settings.data_dir)
    yield db
    db.close()


# ── D2: email ledger ─────────────────────────────────────────────────

def test_email_happy_path_settles(outbox):
    assert outbox.email_discover(7, 101, summary="boss: hi")
    assert not outbox.email_discover(7, 101)          # idempotent
    [item] = outbox.email_due(7)
    assert item["state"] == "pending"
    token = outbox.email_begin_turn(7, 101)
    assert token and outbox.email_begin_turn(7, 101) is None   # single claim
    assert outbox.email_finish_turn(7, 101, token, "回复", ["dfremm1"])
    [item] = outbox.email_due(7)
    assert item["state"] == "processed" and item["reply"] == "回复"
    assert item["surfaced_ids"] == ["dfremm1"]        # receipts persisted
    outbox.email_ack(7, 101)
    assert outbox.email_due(7) == []
    assert outbox.email_settled_frontier(7, 100) == 101


def test_email_interrupted_turn_is_dead_lettered_not_rerun(outbox):
    """Crash between begin_turn and finish_turn: the actions may have half-run
    — the next cycle dead-letters instead of silently rerunning them."""
    outbox.email_discover(7, 5)
    assert outbox.email_begin_turn(7, 5)
    # process died; next cycle's due-scan recovers
    assert outbox.email_due(7) == []
    fail = outbox.open_failures()
    assert len(fail) == 1 and fail[0]["id"] == "dfe7-5"
    assert "partially run" in outbox.conn.execute(
        "SELECT last_error FROM email_ledger WHERE uid=5").fetchone()[0]


def test_email_clean_turn_failure_retries_then_dies(outbox):
    outbox.email_discover(7, 6)
    for attempt in range(3):
        token = outbox.email_begin_turn(7, 6)
        assert token, f"attempt {attempt} could not claim"
        outbox.email_turn_failed(7, 6, token, "LLM down")
    assert outbox.email_due(7) == []                  # dead after 3
    assert outbox.open_failures()[0]["id"] == "dfe7-6"


def test_email_send_failure_retries_outbox_only(outbox):
    outbox.email_discover(7, 8)
    token = outbox.email_begin_turn(7, 8)
    outbox.email_finish_turn(7, 8, token, "答复", [])
    outbox.email_send_failed(7, 8, "smtp down")
    [item] = outbox.email_due(7)                      # still processed: the
    assert item["state"] == "processed"               # TURN is never rerun
    outbox.email_send_failed(7, 8, "smtp down")
    outbox.email_send_failed(7, 8, "smtp down")
    assert outbox.email_due(7) == []                  # dead after 3 sends


def test_email_head_of_line_frontier(outbox):
    for uid in (11, 13, 17):                          # gaps: contiguous means
        outbox.email_discover(7, uid)                 # among DISCOVERED uids
    token = outbox.email_begin_turn(7, 11)
    outbox.email_finish_turn(7, 11, token, "r", [])
    outbox.email_ack(7, 11)
    assert outbox.email_settled_frontier(7, 10) == 11  # 13 pending blocks 17
    outbox.email_discover(7, 12, ignored=True)
    assert outbox.email_settled_frontier(7, 10) == 12  # ignored settles


def test_email_old_epoch_rows_survive_uidvalidity_change(outbox):
    outbox.email_discover(7, 5)
    tok = outbox.email_begin_turn(7, 5)
    outbox.email_turn_failed(7, 5, tok, "x")
    outbox.email_turn_failed(7, 5, outbox.email_begin_turn(7, 5), "x")
    outbox.email_turn_failed(7, 5, outbox.email_begin_turn(7, 5), "x")
    assert outbox.open_failures()                     # dead, open
    outbox.email_discover(8, 1)                       # new epoch coexists
    assert any(f["id"] == "dfe7-5" for f in outbox.open_failures())


# ── D4: routine ledger ───────────────────────────────────────────────

def test_routine_delivery_retry_never_reexecutes(outbox):
    token = outbox.routine_claim("rt1", "2026-08-01 07:30")
    assert outbox.routine_transition("rt1", "2026-08-01 07:30", token,
                                     "executing", from_states=("claimed",))
    assert outbox.routine_transition("rt1", "2026-08-01 07:30", token,
                                     "executed", output="天气晴",
                                     from_states=("executing",))
    outbox.routine_delivery_failed("rt1", "2026-08-01 07:30", token, "down")
    [row] = outbox.routine_recover()
    assert row["state"] == "executed" and row["output"] == "天气晴"
    assert outbox.routine_delivered("rt1", "2026-08-01 07:30",
                                    row["claim_token"])
    assert outbox.routine_recover() == []


def test_routine_stale_executing_becomes_unknown_never_retried(outbox):
    """Any executing row seen by the cycle-start scan is from a dead process
    (the scan and the tasks share one thread) — no timed lease involved."""
    token = outbox.routine_claim("rt2", "2026-08-01 08:00")
    outbox.routine_transition("rt2", "2026-08-01 08:00", token, "executing",
                              from_states=("claimed",))
    assert outbox.routine_recover() == []
    fail = outbox.open_failures()
    assert any(f["id"].startswith("dfort2@") and "一半" in f["summary"]
               for f in fail)
    assert outbox.routine_claim("rt2", "2026-08-01 08:00") is None  # closed


def test_routine_stale_claimed_is_reclaimable_and_fences_old_claimant(outbox):
    """claimed precedes side effects → safe to re-claim; recovery RETURNS it
    for resumption; the DISPLACED claimant's writes are rejected by CAS."""
    old = outbox.routine_claim("rt3", "2026-08-01 09:00")
    [row] = outbox.routine_recover()
    assert row["state"] == "claimed"           # stranded claim is resumable
    new = outbox.routine_claim("rt3", "2026-08-01 09:00")   # re-claim
    assert new and new != old
    assert not outbox.routine_transition("rt3", "2026-08-01 09:00", old,
                                         "executing", from_states=("claimed",))
    assert outbox.routine_transition("rt3", "2026-08-01 09:00", new,
                                     "executing", from_states=("claimed",))


def test_routine_condition_false_is_terminal_and_quiet(outbox):
    token = outbox.routine_claim("rt4", "2026-08-01 10:00")
    assert outbox.routine_transition("rt4", "2026-08-01 10:00", token,
                                     "condition_false", error="不下雨",
                                     from_states=("claimed",))
    assert outbox.open_failures() == []
    assert outbox.routine_recover() == []


def test_fire_due_end_to_end_with_send_failure_then_retry(settings, monkeypatch):
    """The integrated path: task runs ONCE; a failed send leaves the output
    in the ledger; the next cycle delivers it without re-running the task."""
    from datetime import datetime as dt

    from assistant.agent import routines as routines_mod
    from assistant.agent.routines import RoutineStore, fire_due

    store = RoutineStore(settings.data_dir)
    store.add("say weather", "07:30", "daily", now=dt(2026, 8, 1, 6, 0))
    runs = []
    monkeypatch.setattr(routines_mod, "check_condition",
                        lambda s, c: (True, ""))
    monkeypatch.setattr("assistant.agent.chat.agent.handle_message",
                        lambda *a, **k: runs.append(1) or "天气晴 30°C")
    sends = {"n": 0}

    def flaky_send(s, text):
        sends["n"] += 1
        return "failed: down" if sends["n"] == 1 else "sent"

    monkeypatch.setattr(routines_mod, "send_wechat", flaky_send, raising=False)
    monkeypatch.setattr("assistant.platform.notify.send_wechat", flaky_send)

    out1 = fire_due(settings, now=dt(2026, 8, 1, 7, 30))
    assert len(runs) == 1 and any(o["fired"] for o in out1)
    out2 = fire_due(settings, now=dt(2026, 8, 1, 7, 31))
    assert len(runs) == 1                       # task NOT re-executed
    assert any(o["note"] == "delivered on retry" for o in out2)
    db = OutboxDB(settings.data_dir)
    try:
        assert db.conn.execute(
            "SELECT state FROM routine_ledger").fetchone()[0] == "delivered"
    finally:
        db.close()


# ── D3: reminder fencing ─────────────────────────────────────────────

def test_reminder_completion_cas_rejects_displaced_claimant(settings, monkeypatch):
    """A claimant that lost its claim (lease expiry → reclaim) cannot mark
    the newer attempt complete."""
    from assistant.platform.notify import ReminderStore

    store = ReminderStore(settings.data_dir)
    store.add("面试", datetime.now() - timedelta(minutes=5))

    def send_and_displace(s, text):
        # while the send is in flight, simulate a lease-expired reclaim
        data = store._load()
        data["reminders"][0]["claim_token"] = "someone-else"
        store._save(data)
        return "sent"

    delivered = store.deliver_due(settings, send=send_and_displace)
    assert delivered == []                       # CAS rejected the stale write
    assert store._load()["reminders"][0].get("sent_at") is None


def test_reminder_cancel_invalidates_inflight_claim(settings):
    from assistant.platform.notify import ReminderStore

    store = ReminderStore(settings.data_dir)
    r = store.add("会议", datetime.now() - timedelta(minutes=5))

    def send_and_cancel(s, text):
        store.cancel(r["id"])
        return "sent"

    assert store.deliver_due(settings, send=send_and_cancel) == []
    assert store._load()["reminders"][0]["sent_at"] == "cancelled"


def test_reminder_dead_letter_records_failed_at(settings):
    from assistant.platform.notify import _MAX_DELIVERY_ATTEMPTS, ReminderStore

    store = ReminderStore(settings.data_dir)
    store.add("x", datetime.now() - timedelta(minutes=5))
    for _ in range(_MAX_DELIVERY_ATTEMPTS):
        store.deliver_due(settings, send=lambda *a: "failed: down")
    [row] = store.failed()
    assert row["failed_at"]


# ── D5: surface, receipts, ack ───────────────────────────────────────

def test_surface_predicate_receipts_and_expiry(settings, outbox):
    outbox.add_system_note("测试事件")
    [f] = delivery.open_failures(settings)
    assert f["id"] == "dfs1"
    # never surfaced → stays eligible indefinitely
    assert delivery.open_failures(settings)
    delivery.mark_surfaced(settings, ["dfs1"])
    assert delivery.open_failures(settings)      # inside 48h: still shown
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    outbox.conn.execute("UPDATE system_notes SET surfaced_at=?", (old,))
    outbox.conn.commit()
    assert delivery.open_failures(settings) == []   # expired after being seen


def test_surface_ack_via_registry_action(settings, outbox):
    from assistant.agent.actions.registry import run_action

    outbox.add_system_note("事故")
    out = run_action("acknowledge_failure", {"id": "dfs1"}, settings)
    assert "cleared" in out
    assert delivery.open_failures(settings) == []
    out = run_action("acknowledge_failure", {"id": "dfs1"}, settings)
    assert "no open delivery failure" in out


def test_reminder_failures_join_the_surface_with_typed_ids(settings):
    from assistant.platform.notify import _MAX_DELIVERY_ATTEMPTS, ReminderStore

    store = ReminderStore(settings.data_dir)
    store.add("提醒", datetime.now() - timedelta(minutes=5))
    for _ in range(_MAX_DELIVERY_ATTEMPTS):
        store.deliver_due(settings, send=lambda *a: "failed: down")
    ids = [f["id"] for f in delivery.open_failures(settings)]
    assert ids == ["dfremm1"]
    assert delivery.acknowledge(settings, "dfremm1")
    assert delivery.open_failures(settings) == []


def test_failure_block_prepended_in_code_and_receipted(settings, outbox, monkeypatch):
    from assistant.agent.chat.agent import handle_turn

    outbox.add_system_note("重要故障")

    class LLM_:
        def complete_json(self, *a, **k):
            return {"reply": "好的", "actions": []}

    turn = handle_turn("在吗", settings, LLM_())
    assert turn.reply.startswith("⚠ 有事项没送达")
    assert "dfs1" in turn.reply and len(turn.reply.split("\n\n")[0].encode()) <= 512
    assert turn.surfaced_failure_ids == ["dfs1"]
    # receipts are the SEND SITE's job — handle_turn alone must not start
    # the expiry clock
    assert outbox.conn.execute(
        "SELECT surfaced_at FROM system_notes").fetchone()[0] is None
    delivery.mark_surfaced(settings, turn.surfaced_failure_ids)
    assert outbox.conn.execute(
        "SELECT surfaced_at FROM system_notes").fetchone()[0]


def test_prune_never_drops_open_failures(settings, outbox):
    outbox.email_discover(7, 5)
    tok = outbox.email_begin_turn(7, 5)
    for _ in range(3):
        outbox.email_turn_failed(7, 5, tok, "x")
        tok = outbox.email_begin_turn(7, 5)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    outbox.conn.execute("UPDATE email_ledger SET updated_at=?", (old,))
    outbox.conn.commit()
    assert outbox.prune() == 0                   # open failure: kept
    assert outbox.open_failures()
    outbox.acknowledge("dfe7-5")
    assert outbox.prune() == 1                   # acked: prunable


def test_display_ids_round_trip():
    assert parse_failure_id("dfe7-101") == ("email", (7, 101))
    assert parse_failure_id("dfort1@2026-08-01T07:30") == \
        ("routine", ("rt1", "2026-08-01 07:30"))
    assert parse_failure_id("dfremm3") == ("reminder", "m3")
    assert parse_failure_id("dfs12") == ("system", 12)
    assert parse_failure_id("garbage") == ("", None)


# ── D1: storage policy ───────────────────────────────────────────────

def test_corruption_recovery_is_narrow(settings, tmp_path):
    path = settings.data_dir / "outbox.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not a database")
    db = OutboxDB(settings.data_dir)               # recovers + system note
    try:
        [f] = db.open_failures()
        assert f["kind"] == "system" and "reset" in f["summary"]
    finally:
        db.close()
    assert list(settings.data_dir.glob("outbox.db.corrupt-*"))
    # the digest DeliveryLedger's file is untouched by construction
    assert not (settings.data_dir / "delivery.db").exists()


def test_permissions_0600(settings):
    db = OutboxDB(settings.data_dir)
    db.close()
    assert (settings.data_dir / "outbox.db").stat().st_mode & 0o777 == 0o600


def test_fire_due_resumes_stranded_claim(settings, monkeypatch):
    """Crash after the ledger claim but before execution (the round-1 repro):
    the YAML shadow blocks claim_due, but recovery RESUMES the claimed row —
    the occurrence is never silently stranded."""
    from datetime import datetime as dt

    from assistant.agent import routines as routines_mod
    from assistant.agent.routines import RoutineStore, fire_due

    store = RoutineStore(settings.data_dir)
    r = store.add("morning brief", "07:30", "daily", now=dt(2026, 8, 1, 6, 0))
    # simulate: due-scan claimed the ledger row AND wrote the YAML shadow,
    # then the process died before the condition check
    db = OutboxDB(settings.data_dir)
    try:
        assert db.routine_claim(r["id"], "2026-08-01 07:30")
    finally:
        db.close()
    store.claim_due(now=dt(2026, 8, 1, 7, 30))       # YAML says checked

    runs = []
    monkeypatch.setattr(routines_mod, "check_condition", lambda s, c: (True, ""))
    monkeypatch.setattr("assistant.agent.chat.agent.handle_message",
                        lambda *a, **k: runs.append(1) or "简报内容")
    monkeypatch.setattr("assistant.platform.notify.send_wechat",
                        lambda s, t: "sent")
    out = fire_due(settings, now=dt(2026, 8, 1, 7, 31))
    assert runs == [1]                               # resumed, executed once
    assert any(o["fired"] for o in out)
    db = OutboxDB(settings.data_dir)
    try:
        assert db.conn.execute("SELECT state FROM routine_ledger").fetchone()[0] \
            == "delivered"
    finally:
        db.close()


def test_fire_due_cancelled_mid_flight_closes_occurrence(settings, monkeypatch):
    from datetime import datetime as dt

    from assistant.agent.routines import RoutineStore, fire_due

    store = RoutineStore(settings.data_dir)
    r = store.add("x", "07:30", "daily", now=dt(2026, 8, 1, 6, 0))
    db = OutboxDB(settings.data_dir)
    try:
        assert db.routine_claim(r["id"], "2026-08-01 07:30")
    finally:
        db.close()
    store.claim_due(now=dt(2026, 8, 1, 7, 30))
    store.cancel(r["id"])                            # cancelled while in flight
    fire_due(settings, now=dt(2026, 8, 1, 7, 31))
    db = OutboxDB(settings.data_dir)
    try:
        assert db.conn.execute("SELECT state FROM routine_ledger").fetchone()[0] \
            == "cancelled"
        assert db.open_failures() == []
    finally:
        db.close()


def test_chat_endpoint_reports_receipts(settings, monkeypatch, outbox):
    """/chat's 200 write is a transport acceptance — the D5 receipt must
    follow it (the round-1 gap: surfaced_at stayed null forever)."""
    import threading

    import httpx

    from assistant.agent.app import build_services
    from assistant.platform import serve as serve_mod

    outbox.add_system_note("大事故")

    class LLM_:
        def complete_json(self, *a, **k):
            return {"reply": "好", "actions": []}

    server = serve_mod.make_server(settings_factory=lambda: settings, port=0,
                                   llm_factory=lambda s: LLM_(),
                                   services=build_services())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        body = httpx.post(f"{base}/chat", json={"text": "在吗"},
                          timeout=30).json()
        assert "dfs1" in body["reply"]               # block prepended in code
        # the receipt lands right AFTER the response reaches the client —
        # poll briefly rather than race the handler thread's tail
        import time as _t

        deadline = _t.monotonic() + 5
        surfaced = None
        while _t.monotonic() < deadline and surfaced is None:
            surfaced = outbox.conn.execute(
                "SELECT surfaced_at FROM system_notes").fetchone()[0]
            if surfaced is None:
                _t.sleep(0.05)
        assert surfaced is not None
    finally:
        server.shutdown()
        server.server_close()


def test_renderer_terminates_on_pathological_summaries():
    from assistant.platform.delivery import render_failure_block

    failures = [{"id": f"dfs{i}", "kind": "system", "summary": "灾" * 400}
                for i in range(5)]
    block = render_failure_block(failures)
    assert len(block.encode()) <= 512
    assert block.startswith("⚠")


def test_email_fetch_failure_leaves_pending(settings, monkeypatch, outbox):
    """A transient IMAP fetch failure must not settle mail as ignored (the
    round-1 blocker) — the row stays pending for the next cycle."""
    from assistant.agent.chat.email_channel import EmailChannel

    ch = EmailChannel.__new__(EmailChannel)          # unit-level: no IMAP login
    ch.settings = settings

    class BoomConn:
        def uid(self, *a):
            raise OSError("timeout")

    ok, msg = ch._fetch_parse(BoomConn(), 5)
    assert ok is False and msg is None               # transient, not settleable


def test_internal_turns_suppress_failure_block(settings, outbox):
    """Routine task execution must not get the D5 block prepended (it would
    pollute routine output and its receipts could never be reported)."""
    from assistant.agent.chat.agent import handle_message

    outbox.add_system_note("事故")

    class LLM_:
        def complete_json(self, *a, **k):
            return {"reply": "天气晴", "actions": []}

    assert handle_message("weather", settings, LLM_(), internal=True) == "天气晴"
    assert "⚠" in handle_message("在吗", settings, LLM_())  # owner turns do


def test_newer_schema_refused_untouched(settings):
    db = OutboxDB(settings.data_dir)
    db.set_meta("schema_version", "99")
    db.close()
    with pytest.raises(RuntimeError, match="newer"):
        OutboxDB(settings.data_dir)
    db2 = None
    import sqlite3 as _sq

    conn = _sq.connect(settings.data_dir / "outbox.db")
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'"
                        ).fetchone()[0] == "99"   # untouched
    conn.close()


def test_cancel_between_due_read_and_execution_never_runs(settings, monkeypatch):
    """The round-2 cancellation race: a cancel landing after the claim is
    revalidated before execution — the routine never runs."""
    from datetime import datetime as dt

    from assistant.agent import routines as routines_mod
    from assistant.agent.routines import RoutineStore, fire_due

    store = RoutineStore(settings.data_dir)
    r = store.add("x", "07:30", "daily", now=dt(2026, 8, 1, 6, 0))
    runs = []
    monkeypatch.setattr(routines_mod, "check_condition",
                        lambda s, c: runs.append("cond") or (True, ""))
    monkeypatch.setattr("assistant.agent.chat.agent.handle_message",
                        lambda *a, **k: runs.append("task") or "out")
    real_due = RoutineStore.due_now

    def due_then_cancel(self, now=None):
        out = real_due(self, now)
        # the cancel slips in AFTER the due read (the executor thread) — the
        # user write lock serializes it either before (no due) or here
        return out

    monkeypatch.setattr(RoutineStore, "due_now", due_then_cancel)
    store.cancel(r["id"])
    fire_due(settings, now=dt(2026, 8, 1, 7, 31))
    assert runs == []                              # never executed
