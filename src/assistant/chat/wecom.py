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
    """WeChat Work channel: pushes agent replies to the owner and, when a
    callback server is configured, receives their messages. Sending needs only
    outbound HTTPS (``enabled`` tracks that the corp/secret/agent creds exist);
    inbound arrives asynchronously via an internal inbox queue fed by the
    callback HTTP server."""

    name = "wecom"

    def __init__(self, settings: Settings):
        """Set ``enabled`` from the presence of corp/secret/agent creds and
        initialize the empty access-token cache, inbound queue, and (unstarted)
        callback server handle."""
        self.settings = settings
        self.enabled = bool(settings.wecom_corp_id and settings.wecom_secret
                            and settings.wecom_agent_id)
        self._token: str = ""
        self._token_expiry: float = 0.0
        self._inbox: queue.Queue = queue.Queue()
        self._server: ThreadingHTTPServer | None = None

    # ── sending (outbound HTTPS only) ────────────────────────────────
    def _access_token(self) -> str:
        """Return a valid WeCom API access token, fetching a fresh one only
        when the cached token is within 60s of expiry. Raises on an API error."""
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
        """Push ``text`` (capped at 2000 chars) to the owner as a WeCom text
        message, or to everyone in the app when no owner userid is set.
        ``in_reply_to`` is unused — WeChat has no reply threading. Raises on an
        API error."""
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
        """Drain and return every message the callback server has queued since
        the last poll (empty list if none) — non-blocking."""
        messages = []
        while True:
            try:
                messages.append(self._inbox.get_nowait())
            except queue.Empty:
                return messages

    def start_callback_server(self) -> bool:
        """Start the background HTTP server that receives WeCom callbacks, so
        inbound messages land in the inbox queue. Returns False (send-only) when
        the Token/AESKey needed to decrypt callbacks aren't configured; True
        once the threaded server is serving on the callback port."""
        if not (self.settings.wecom_token and self.settings.wecom_aes_key):
            return False
        crypto = _MsgCrypto(self.settings.wecom_token, self.settings.wecom_aes_key,
                            self.settings.wecom_corp_id)
        channel = self

        class Handler(BaseHTTPRequestHandler):
            """HTTP handler for WeCom's app callback endpoint: GET serves the
            one-time URL-verification handshake, POST receives owner messages.
            Closes over ``crypto`` (signature/decrypt) and ``channel`` (inbox)."""

            def log_message(self, *args):
                """Silence the default stderr access log; route hits to our
                logger at debug level instead."""
                log.debug("wecom callback: %s", args)

            def _reply(self, code: int, body: str = "") -> None:
                """Write a bare HTTP response with ``code`` and optional body —
                the minimal reply WeCom expects (no headers beyond status)."""
                self.send_response(code)
                self.end_headers()
                self.wfile.write(body.encode())

            def do_GET(self):
                """WeCom URL-verification handshake: decrypt the ``echostr`` and
                echo it back (200) to prove ownership of Token/AESKey, or 400 if
                verification fails."""
                q = parse_qs(urlparse(self.path).query)
                try:
                    echo = crypto.decrypt(
                        q["echostr"][0], q["msg_signature"][0],
                        q["timestamp"][0], q["nonce"][0])
                    self._reply(200, echo)
                except Exception as exc:
                    log.warning("wecom verification failed: %s", exc)
                    self._reply(400)

            def do_POST(self):
                """Receive an incoming WeCom message: decrypt the body, verify
                the sender is the owner, and enqueue the text for ``poll`` to
                pick up. Always answers an empty 200 (no passive reply — the
                agent pushes its answer asynchronously via ``send``); a decrypt
                failure answers 400."""
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
        """Cache the crypto primitives and decode the base64 EncodingAESKey to
        its 32-byte AES key (raising if it isn't exactly 32 bytes). ``token``
        signs callbacks and ``corp_id`` is checked against the decrypted
        payload's receiver."""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        self._cipher_parts = (Cipher, algorithms, modes)
        self.token = token
        self.key = base64.b64decode(encoding_aes_key + "=")
        if len(self.key) != 32:
            raise ValueError("EncodingAESKey must decode to 32 bytes")
        self.corp_id = corp_id

    def decrypt(self, encrypted_b64: str, signature: str, timestamp: str, nonce: str) -> str:
        """Verify a callback's SHA1 signature, then AES-256-CBC decrypt the
        payload and return its inner message string. Raises ValueError on a bad
        signature or if the embedded corp id doesn't match — either means the
        request isn't a genuine WeCom callback. The wire format is a 16-byte
        random prefix, a 4-byte big-endian length, the message, then the corp
        id; PKCS#7 padding is stripped first."""
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
