"""Transport fixtures exercise real admission, acknowledgements and send receipts."""

import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from assistant.platform.runtime.channels import QQChannel, QQConfig, SlackChannel, SlackConfig
from assistant.platform.runtime.channels.base import split_text
from assistant.platform.runtime.channels.qq import parse_segments


def slack_config(**updates):
    """Use fake scoped tokens; no test accesses a real Slack connection."""
    return SlackConfig(app_token="fake-app-secret", bot_token="fake-bot-secret",
                       allowed_users=("U1",), team_id="T1", bot_user_id="B1", **updates)


def slack_payload(*, direct=False, **updates):
    """A real-shaped Socket Mode event with a stable source message timestamp."""
    event = {"type": "message" if direct else "app_mention", "user": "U1",
             "channel": "D1" if direct else "C1", "channel_type": "im" if direct else "channel",
             "ts": "1700000000.001", "text": "hello" if direct else "<@B1> hello"}
    event.update(updates)
    return {"envelope_id": "envelope-1", "type": "events_api", "payload": {
        "team_id": "T1", "event_id": "upstream-id", "event": event}}


def qq_payload(**updates):
    """One authorized QQ group mention in OneBot array format."""
    event = {"post_type": "message", "message_type": "group", "self_id": 99,
             "user_id": 123, "group_id": 456, "message_id": 12,
             "message": [{"type": "at", "data": {"qq": "99"}},
                         {"type": "text", "data": {"text": " hello"}}]}
    event.update(updates)
    return event


class SocketFixture:
    """A bidirectional socket whose response queue is independent of the handler."""

    def __init__(self, on_send=None):
        """Capture writes and optionally synthesize authoritative API receipts."""
        self.incoming = asyncio.Queue()
        self.sent = []
        self.on_send = on_send
        self.closed = False

    def __aiter__(self):
        """Yield complete JSON frames like websockets' client connection."""
        return self

    async def __anext__(self):
        """A sentinel ends the fixture without timing assumptions."""
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)

    async def send(self, raw):
        """Record a frame before making its response available to the reader."""
        data = json.loads(raw)
        self.sent.append(data)
        if self.on_send:
            await self.on_send(self, data)

    async def close(self):
        """Release a blocked reader, including on repeated close calls."""
        self.closed = True
        await self.incoming.put(None)


def test_channels_require_explicit_owner_and_hide_credentials():
    """Open sender admission and accidental credential representations are refused."""
    with pytest.raises(ValueError, match="allowed_users"):
        SlackConfig(app_token="secret", bot_token="secret")
    with pytest.raises(ValueError, match="specific authorized"):
        QQConfig(allowed_users=("*",))
    with pytest.raises(ValueError, match="remote"):
        QQConfig(ws_url="ws://onebot.example:3001", allowed_users=("123",))
    with pytest.raises(ValueError, match="credentials"):
        QQConfig(ws_url="ws://localhost:3001?access_token=secret", allowed_users=("123",))
    assert "fake-app-secret" not in repr(slack_config())
    assert "fake-bot-secret" not in repr(slack_config())
    assert "qq-secret" not in repr(QQConfig(allowed_users=("123",), access_token="qq-secret"))


def test_slack_admission_threads_and_stable_event_identity():
    """Workspace, sender and source message identity survive transport redelivery."""
    channel = SlackChannel(slack_config())
    event = channel.decode_event(slack_payload())
    assert event.text == "hello"
    assert event.thread_id == event.message_id == "1700000000.001"
    assert event.workspace_id == "T1" and event.owner_id == "default"
    duplicate = slack_payload(type="message")
    duplicate["payload"]["event_id"] = "different-events-api-id"
    assert channel.decode_event(duplicate).dedup_key == event.dedup_key
    dm = channel.decode_event(slack_payload(direct=True))
    assert dm.is_direct and dm.thread_id == ""
    thread = channel.decode_event(slack_payload(thread_ts="1700000000.000"))
    assert thread.thread_id == "1700000000.000"
    assert thread.session_key != event.session_key
    for patch in ({"user": "U2"}, {"bot_id": "BOT"}, {"user": "B1"},
                  {"subtype": "message_changed"}, {"type": "message", "text": "unaddressed"}):
        assert channel.decode_event(slack_payload(**patch)) is None
    wrong_team = slack_payload()
    wrong_team["payload"]["team_id"] = "other-team"
    assert channel.decode_event(wrong_team) is None
    # Payload owner assertions are ignored.
    forged = slack_payload()
    forged["payload"]["owner_id"] = "someone-else"
    assert channel.decode_event(forged).owner_id == "default"
    asyncio.run(channel.close())


def test_slack_attachment_metadata_never_carries_bearer_download_urls():
    """Transport metadata is useful without persisting credential-bearing URLs."""
    channel = SlackChannel(slack_config())
    event = channel.decode_event(slack_payload(text="", files=[{
        "id": "F1", "name": "receipt.png", "mimetype": "image/png", "size": 100,
        "url_private": "https://files.slack.com/secret-file"}]))
    assert event.metadata["attachments"][0]["name"] == "receipt.png"
    assert "url_private" not in event.metadata["attachments"][0]
    asyncio.run(channel.close())


def test_slack_ack_precedes_slow_execution_and_duplicates_do_not_execute_twice():
    """A blocked model turn cannot delay Socket Mode acknowledgements."""
    async def scenario():
        channel = SlackChannel(slack_config())
        socket = SocketFixture()
        started, release = asyncio.Event(), asyncio.Event()
        handled = []

        async def handler(event):
            assert socket.sent[0] == {"envelope_id": "envelope-1"}
            handled.append(event)
            started.set()
            await release.wait()

        channel._worker = asyncio.create_task(channel._dispatch(handler))
        assert await channel.receive_envelope(slack_payload(), socket)
        await asyncio.wait_for(started.wait(), 1)
        again = slack_payload()
        again["envelope_id"] = "envelope-2"
        await asyncio.wait_for(channel.receive_envelope(again, socket), 1)
        assert len(socket.sent) == 2 and len(handled) == 1
        release.set()
        await channel._queue.join()
        await channel.close()

    asyncio.run(scenario())


def test_slack_overflow_is_not_acknowledged_or_marked_seen():
    """Do not claim acceptance when the bounded queue has no room."""
    async def scenario():
        channel = SlackChannel(slack_config(queue_size=1))
        socket = SocketFixture()
        await channel.receive_envelope(slack_payload(), socket)
        newer = slack_payload(ts="1700000000.002")
        newer["envelope_id"] = "envelope-2"
        assert not await channel.receive_envelope(newer, socket)
        assert socket.sent == [{"envelope_id": "envelope-1"}]
        assert channel.decode_event(newer).dedup_key not in channel._seen
        await channel.close()

    asyncio.run(scenario())


def test_slack_durable_acceptance_finishes_before_ack_and_callback():
    """Provider ACK is a durable inbox receipt, not merely a volatile queue write."""
    async def scenario():
        channel = SlackChannel(slack_config())
        socket = SocketFixture()
        accepting, persisted, handled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def accept(event):
            accepting.set()
            await persisted.wait()

        async def handler(event):
            assert persisted.is_set() and len(socket.sent) == 1
            handled.set()

        channel.set_acceptor(accept)
        channel._worker = asyncio.create_task(channel._dispatch(handler))
        receiving = asyncio.create_task(channel.receive_envelope(slack_payload(), socket))
        await asyncio.wait_for(accepting.wait(), 1)
        assert not socket.sent and channel._queue.empty() and not handled.is_set()
        persisted.set()
        assert await receiving
        await asyncio.wait_for(handled.wait(), 1)
        await channel.close()

    asyncio.run(scenario())


def test_slack_failed_durable_admission_stays_unacknowledged_and_retryable():
    """A database error must not acknowledge or deduplicate an uncommitted event."""
    async def scenario():
        channel = SlackChannel(slack_config())
        socket = SocketFixture()

        async def fail(event):
            raise OSError("fixture storage unavailable")

        channel.set_acceptor(fail)
        payload = slack_payload()
        assert not await channel.receive_envelope(payload, socket)
        assert not socket.sent and channel._queue.empty()
        assert channel.decode_event(payload).dedup_key not in channel._seen
        channel.set_acceptor(None)
        assert await channel.receive_envelope(payload, socket)
        assert socket.sent == [{"envelope_id": "envelope-1"}]
        await channel.close()

    asyncio.run(scenario())


def test_slack_durable_inbox_accepts_even_when_callback_queue_is_full():
    """Durably accepted envelopes can be ACKed while host workers catch up."""
    async def scenario():
        channel = SlackChannel(slack_config(queue_size=1))
        socket = SocketFixture()
        accepted = []

        async def accept(event):
            accepted.append(event)

        channel.set_acceptor(accept)
        await channel.receive_envelope(slack_payload(), socket)
        newer = slack_payload(ts="1700000000.002")
        newer["envelope_id"] = "envelope-2"
        assert await channel.receive_envelope(newer, socket)
        assert len(accepted) == len(socket.sent) == 2
        assert channel._queue.qsize() == 1
        await channel.close()

    asyncio.run(scenario())


def test_slack_send_preserves_full_text_thread_and_receipts():
    """Every chunk is retained and no reply escapes into another Slack room."""
    async def scenario():
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            assert request.headers["Authorization"] == "Bearer fake-bot-secret"
            return httpx.Response(200, json={"ok": True, "ts": str(len(requests))})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            channel = SlackChannel(slack_config(), http_client=http)
            event = channel.decode_event(slack_payload())
            text = "α" * 3550 + "\n" + "end " * 1200
            receipt = await channel.send(event, text)
            assert receipt.status == "sent"
            assert len(receipt.message_ids) == len(requests) > 1
            assert "".join(item["text"] for item in requests) == text
            assert all(item["channel"] == "C1" and item["thread_ts"] == event.thread_id
                       and len(item["text"]) <= 3500 for item in requests)
            assert (await channel.send(replace(event, owner_id="other"), "x")).status == "failed"
            await channel.close()

    asyncio.run(scenario())


def test_slack_uncertain_send_is_not_retried_and_errors_are_secret_free():
    """A dropped response cannot silently duplicate an already accepted message."""
    async def scenario():
        calls = []

        def fail(request):
            calls.append(request)
            raise httpx.ReadTimeout("fake-bot-secret in a remote exception", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
            channel = SlackChannel(slack_config(), http_client=http)
            receipt = await channel.send(channel.decode_event(slack_payload()), "hello")
            assert receipt.status == "unknown" and len(calls) == 1
            assert "fake-bot-secret" not in receipt.error
            await channel.close()

    asyncio.run(scenario())


def test_slack_explicit_rejections_are_not_reported_as_uncertain_delivery():
    """Slack rate limits and denied requests provide authoritative failure."""
    async def scenario():
        for response in (httpx.Response(429, headers={"Retry-After": "1"}),
                         httpx.Response(200, json={"ok": False, "error": "fake-secret"})):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as http:
                channel = SlackChannel(slack_config(), http_client=http)
                receipt = await channel.send(channel.decode_event(slack_payload()), "hello")
                assert receipt.status == "failed" and "fake-secret" not in receipt.error
                await channel.close()

    asyncio.run(scenario())


def test_slack_run_opens_socket_with_app_token_and_replies_with_bot_token():
    """The complete adapter connects, admits, acknowledges and replies in-thread."""
    async def scenario():
        calls = []
        finished = asyncio.Event()
        socket = SocketFixture()
        await socket.incoming.put(slack_payload())

        def respond(request):
            method = request.url.path.rsplit("/", 1)[-1]
            calls.append((method, request.headers["Authorization"]))
            if method == "auth.test":
                return httpx.Response(200, json={"ok": True, "team_id": "T1", "user_id": "B1"})
            if method == "apps.connections.open":
                return httpx.Response(200, json={"ok": True, "url": "wss://wss-primary.slack.com/link?ticket=fake"})
            return httpx.Response(200, json={"ok": True, "ts": "reply-1"})

        async def connect(url):
            assert url.startswith("wss://wss-primary.slack.com/")
            return socket

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            channel = SlackChannel(slack_config(), http_client=http, socket_factory=connect)
            assert not channel.connected

            async def handle(event):
                assert channel.connected
                assert socket.sent == [{"envelope_id": "envelope-1"}]
                assert (await channel.send(event, "verified")).status == "sent"
                finished.set()

            running = asyncio.create_task(channel.run(handle))
            await asyncio.wait_for(finished.wait(), 1)
            await channel.close()
            await asyncio.wait_for(running, 1)
            assert not channel.connected
        assert ("apps.connections.open", "Bearer fake-app-secret") in calls
        assert ("chat.postMessage", "Bearer fake-bot-secret") in calls

    asyncio.run(scenario())


def test_cq_strings_split_before_decoding_and_array_text_stays_literal():
    """Encoded CQ text never becomes a bot mention or attachment."""
    channel = QQChannel(QQConfig(allowed_users=("123",), self_id="99"))
    assert channel.decode_event(qq_payload(message="&#91;CQ:at,qq=99&#93; pretend")) is None
    event = channel.decode_event(qq_payload(message="[CQ:at,qq=99] hello &amp; welcome &#91;ok&#93;"))
    assert event.text == "hello & welcome [ok]" and event.addressed
    segments = parse_segments("before[CQ:image,file=foo&#44;bar,url=https://example.test/x]after")
    assert segments[1]["data"]["file"] == "foo,bar"
    assert parse_segments([{"type": "text", "data": {"text": "&amp;"}}])[0]["data"]["text"] == "&amp;"
    asyncio.run(channel.close())


def test_qq_normalizes_owner_group_private_and_quote_scopes():
    """QQ identity comes from trusted configuration and authorized platform users."""
    channel = QQChannel(QQConfig(allowed_users=("123",), allowed_groups=("456",), self_id="99"))
    group = channel.decode_event(qq_payload())
    private = channel.decode_event(qq_payload(message_type="private", message="hello"))
    assert group.room_id == "456" and not group.is_direct
    assert private.room_id == "123" and private.is_direct
    assert group.session_key != private.session_key
    assert channel.decode_event(qq_payload(user_id=99)) is None
    assert channel.decode_event(qq_payload(user_id=987)) is None
    assert channel.decode_event(qq_payload(self_id=100)) is None
    assert channel.decode_event(qq_payload(group_id=999)) is None
    assert channel.decode_event(qq_payload(group_id="bad-target")) is None
    assert channel.decode_event(qq_payload(message_id={"id": "12"})) is None
    assert channel.decode_event(qq_payload(message="not addressed")) is None
    channel._remember("90", "previous bot reply", True)
    reply = channel.decode_event(qq_payload(message="[CQ:reply,id=90]continue"))
    assert reply.addressed and reply.metadata["quoted_text"] == "previous bot reply"
    asyncio.run(channel.close())


def test_qq_reader_resolves_api_during_slow_handler_and_preserves_long_text():
    """The OneBot reader stays live while a callback awaits its send receipt."""
    async def scenario():
        finished = asyncio.Event()
        receipts, texts = [], []

        async def respond(socket, request):
            if request["action"] == "get_login_info":
                data = {"user_id": 99}
            else:
                data = {"message_id": 100 + len(texts)}
                texts.append(request["params"]["message"][-1]["data"]["text"])
            await socket.incoming.put({"status": "ok", "retcode": 0,
                                       "data": data, "echo": request["echo"]})

        socket = SocketFixture(respond)

        async def connect(url, headers):
            assert headers == {"Authorization": "Bearer fake-qq-token"}
            return socket

        channel = QQChannel(QQConfig(allowed_users=("123",), access_token="fake-qq-token"),
                            socket_factory=connect)
        text = "line\n" * 1000 + "[CQ:at,qq=all]"

        async def handler(event):
            receipts.append(await channel.send(event, text))
            finished.set()

        running = asyncio.create_task(channel.run(handler))
        await socket.incoming.put(qq_payload())
        await asyncio.wait_for(finished.wait(), 2)
        assert receipts[0].status == "sent"
        assert "".join(texts) == text and all(len(chunk) <= 1800 for chunk in texts)
        sends = [item for item in socket.sent if item["action"] == "send_group_msg"]
        assert all(item["params"]["group_id"] == 456 for item in sends)
        assert sends[-1]["params"]["message"][-1]["type"] == "text"
        await channel.close()
        await asyncio.wait_for(running, 1)

    asyncio.run(scenario())


def test_qq_missing_receipt_stays_unknown_and_rejection_stays_failed():
    """Report what the provider acknowledged; never infer success from a socket write."""
    async def scenario():
        channel = QQChannel(QQConfig(allowed_users=("123",), api_timeout=0.01))
        channel._ws = SocketFixture()
        event = channel.decode_event(qq_payload())
        receipt = await channel.send(event, "hello")
        assert receipt.status == "unknown" and len(channel._ws.sent) == 1
        assert not channel._pending

        async def reject(socket, request):
            await socket.incoming.put({"status": "failed", "retcode": 100,
                                       "message": "fake-secret", "echo": request["echo"]})

        socket = SocketFixture(reject)
        channel._ws = socket
        channel._reader = asyncio.create_task(channel._read(socket))
        receipt = await channel.send(event, "hello")
        assert receipt.status == "failed" and "fake-secret" not in receipt.error
        await channel.close()

    asyncio.run(scenario())


def test_qq_durable_inbox_retains_events_beyond_callback_queue_capacity():
    """QQ's volatile callback queue cannot discard already admitted owner work."""
    async def scenario():
        channel = QQChannel(QQConfig(allowed_users=("123",), queue_size=1))
        socket = SocketFixture()
        channel._ws = socket
        accepted = []

        async def accept(event):
            accepted.append(event)

        channel.set_acceptor(accept)
        await socket.incoming.put(qq_payload(message_id=12))
        await socket.incoming.put(qq_payload(message_id=13))
        await socket.incoming.put(None)
        await channel._read(socket)
        assert [event.message_id for event in accepted] == ["12", "13"]
        assert channel._queue.qsize() == 1
        assert not socket.sent  # accepted work is not falsely rejected as busy
        await channel.close()

    asyncio.run(scenario())


def test_split_text_does_not_drop_unicode_or_linebreaks():
    """Lossless splitting is independent of markdown and language."""
    text = "你好\n" * 80 + "👩🏽‍💻" * 50
    chunks = split_text(text, 31)
    assert "".join(chunks) == text
    assert all(0 < len(chunk) <= 31 for chunk in chunks)


def test_qq_real_local_websocket_handshake_and_reply():
    """The shared WebSocket transport interoperates with a local OneBot fixture."""
    async def scenario():
        from websockets.asyncio.server import serve

        replied = asyncio.Event()
        received = []

        async def server(socket):
            async for raw in socket:
                request = json.loads(raw)
                if request["action"] == "get_login_info":
                    await socket.send(json.dumps({"status": "ok", "retcode": 0,
                                                  "data": {"user_id": 99}, "echo": request["echo"]}))
                    await socket.send(json.dumps(qq_payload()))
                else:
                    received.append(request)
                    await socket.send(json.dumps({"status": "ok", "retcode": 0,
                                                  "data": {"message_id": 77}, "echo": request["echo"]}))

        async with serve(server, "127.0.0.1", 0) as fixture:
            port = fixture.sockets[0].getsockname()[1]
            channel = QQChannel(QQConfig(ws_url=f"ws://127.0.0.1:{port}", allowed_users=("123",)))
            assert not channel.connected

            async def handle(event):
                assert channel.connected
                receipt = await channel.send(event, "verified")
                assert receipt.status == "sent" and receipt.message_ids == ("77",)
                replied.set()

            running = asyncio.create_task(channel.run(handle))
            await asyncio.wait_for(replied.wait(), 3)
            await channel.close()
            await asyncio.wait_for(running, 1)
            assert not channel.connected
            assert received[0]["params"]["message"][-1]["data"]["text"] == "verified"

    asyncio.run(scenario())
