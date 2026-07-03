"""WeChat Work (企业微信) channel — reaches the owner inside regular WeChat.

Setup (one-time, owner):
1. Register a free WeCom org at work.weixin.qq.com (no business verification
   needed for personal use), create a self-built app (自建应用) → gives
   WECOM_CORP_ID / WECOM_SECRET / WECOM_AGENT_ID; WECOM_OWNER_USERID is your
   member id (usually your name pinyin, see 通讯录).
2. In WeChat: 我 → 设置 → 插件 → 企业微信 (WeChat plugin), scan the org QR —
   the app's messages then arrive inside WeChat and you can reply there.
3. Receiving replies requires the app's 接收消息 callback URL to reach this
   machine (tunnel/VPS → this port). Configure Token + EncodingAESKey from
   that page as WECOM_TOKEN / WECOM_AES_KEY.

Sending only needs outbound HTTPS, so push works even without the callback.
"""

import base64
import hashlib
import logging
import queue
import struct
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx

from ..config import Settings

log = logging.getLogger("assistant")

_API = "https://qyapi.weixin.qq.com/cgi-bin"


class WeComChannel:
    name = "wecom"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = bool(settings.wecom_corp_id and settings.wecom_secret
                            and settings.wecom_agent_id)
        self._token: str = ""
        self._token_expiry: float = 0.0
        self._inbox: queue.Queue = queue.Queue()
        self._server: ThreadingHTTPServer | None = None

    # ── sending (outbound HTTPS only) ────────────────────────────────
    def _access_token(self) -> str:
        if time.time() < self._token_expiry - 60:
            return self._token
        resp = httpx.get(f"{_API}/gettoken", params={
            "corpid": self.settings.wecom_corp_id,
            "corpsecret": self.settings.wecom_secret}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode"):
            raise RuntimeError(f"wecom gettoken: {data}")
        self._token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 7200)
        return self._token

    def send(self, text: str, in_reply_to: dict | None = None) -> None:
        resp = httpx.post(f"{_API}/message/send",
                          params={"access_token": self._access_token()},
                          json={"touser": self.settings.wecom_owner_userid or "@all",
                                "msgtype": "text",
                                "agentid": self.settings.wecom_agent_id,
                                "text": {"content": text[:2000]}}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode"):
            raise RuntimeError(f"wecom send: {data}")

    # ── receiving (callback server; needs a public tunnel to this port) ──
    def poll(self) -> list[dict]:
        messages = []
        while True:
            try:
                messages.append(self._inbox.get_nowait())
            except queue.Empty:
                return messages

    def start_callback_server(self) -> bool:
        if not (self.settings.wecom_token and self.settings.wecom_aes_key):
            return False
        crypto = _MsgCrypto(self.settings.wecom_token, self.settings.wecom_aes_key,
                            self.settings.wecom_corp_id)
        channel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # route through our logger
                log.debug("wecom callback: %s", args)

            def _reply(self, code: int, body: str = "") -> None:
                self.send_response(code)
                self.end_headers()
                self.wfile.write(body.encode())

            def do_GET(self):  # URL verification handshake
                q = parse_qs(urlparse(self.path).query)
                try:
                    echo = crypto.decrypt(
                        q["echostr"][0], q["msg_signature"][0],
                        q["timestamp"][0], q["nonce"][0])
                    self._reply(200, echo)
                except Exception as exc:
                    log.warning("wecom verification failed: %s", exc)
                    self._reply(400)

            def do_POST(self):  # incoming message
                q = parse_qs(urlparse(self.path).query)
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                try:
                    encrypted = ET.fromstring(raw).findtext("Encrypt", "")
                    xml_text = crypto.decrypt(
                        encrypted, q["msg_signature"][0],
                        q["timestamp"][0], q["nonce"][0])
                    root = ET.fromstring(xml_text)
                    sender = root.findtext("FromUserName", "")
                    text = (root.findtext("Content") or "").strip()
                    owner = channel.settings.wecom_owner_userid
                    if text and (not owner or sender == owner):
                        channel._inbox.put({"channel": channel.name, "text": text[:4000],
                                            "subject": "", "sender": sender})
                    elif text:
                        log.warning("wecom message from non-owner %r ignored", sender)
                    self._reply(200)  # empty 200 = no passive reply; we push async
                except Exception as exc:
                    log.warning("wecom callback decrypt failed: %s", exc)
                    self._reply(400)

        self._server = ThreadingHTTPServer(("0.0.0.0", self.settings.wecom_callback_port),
                                           Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        log.info("wecom callback server on :%d", self.settings.wecom_callback_port)
        return True


class _MsgCrypto:
    """WeCom callback crypto (WXBizMsgCrypt): SHA1 signature + AES-256-CBC."""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        self._cipher_parts = (Cipher, algorithms, modes)
        self.token = token
        self.key = base64.b64decode(encoding_aes_key + "=")
        if len(self.key) != 32:
            raise ValueError("EncodingAESKey must decode to 32 bytes")
        self.corp_id = corp_id

    def decrypt(self, encrypted_b64: str, signature: str, timestamp: str, nonce: str) -> str:
        expected = hashlib.sha1(
            "".join(sorted([self.token, timestamp, nonce, encrypted_b64])).encode()
        ).hexdigest()
        if expected != signature:
            raise ValueError("bad msg_signature")
        Cipher, algorithms, modes = self._cipher_parts
        decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
        plain = decryptor.update(base64.b64decode(encrypted_b64)) + decryptor.finalize()
        plain = plain[:-plain[-1]]  # strip PKCS#7 padding
        msg_len = struct.unpack(">I", plain[16:20])[0]
        msg = plain[20:20 + msg_len]
        receiver = plain[20 + msg_len:].decode()
        if receiver != self.corp_id:
            raise ValueError("corp id mismatch")
        return msg.decode()
