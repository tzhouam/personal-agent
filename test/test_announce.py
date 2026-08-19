from assistant.agent.deliver.announce import announce_digest


def _enabled(settings, tmp_path, script: str):
    bin_path = tmp_path / "fake-openclaw"
    bin_path.write_text(script)
    bin_path.chmod(0o755)
    return settings.model_copy(update={
        "wechat_announce": True,
        "announce_account": "acct-1",
        "announce_to": "owner-id",
        "openclaw_bin": str(bin_path),
    })


def test_disabled_by_default(settings):
    assert announce_digest(settings, "hi") == "disabled"
    half = settings.model_copy(update={"wechat_announce": True})
    assert announce_digest(half, "hi").startswith("disabled (set ")


def test_sent_passes_exact_cli_args(settings, tmp_path):
    s = _enabled(settings, tmp_path,
                 '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$(dirname "$0")/args.txt"\nexit 0\n')
    assert announce_digest(s, "Daily digest done.") == "sent"
    args = (tmp_path / "args.txt").read_text().splitlines()
    assert args == ["message", "send", "--channel", "openclaw-weixin",
                    "--account", "acct-1", "--target", "owner-id",
                    "-m", "Daily digest done."]


def test_failure_never_raises(settings, tmp_path):
    s = _enabled(settings, tmp_path, '#!/bin/sh\necho "boom" >&2\nexit 7\n')
    assert announce_digest(s, "x") == "failed: rc=7 boom (sent 0/1)"  # F10: part detail rides in failures
    missing = s.model_copy(update={"openclaw_bin": str(tmp_path / "nope")})
    assert announce_digest(missing, "x").startswith("failed: ")


def test_split_message_byte_bounded_cjk(settings):
    """F10: parts respect a BYTE budget (CJK = 3 bytes/char), markers ride
    inside the budget, no codepoint is split, truncation is visible."""
    from assistant.platform.notify import split_message

    text = "第一段。" * 200                    # ~2.4KB of CJK
    parts = split_message(text, max_bytes=600)
    assert 1 < len(parts) <= 5
    for p in parts:
        assert len(p.encode()) <= 600          # marker included in budget
    assert parts[0].startswith("(1/")
    assert "".join(p.split(") ", 1)[1] for p in parts).startswith("第一段。")

    huge = "x" * 100000
    parts = split_message(huge, max_bytes=600, hard_max_parts=3)
    assert len(parts) == 3 and parts[-1].endswith("…(回复过长已截断)")


def test_send_chunked_report_semantics():
    """parts_sent counts DELIVERED parts; first-part failure reports 0; the
    tail is never sent out of order."""
    from assistant.platform.notify import send_chunked

    sent = []
    ok = send_chunked(sent.append, "短消息", max_bytes=2048)
    assert ok == {"parts_sent": 1, "parts_total": 1, "error": None}

    calls = []

    def fail_second(part):
        calls.append(part)
        if len(calls) == 2:
            raise RuntimeError("gone")

    calls.clear()
    r = send_chunked(fail_second, "段落。" * 400, max_bytes=600)
    assert r["parts_sent"] == 1 and r["parts_total"] >= 3
    assert "sent 1/" in r["error"]
    assert len(calls) == 2                     # stopped at the failure


def test_wecom_send_chunks_and_targets_sender(settings, monkeypatch):
    """WeCom sends split at 2048 BYTES and target the message's sender —
    never the @all broadcast — when a sender is known."""
    import httpx as _httpx

    from assistant.agent.chat import wecom as wecom_mod

    sent = []

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"errcode": 0}

    def fake_post(url, params=None, json=None, timeout=None):
        sent.append(json)
        return R()

    monkeypatch.setattr(wecom_mod.httpx, "post", fake_post)
    monkeypatch.setattr(settings, "wecom_corp_id", "c")
    monkeypatch.setattr(settings, "wecom_secret", "s")
    monkeypatch.setattr(settings, "wecom_agent_id", "1")
    ch = wecom_mod.WeComChannel(settings)
    ch._token, ch._token_expiry = "tok", 9e12

    ch.send("你好" * 800, in_reply_to={"sender": "boss"})   # 4800 bytes
    assert len(sent) >= 3
    assert all(s["touser"] == "boss" for s in sent)
    assert all(len(s["text"]["content"].encode()) <= 2048 for s in sent)


def test_listener_refuses_scheduling_actions(settings):
    """F15: under chat-listen, set_reminder/create_routine refuse with an
    actionable message (nothing would ever deliver them there)."""
    from assistant.agent.actions.registry import run_action
    from assistant.agent.chat.service import _listener_active

    token = _listener_active.set(True)
    try:
        out = run_action("set_reminder", {"message": "开会", "when": "+5m"},
                         settings)
        assert "chat-listen" in out and "assistant serve" in out
        out = run_action("create_routine", {"task": "报天气", "time": "07:30",
                                            "days": "daily"}, settings)
        assert "chat-listen" in out
    finally:
        _listener_active.reset(token)
    # outside the listener they work normally
    assert "reminder" in run_action("set_reminder",
                                    {"message": "开会", "when": "+5m"},
                                    settings)


def test_truncated_final_part_stays_within_byte_budget(settings):
    """F10 round-2: the truncation marker rides INSIDE the budget."""
    from assistant.platform.notify import split_message

    parts = split_message("好" * 100000, max_bytes=600, hard_max_parts=3)
    assert len(parts) == 3 and parts[-1].endswith("…(回复过长已截断)")
    for p in parts:
        assert len(p.encode()) <= 600
