"""Always-on login-QR refresher + the loopback GET /qr route."""
import threading

import httpx
import pytest

from assistant.platform.login_qr import LoginQRRefresher, render_qr_png
from assistant.platform.serve import make_server

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_render_qr_png_is_a_png():
    png = render_qr_png("https://liteapp.weixin.qq.com/q/abc?qrcode=z")
    assert png[:8] == _PNG_MAGIC and len(png) > 100


class _FakeProc:
    """Popen stand-in: streams canned login output lines, then 'exits'."""
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class _Settings:
    openclaw_bin = "/bin/true"
    announce_channel = "openclaw-weixin"


def test_refresher_parses_urls_last_one_wins(monkeypatch):
    lines = [
        "用手机微信扫描以下二维码：\n",
        "▄▄▄ (qr art) ▄▄▄\n",
        "https://liteapp.weixin.qq.com/q/FRESH1?qrcode=aaa&bot_type=3\n",
        "⏳ 二维码已过期，正在刷新...\n",
        "https://liteapp.weixin.qq.com/q/FRESH2?qrcode=bbb&bot_type=3\n",
    ]
    import assistant.platform.login_qr as m
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))
    r = LoginQRRefresher(_Settings())
    r._run_once()                              # one synchronous attempt
    cur = r.current()
    assert cur is not None
    assert "FRESH2" in cur["url"] and cur["url"].endswith("bot_type=3")   # latest wins
    assert cur["png"][:8] == _PNG_MAGIC


def test_refresher_missing_binary_is_not_fatal(monkeypatch):
    import assistant.platform.login_qr as m

    def _boom(*a, **k):
        raise FileNotFoundError("no openclaw")
    monkeypatch.setattr(m.subprocess, "Popen", _boom)
    r = LoginQRRefresher(_Settings())
    # the loop swallows FileNotFoundError and would back off; _run_once raises it
    with pytest.raises(FileNotFoundError):
        r._run_once()
    assert r.current() is None


class _FakeRefresher:
    def __init__(self, cur):
        self._cur = cur

    def current(self):
        return self._cur


def _serve(settings):
    srv = make_server(settings_factory=lambda: settings, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_qr_route_serves_png_and_json(settings):
    url = "https://liteapp.weixin.qq.com/q/X?qrcode=z&bot_type=3"
    srv, base = _serve(settings)
    srv.qr_refresher = _FakeRefresher(
        {"png": render_qr_png(url), "url": url, "ts": "2026-07-22T00:00:00+00:00"})
    try:
        r = httpx.get(f"{base}/qr")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == _PNG_MAGIC
        assert "liteapp.weixin.qq.com" in r.headers["x-qr-url"]
        j = httpx.get(f"{base}/qr?format=json").json()
        assert j["url"] == url and "ts" in j
    finally:
        srv.shutdown()


def test_qr_route_503_when_no_qr_yet(settings):
    srv, base = _serve(settings)                # no qr_refresher attached
    try:
        assert httpx.get(f"{base}/qr").status_code == 503
    finally:
        srv.shutdown()


def test_qr_route_requires_serve_token(settings):
    settings.serve_token = "s3cret"             # gate the route
    srv, base = _serve(settings)
    srv.qr_refresher = _FakeRefresher(
        {"png": b"x", "url": "https://liteapp.weixin.qq.com/q/x", "ts": "t"})
    try:
        assert httpx.get(f"{base}/qr").status_code == 401                       # no bearer
        ok = httpx.get(f"{base}/qr", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
    finally:
        srv.shutdown()
