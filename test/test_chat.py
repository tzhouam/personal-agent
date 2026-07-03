import base64
import hashlib
import struct

from assistant.chat.agent import handle_message
from assistant.chat.email_channel import EmailChannel
from assistant.chat.wecom import _MsgCrypto
from assistant.todo_store import TodoStore


class FakeLLM:
    def __init__(self, result):
        self.result = result
        self.prompts = []

    def complete_json(self, prompt, system=None, **kw):
        self.prompts.append(prompt)
        return self.result


def test_handle_message_answers_and_executes_actions(settings):
    store = TodoStore(settings.profile_dir)
    store.upsert("k1", title="Review PR", source="github", priority="red")
    llm = FakeLLM({"reply": "You have 1 open todo.",
                   "actions": [{"type": "add_todo", "title": "Buy GPU"},
                               {"type": "done_todo", "id": "t1"}]})
    reply = handle_message("what's open? also add a todo to buy a GPU and close t1",
                           settings, llm)
    assert reply.startswith("You have 1 open todo.")
    assert "added todo t2: Buy GPU" in reply
    assert "todo t1 marked done" in reply
    # actions really executed against the store
    assert [t["title"] for t in store.open_items()] == ["Buy GPU"]
    # context carried the open todo into the prompt
    assert "[t1] Review PR" in llm.prompts[0]


def test_handle_message_rejects_unknown_and_bad_actions(settings):
    llm = FakeLLM({"reply": "ok", "actions": [{"type": "delete_profile"},
                                              {"type": "done_todo", "id": "t99"}]})
    reply = handle_message("hi", settings, llm)
    assert "unknown action 'delete_profile' ignored" in reply
    assert "no open todo 't99'" in reply


def _raw_mail(sender: str, subject: str, body: str) -> bytes:
    return (f"From: Owner <{sender}>\r\nTo: me\r\nSubject: {subject}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}").encode()


def test_email_channel_parse_auth_and_prefix(settings):
    channel = EmailChannel(settings, ["tester@example.com"])
    ok = channel._parse(_raw_mail("tester@example.com", "agent: list todos", "please"))
    assert ok and ok["text"] == "list todos\nplease" and ok["sender"] == "tester@example.com"
    # subject alone is enough
    assert channel._parse(_raw_mail("tester@example.com", "Re: agent what's due", ""))
    # wrong sender or missing prefix → ignored
    assert channel._parse(_raw_mail("evil@example.com", "agent: hi", "x")) is None
    assert channel._parse(_raw_mail("tester@example.com", "hello", "x")) is None


def test_email_channel_strips_quoted_history(settings):
    channel = EmailChannel(settings, ["tester@example.com"])
    body = "new question\nOn Thu, Jul 3, Assistant wrote:\n> old reply\n> more"
    msg = channel._parse(_raw_mail("tester@example.com", "agent", body))
    assert msg["text"] == "new question"


def _encrypt(crypto: _MsgCrypto, msg: str, token: str, ts: str, nonce: str):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    payload = b"0123456789abcdef" + struct.pack(">I", len(msg.encode())) \
        + msg.encode() + crypto.corp_id.encode()
    pad = 32 - len(payload) % 32
    payload += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(crypto.key), modes.CBC(crypto.key[:16])).encryptor()
    encrypted = base64.b64encode(encryptor.update(payload) + encryptor.finalize()).decode()
    signature = hashlib.sha1("".join(sorted([token, ts, nonce, encrypted])).encode()).hexdigest()
    return encrypted, signature


def test_wecom_crypto_roundtrip():
    aes_key = base64.b64encode(b"k" * 32).decode().rstrip("=")
    crypto = _MsgCrypto("tok", aes_key, "corp1")
    encrypted, signature = _encrypt(crypto, "<xml><Content>hi</Content></xml>",
                                    "tok", "123", "n1")
    assert crypto.decrypt(encrypted, signature, "123", "n1") \
        == "<xml><Content>hi</Content></xml>"
    # tampered signature rejected
    try:
        crypto.decrypt(encrypted, "0" * 40, "123", "n1")
        assert False, "bad signature accepted"
    except ValueError:
        pass
