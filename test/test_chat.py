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


def test_action_review_retries_failures(settings):
    # round 1 emits a bad amount → rejected; the review round corrects it
    class SequenceLLM:
        def __init__(self, results):
            self.results = list(results)
            self.prompts = []

        def complete_json(self, prompt, system=None, **kw):
            self.prompts.append(prompt)
            return self.results.pop(0)

    llm = SequenceLLM([
        {"reply": "记好了", "actions": [
            {"type": "log_transaction", "kind": "spend", "amount": 45,
             "note": "午饭"}]},                       # kind invalid → rejected
        {"reply": "修正后已记录", "actions": [
            {"type": "log_transaction", "kind": "expense", "amount": 45,
             "note": "午饭"}]},
    ])
    reply = handle_message("记账午饭45", settings, llm)
    assert len(llm.prompts) == 2
    assert "transaction rejected" in llm.prompts[1]      # saw the failure
    assert "Actions you just emitted" in llm.prompts[1]
    assert reply.startswith("修正后已记录")               # revised reply kept
    assert "(retry) logged f1: expense 45.0" in reply
    from assistant.finance_store import FinanceStore
    assert FinanceStore(settings.profile_dir).records()[0]["amount"] == 45.0


def test_action_review_skips_success_and_duplicates(settings):
    class CountingLLM:
        def __init__(self, result):
            self.result, self.calls = result, 0

        def complete_json(self, prompt, system=None, **kw):
            self.calls += 1
            return self.result

    # all-success → single LLM call
    llm = CountingLLM({"reply": "ok", "actions": [
        {"type": "add_todo", "title": "Buy GPU"}]})
    handle_message("add todo", settings, llm)
    assert llm.calls == 1
    # duplicate rejection → no retry round
    from assistant.finance_store import FinanceStore
    FinanceStore(settings.profile_dir).add("expense", 68, note="面点王", time="12:30")
    llm = CountingLLM({"reply": "ok", "actions": [
        {"type": "log_transaction", "kind": "expense", "amount": 68,
         "note": "面点王", "time": "12:30"}]})
    reply = handle_message("记一下", settings, llm)
    assert llm.calls == 1 and "duplicate of f1" in reply


def test_action_review_gives_up_when_unfixable(settings):
    class StubbornLLM:
        def __init__(self):
            self.calls = 0

        def complete_json(self, prompt, system=None, **kw):
            self.calls += 1
            if self.calls == 1:
                return {"reply": "done", "actions": [{"type": "done_todo", "id": "t99"}]}
            return {"reply": "那个待办不存在", "actions": []}  # unfixable → stop

    llm = StubbornLLM()
    reply = handle_message("完成t99", settings, llm)
    assert llm.calls == 2                       # one review round, then stop
    assert "no open todo 't99'" in reply
    assert reply.startswith("那个待办不存在")


def test_context_caps_todos_by_urgency(settings):
    from assistant.chat.agent import build_context

    store = TodoStore(settings.profile_dir)
    for i in range(40):
        store.upsert(f"k{i}", title=f"todo number {i}", source="github",
                     priority="yellow", detail="x" * 300)
    ctx = build_context(settings)
    section = ctx.split("## Open todos")[1].split("\n## ")[0]
    assert "…and 15 lower-urgency todos" in section
    assert section.count("[t") == 25
    # per-todo detail is trimmed too
    assert "x" * 121 not in section
