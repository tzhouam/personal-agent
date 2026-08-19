"""Track B regressions: the image-turn consistency patch (F5/F6/F7/F16).

The incident class: the prompt claiming images that are not actually attached
(a sighted model honestly answers it can't see any), silent drops of unusable
attachments at any layer, a describe-fallback that left the rest of the turn
inconsistent, and retrieval results discarded when the compose call failed."""

import base64

import pytest

from assistant.agent.chat.agent import handle_message, handle_turn
from assistant.platform import vision


def _png(tmp_path, name="a.png", size=30):
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * size)
    return str(p)


class Recorder:
    """Scripted LLM: each entry is a dict to return, or an Exception to
    raise; records every (prompt, kwargs) pair."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete_json(self, prompt, system=None, **kw):
        self.calls.append((prompt, kw))
        step = self.script.pop(0) if self.script else {"reply": "ok", "actions": []}
        if isinstance(step, Exception):
            raise step
        return step


# ── F7: validation + honest headers + deterministic notices ─────────────

def test_all_invalid_images_never_claim_attachment(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_supports_images", True)
    llm = Recorder([{"reply": "看不了", "actions": []}])
    reply = handle_message("看看这个", settings, llm,
                           image_paths=[str(tmp_path / "missing.png")])
    prompt, kw = llm.calls[0]
    assert "look at them directly" not in prompt      # no false attach claim
    assert "could NOT be used" in prompt and "not found" in prompt
    assert not kw.get("images")                       # nothing attached
    assert "⚠ 有图片未能使用" in reply                 # owner told, in code


def test_mixed_valid_invalid_attaches_and_reports(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_supports_images", True)
    good = _png(tmp_path)
    llm = Recorder([{"reply": "看到了", "actions": []}])
    reply = handle_message("", settings, llm,
                           image_paths=[good, str(tmp_path / "gone.png")])
    prompt, kw = llm.calls[0]
    assert "look at them directly" in prompt          # the valid one attaches
    assert kw.get("images") == [good]
    assert "gone.png" in prompt and "could NOT be used" in prompt
    assert "gone.png" in reply                        # deterministic notice


def test_oversized_file_rejected_not_crashed(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_supports_images", True)
    monkeypatch.setattr(vision, "_MAX_IMAGE_BYTES", 16)
    big = _png(tmp_path, "big.png", size=64)
    llm = Recorder([{"reply": "ok", "actions": []}])
    reply = handle_message("看看", settings, llm, image_paths=[big])
    _, kw = llm.calls[0]
    assert not kw.get("images")
    assert "too large" in reply


def test_staged_channel_notes_fold_into_notice(settings, monkeypatch):
    monkeypatch.setattr(settings, "llm_supports_images", True)
    llm = Recorder([{"reply": "好的", "actions": []}])
    reply = handle_message("在吗", settings, llm,
                           rejected_images=["[image too large to process: x.jpg]"])
    assert "x.jpg" in reply and "⚠ 有图片未能使用" in reply


def test_cap_overflow_is_reported(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_supports_images", True)
    monkeypatch.setattr(settings, "vision_max_images", 1)
    pics = [_png(tmp_path, f"{i}.png") for i in range(3)]
    llm = Recorder([{"reply": "ok", "actions": []}])
    reply = handle_message("", settings, llm, image_paths=pics)
    _, kw = llm.calls[0]
    assert kw.get("images") == [pics[0]]
    assert "2 more image(s) ignored (max 1)" in reply


def test_notice_reaches_owner_on_hard_failure_exit(settings, tmp_path, monkeypatch):
    """The rejected notice rides _finish — even a turn whose LLM call dies
    still tells the owner about the unusable image."""
    monkeypatch.setattr(settings, "llm_supports_images", True)

    class Dead:
        def complete_json(self, *a, **k):
            raise RuntimeError("api down")

    reply = handle_message("看看", settings, Dead(),
                           image_paths=[str(tmp_path / "nope.png")])
    assert "nope.png" in reply


def test_email_staging_reports_unstageable_parts(settings):
    from email.mime.image import MIMEImage
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from assistant.agent.chat.email_channel import _image_attachments

    msg = MIMEMultipart()
    msg.attach(MIMEText("看看这些"))
    ok = MIMEImage(b"\x89PNG\r\n\x1a\ndata", _subtype="png")
    ok.add_header("Content-Disposition", "attachment", filename="ok.png")
    msg.attach(ok)
    big = MIMEImage(b"x" * (10 * 1024 * 1024 + 1), _subtype="png")
    big.add_header("Content-Disposition", "attachment", filename="big.png")
    msg.attach(big)
    weird = MIMEImage(b"data", _subtype="tiff")
    weird.add_header("Content-Disposition", "attachment", filename="scan.tiff")
    msg.attach(weird)

    paths, rejected = _image_attachments(msg, settings)
    assert len(paths) == 1 and paths[0].endswith(".png")
    assert any("big.png" in r and "too large" in r for r in rejected)
    assert any("scan.tiff" in r and "unsupported" in r for r in rejected)


def test_chat_staging_notes_for_bad_base64_and_type(settings):
    from assistant.platform.serve import _staged_images

    body = {"images": [
        {"media_type": "image/png", "data": base64.b64encode(b"ok").decode()},
        {"media_type": "image/png", "data": "!!!not-base64!!!"},
        {"media_type": "application/pdf", "data": base64.b64encode(b"x").decode()},
        "not-a-dict",
    ]}
    paths, notes = _staged_images(body, settings)
    assert len(paths) == 1
    assert any("invalid base64" in n for n in notes)
    assert any("unsupported image media type" in n for n in notes)
    assert any("malformed" in n for n in notes)


# ── F5: describe-fallback keeps the whole turn consistent ───────────────

def test_fallback_recomposes_prompt_for_followup_calls(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_supports_images", True)
    monkeypatch.setattr(settings, "vision_api_key", "k")
    monkeypatch.setattr(settings, "vision_model", "eyes")
    monkeypatch.setattr(vision, "_remote_describe",
                        lambda s, paths: ["一张收据，金额45元"] * len(paths))
    pic = _png(tmp_path)
    llm = Recorder([
        RuntimeError("image input not supported"),   # native call fails
        {"reply": "", "actions": []},                # fallback → empty reply
        {"reply": "是一张45元的收据", "actions": []},  # empty-reply retry
    ])
    reply = handle_message("这是什么", settings, llm, image_paths=[str(pic)])
    assert "45元的收据" in reply

    native_prompt, native_kw = llm.calls[0]
    assert native_kw.get("images") == [str(pic)]
    fb_prompt, fb_kw = llm.calls[1]
    assert "一张收据，金额45元" in fb_prompt           # descriptions in place
    assert "look at them directly" not in fb_prompt   # no stale attach claim
    assert not fb_kw.get("images")                    # effective images empty
    assert fb_kw.get("role") == "chat"                # routing kept (old bug)
    retry_prompt, retry_kw = llm.calls[2]
    assert "一张收据，金额45元" in retry_prompt        # follow-ups consistent
    assert not retry_kw.get("images")


def test_fallback_without_vision_backend_retries_native(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_supports_images", True)
    pic = _png(tmp_path)
    llm = Recorder([
        RuntimeError("blip"),
        {"reply": "看到了", "actions": []},           # transient: retry works
    ])
    assert handle_message("看看", settings, llm,
                          image_paths=[str(pic)]) == "看到了"
    assert llm.calls[1][1].get("images") == [str(pic)]  # native retried


# ── F6: retrieval results survive a failed compose ──────────────────────

def _seed_finance(settings):
    from assistant.agent.finance_store import FinanceStore

    FinanceStore(settings.profile_dir).add(
        "expense", 45, category="food", note="午饭", when="2026-06-10")


def test_failed_compose_keeps_retrieved_records(settings):
    _seed_finance(settings)
    llm = Recorder([
        {"reply": "我查一下", "actions": [
            {"type": "query_transactions", "month": "2026-06"}]},
        RuntimeError("compose died"),
    ])
    reply = handle_message("六月花了多少", settings, llm)
    assert "45" in reply                     # raw records kept, not vanished
    assert "✔" in reply


def test_successful_compose_replaces_raw_records(settings):
    _seed_finance(settings)
    llm = Recorder([
        {"reply": "我查一下", "actions": [
            {"type": "query_transactions", "month": "2026-06"}]},
        {"reply": "六月总支出45元，都花在吃饭上", "actions": []},
    ])
    reply = handle_message("六月花了多少", settings, llm)
    assert "六月总支出45元" in reply
    assert "✔" not in reply                  # composed answer, no raw dump


# ── F16: transient backend error ≠ unconfigured ─────────────────────────

def test_configured_backend_error_says_try_again(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vision_api_key", "k")
    monkeypatch.setattr(settings, "vision_model", "eyes")

    def boom(s, paths):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(vision, "_remote_describe", boom)
    out = vision.describe_images(settings, [_png(tmp_path)])
    assert "failed this time" in out[0] and "try again" in out[0]
    assert "VISION_*" not in out[0]


def test_unconfigured_backend_keeps_setup_hint(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "_remote_describe", lambda s, p: None)
    out = vision.describe_images(settings, [_png(tmp_path)])
    assert "VISION_*" in out[0]


def test_text_only_model_gets_same_validation_notice(settings, tmp_path, monkeypatch):
    """The arbiter validates for BOTH model modes: a text-only deployment's
    owner also gets the deterministic rejection notice."""
    monkeypatch.setattr(settings, "llm_supports_images", False)
    llm = Recorder([{"reply": "嗯", "actions": []}])
    reply = handle_message("看看", settings, llm,
                           image_paths=[str(tmp_path / "ghost.png")])
    prompt, kw = llm.calls[0]
    assert "ghost.png" in prompt and "could NOT be used" in prompt
    assert "ghost.png" in reply and "⚠ 有图片未能使用" in reply


def test_image_only_all_invalid_prompt_never_claims_usable(settings, tmp_path, monkeypatch):
    """A truly image-only message whose images are ALL unusable must not tell
    the model the owner 'sent the attached image(s) — react to what they
    show'."""
    monkeypatch.setattr(settings, "llm_supports_images", True)
    llm = Recorder([{"reply": "图片没收到", "actions": []}])
    handle_message("", settings, llm, image_paths=[str(tmp_path / "x.png")])
    prompt, _ = llm.calls[0]
    assert "react to what they show" not in prompt
    assert "NONE could be used" in prompt


def test_validation_before_cap(settings, tmp_path, monkeypatch):
    """An invalid path must not consume a cap slot a valid image needed."""
    monkeypatch.setattr(settings, "llm_supports_images", True)
    monkeypatch.setattr(settings, "vision_max_images", 1)
    good = _png(tmp_path)
    llm = Recorder([{"reply": "ok", "actions": []}])
    handle_message("看", settings, llm,
                   image_paths=[str(tmp_path / "bad.png"), good])
    _, kw = llm.calls[0]
    assert kw.get("images") == [good]


def test_chat_staging_rejects_malformed_containers(settings):
    from assistant.platform.serve import _staged_images

    paths, notes = _staged_images({"images": "AAAA" * 1000}, settings)
    assert paths == [] and notes == ["[malformed images payload ignored]"]
    paths, notes = _staged_images({"image_paths": {"a": 1}}, settings)
    assert paths == [] and notes == ["[malformed image_paths payload ignored]"]


def test_email_mime_type_wins_over_filename(settings):
    from email.mime.image import MIMEImage
    from email.mime.multipart import MIMEMultipart

    from assistant.agent.chat.email_channel import _image_attachments

    msg = MIMEMultipart()
    tiff = MIMEImage(b"II*\x00data", _subtype="tiff")     # tiff named .png
    tiff.add_header("Content-Disposition", "attachment", filename="scan.png")
    msg.attach(tiff)
    bare = MIMEImage(b"\xff\xd8\xffjpegdata", _subtype="jpeg")  # no filename
    msg.attach(bare)

    paths, rejected = _image_attachments(msg, settings)
    assert len(paths) == 1 and paths[0].endswith(".jpg")  # suffix FROM type
    assert any("image/tiff" in r for r in rejected)       # tiff refused


def test_identical_non_retrieval_outcome_survives_compose(settings):
    """The compose drop is positional: an unrelated action whose outcome
    string happens to equal the retrieval outcome must not be removed."""
    from assistant.agent.chat import agent as agent_mod

    _seed_finance(settings)
    q = {"type": "query_transactions", "month": "2026-06"}
    llm = Recorder([
        {"reply": "查", "actions": [q, {"type": "add_todo", "title": "还房贷"}]},
        {"reply": "六月支出45元", "actions": []},
    ])
    reply = handle_message("六月花了多少，另外记个待办还房贷", settings, llm)
    assert "六月支出45元" in reply
    assert "还房贷" in reply                    # the todo outcome line survives


def test_image_only_fallback_swaps_owner_sentinel(settings, tmp_path, monkeypatch):
    """Image-only turn + native failure + configured vision: the fallback must
    also swap the owner-message sentinel — the model has descriptions, not
    attachments, and must not be told to look at attached images."""
    monkeypatch.setattr(settings, "llm_supports_images", True)
    monkeypatch.setattr(settings, "vision_api_key", "k")
    monkeypatch.setattr(settings, "vision_model", "eyes")
    monkeypatch.setattr(vision, "_remote_describe", lambda s, p: ["一碗面"])
    pic = _png(tmp_path)
    llm = Recorder([
        RuntimeError("no image support"),
        {"reply": "看起来是一碗面", "actions": []},
    ])
    reply = handle_message("", settings, llm, image_paths=[str(pic)])
    assert "一碗面" in reply
    fb_prompt, fb_kw = llm.calls[1]
    assert "react to what they show" not in fb_prompt      # stale sentinel gone
    assert "descriptions above are their content" in fb_prompt
    assert not fb_kw.get("images")


def test_huge_filename_is_bounded_in_notes(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_supports_images", True)
    llm = Recorder([{"reply": "ok", "actions": []}])
    long_name = "x" * 4000 + ".png"
    reply = handle_message("看看", settings, llm,
                           image_paths=[str(tmp_path / long_name)])
    assert len(reply) < 600                       # bounded, not 4KB amplified
    assert "…" in reply


def test_listener_forwards_rejected_images(settings, monkeypatch):
    """The legacy chat-listen loop must carry channel rejection notes too."""
    from assistant.agent.chat import service

    sent = {}

    class FakeChannel:
        name = "fake"
        enabled = True

        def poll(self):
            return [{"channel": "fake", "text": "看看", "subject": "",
                     "sender": "o", "images": [],
                     "rejected_images": ["[image too large to process: b.jpg]"]}]

        def send(self, text, in_reply_to=None):
            sent["reply"] = text

    monkeypatch.setattr(service, "build_channels",
                        lambda settings, log_wecom=True: [FakeChannel()])
    monkeypatch.setattr(service, "handle_message",
                        lambda text, settings, llm, image_paths=None,
                        rejected_images=None:
                        f"notes={rejected_images}")
    assert service.run_listener(settings, once=True) == 0
    assert "b.jpg" in sent["reply"]


def test_truly_identical_outcome_strings_survive_compose(settings, monkeypatch):
    """Byte-identical retrieval and non-retrieval outcome strings: only the
    retrieval one (by position) is replaced by the composed answer."""
    from assistant.agent.actions import registry as reg

    _seed_finance(settings)
    from assistant.agent.actions.registry import run_action

    import dataclasses

    identical = run_action("query_transactions", {"month": "2026-06"}, settings)
    monkeypatch.setitem(reg.ACTIONS, "add_todo",
                        dataclasses.replace(reg.ACTIONS["add_todo"],
                                            handler=lambda s, a: identical))
    llm = Recorder([
        {"reply": "查", "actions": [
            {"type": "query_transactions", "month": "2026-06"},
            {"type": "add_todo", "title": "x"}]},
        {"reply": "六月支出45元", "actions": []},
    ])
    reply = handle_message("六月花了多少", settings, llm)
    assert "六月支出45元" in reply
    assert identical.splitlines()[0] in reply     # the non-retrieval copy stays
