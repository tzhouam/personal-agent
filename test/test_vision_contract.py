"""F2 regression: `describe_images` must honor its "always returns
`len(paths)` strings" contract against duplicate input paths (the same image
attached twice in one email stages to one sha1-named file) and against a
backend returning a mismatched count — either used to raise KeyError, which
upstream turned into a silently dropped owner email (the UID watermark had
already advanced)."""

from assistant.platform import vision
from assistant.platform.vision import describe_images


def _png(tmp_path, name="a.png"):
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    return str(p)


def _backend(monkeypatch, fn):
    monkeypatch.setattr(vision, "_remote_describe", fn)


def test_duplicate_paths_share_one_description(settings, tmp_path, monkeypatch):
    pic = _png(tmp_path)
    calls = []

    def fake(settings, paths):
        calls.append(list(paths))
        return [f"desc of {p}" for p in paths]

    _backend(monkeypatch, fake)
    out = describe_images(settings, [pic, pic])
    assert out == [f"desc of {pic}"] * 2          # both slots filled, no KeyError
    assert calls == [[pic]]                        # described once, not twice


def test_duplicate_with_invalid_between(settings, tmp_path, monkeypatch):
    pic = _png(tmp_path)
    _backend(monkeypatch, lambda s, ps: ["d"] * len(ps))
    out = describe_images(settings, [pic, str(tmp_path / "missing.png"), pic])
    assert out[0] == "d" and out[2] == "d"
    assert "not found" in out[1]


def test_backend_short_return_pads(settings, tmp_path, monkeypatch):
    pics = [_png(tmp_path, f"{i}.png") for i in range(3)]
    _backend(monkeypatch, lambda s, ps: ["only one"])
    out = describe_images(settings, pics)
    assert len(out) == 3
    assert out[0] == "only one"
    assert all("backend returned no result" in o for o in out[1:])


def test_backend_long_return_trims(settings, tmp_path, monkeypatch):
    pic = _png(tmp_path)
    _backend(monkeypatch, lambda s, ps: ["a", "b", "c"])
    assert describe_images(settings, [pic]) == ["a"]


def test_backend_empty_return_is_failure(settings, tmp_path, monkeypatch):
    pic = _png(tmp_path)
    _backend(monkeypatch, lambda s, ps: [])
    out = describe_images(settings, [pic])
    assert len(out) == 1 and "no vision backend" in out[0]
