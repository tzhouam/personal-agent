"""Vision chain: input validation, backend fallback order, and the plumbing
that carries image paths from chat entry points into the prompt."""

import assistant.vision as vision
from assistant.chat.agent import handle_message
from assistant.vision import describe_images, media_type_for, render_image_context


class FakeLLM:
    def __init__(self, result):
        self.result = result
        self.prompts = []

    def complete_json(self, prompt, system=None, **kw):
        self.prompts.append(prompt)
        return self.result


def _png(tmp_path, name="pic.png", size=100):
    path = tmp_path / name
    path.write_bytes(b"\x89PNG" + b"0" * size)
    return path


def test_media_type_for():
    assert media_type_for("a.PNG") == "image/png"
    assert media_type_for("b.jpeg") == "image/jpeg"
    assert media_type_for("c.pdf") is None


def test_describe_images_validates_paths(settings, tmp_path, monkeypatch):
    ok = _png(tmp_path)
    huge = _png(tmp_path, "huge.png", 11 * 1024 * 1024)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(vision, "_remote_describe",
                        lambda s, paths: [f"desc of {p}" for p in paths])
    out = describe_images(settings, [str(ok), str(tmp_path / "gone.png"),
                                     str(pdf), str(huge)])
    assert out[0] == f"desc of {ok}"
    assert "not found" in out[1]
    assert "unsupported" in out[2]
    assert "too large" in out[3]


def test_describe_images_survives_dead_chain(settings, tmp_path, monkeypatch):
    pic = _png(tmp_path)
    monkeypatch.setattr(vision, "_remote_describe",
                        lambda s, p: (_ for _ in ()).throw(RuntimeError("api down")))
    out = describe_images(settings, [str(pic)])
    assert "no vision backend" in out[0]


def test_render_image_context():
    block = render_image_context(["first", "second"])
    assert "[image 1] first" in block and "[image 2] second" in block
    assert block.startswith("## Attached images")


def test_handle_message_injects_descriptions(settings, tmp_path, monkeypatch):
    pic = _png(tmp_path)
    monkeypatch.setattr("assistant.vision.describe_images",
                        lambda s, p: ["a whiteboard with '15:30' written on it"])
    llm = FakeLLM({"reply": "那是下午三点半的会议安排。", "actions": []})
    reply = handle_message("这是什么？", settings, llm, image_paths=[str(pic)])
    assert reply.startswith("那是下午三点半")
    assert "## Attached images" in llm.prompts[0]
    assert "15:30" in llm.prompts[0]


def test_handle_message_image_only_gets_default_text(settings, tmp_path, monkeypatch):
    pic = _png(tmp_path)
    monkeypatch.setattr("assistant.vision.describe_images", lambda s, p: ["a cat"])
    llm = FakeLLM({"reply": "可爱的猫！", "actions": []})
    handle_message("", settings, llm, image_paths=[str(pic)])
    assert "without text" in llm.prompts[0]


def test_handle_message_caps_image_count(settings, tmp_path, monkeypatch):
    pics = [str(_png(tmp_path, f"p{i}.png")) for i in range(5)]
    seen = {}
    monkeypatch.setattr("assistant.vision.describe_images",
                        lambda s, p: seen.setdefault("n", len(p)) and ["d"] * len(p) or ["d"] * len(p))
    llm = FakeLLM({"reply": "ok", "actions": []})
    handle_message("look", settings, llm, image_paths=pics)
    assert seen["n"] == settings.vision_max_images


def test_email_channel_extracts_image_attachments(settings):
    from email.mime.image import MIMEImage
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from assistant.chat.email_channel import EmailChannel

    msg = MIMEMultipart()
    msg["From"] = "Owner <tester@example.com>"
    msg["Subject"] = "agent: what is this?"
    msg.attach(MIMEText("see attached", "plain"))
    img = MIMEImage(b"\x89PNG fake bytes", _subtype="png")
    img.add_header("Content-Disposition", "attachment", filename="whiteboard.png")
    msg.attach(img)

    channel = EmailChannel(settings, ["tester@example.com"])
    parsed = channel._parse(msg.as_bytes())
    assert parsed["text"] == "what is this?\nsee attached"
    assert len(parsed["images"]) == 1
    saved = parsed["images"][0]
    assert saved.endswith(".png") and (settings.data_dir / "media") in __import__("pathlib").Path(saved).parents
    assert __import__("pathlib").Path(saved).read_bytes() == b"\x89PNG fake bytes"


def test_openai_provider_describe(settings, tmp_path, monkeypatch):
    pic = _png(tmp_path)
    settings.vision_provider = "openai"
    settings.vision_api_key = "sk-test"
    settings.vision_model = "gpt-5-mini"
    calls = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "a red square, text '42'"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["url"] = url
        calls["model"] = json["model"]
        calls["content"] = json["messages"][0]["content"]
        return Resp()

    monkeypatch.setattr("httpx.post", fake_post)
    out = describe_images(settings, [str(pic)])
    assert out == ["a red square, text '42'"]
    assert calls["url"] == "https://api.openai.com/v1/chat/completions"
    assert calls["model"] == "gpt-5-mini"
    assert calls["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_native_multimodal_attaches_images(settings, tmp_path, monkeypatch):
    # llm_supports_images: the main LLM gets the files, no vision pass runs
    pic = _png(tmp_path)
    settings.llm_supports_images = True
    monkeypatch.setattr("assistant.vision.describe_images",
                        lambda s, p: (_ for _ in ()).throw(AssertionError("vision ran")))
    seen = {}

    class NativeLLM(FakeLLM):
        def complete_json(self, prompt, system=None, images=None, **kw):
            seen["images"] = images
            self.prompts.append(prompt)
            return self.result

    llm = NativeLLM({"reply": "看到了", "actions": []})
    reply = handle_message("这是什么？", settings, llm, image_paths=[str(pic)])
    assert reply.startswith("看到了")
    assert seen["images"] == [str(pic)]
    assert "look at them directly" in llm.prompts[0]
    # non-image paths are filtered out of the attachment list
    seen.clear()
    handle_message("x", settings, llm, image_paths=[str(tmp_path / "nope.pdf")])
    assert seen["images"] is None or seen["images"] == []


def test_llm_builds_image_blocks(settings, tmp_path, monkeypatch):
    from assistant.llm import LLM

    pic = _png(tmp_path)
    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)

            class R:
                content = [type("B", (), {"type": "text", "text": "ok"})()]
                stop_reason = "end_turn"
                usage = None
            return R()

    llm = LLM.__new__(LLM)
    llm.client = type("C", (), {"messages": FakeMessages()})()
    llm.default_model = "test-model"
    out = llm.complete("look", images=[str(pic)])
    assert out == "ok"
    content = captured["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[-1] == {"type": "text", "text": "look"}
