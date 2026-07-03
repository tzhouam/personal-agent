from assistant.chat.slack_channel import SlackChannel


class FakeSlack(SlackChannel):
    """SlackChannel with the Web API faked out; records outbound calls."""

    def __init__(self, settings, history):
        settings.slack_bot_token = "xoxb-test"
        super().__init__(settings, ["tester@example.com"])
        self.history = history  # newest first, like the real API
        self.posted = []

    def _api(self, method, **params):
        if method == "users.lookupByEmail":
            return {"user": {"id": "U_OWNER"}}
        if method == "conversations.list":
            return {"channels": [{"id": "D1", "user": "U_OWNER"},
                                 {"id": "D2", "user": "U_STRANGER"}]}
        if method == "conversations.history":
            assert params["channel"] == "D1"  # stranger DM never read
            oldest = float(params.get("oldest", 0))
            return {"messages": [m for m in self.history if float(m["ts"]) > oldest]}
        if method == "conversations.open":
            return {"channel": {"id": "D1"}}
        if method == "chat.postMessage":
            self.posted.append(params)
            return {}
        raise AssertionError(f"unexpected api call {method}")


def _msg(ts, text, user="U_OWNER", **extra):
    return {"type": "message", "ts": ts, "text": text, "user": user, **extra}


def test_slack_first_poll_initializes_without_replay(settings):
    channel = FakeSlack(settings, [_msg("100.2", "old 2"), _msg("100.1", "old 1")])
    assert channel.poll() == []                      # history never replayed
    assert channel._watermarks() == {"D1": "100.2"}  # watermark at the tail


def test_slack_poll_owner_filter_order_and_watermark(settings):
    channel = FakeSlack(settings, [_msg("100.0", "seed")])
    channel.poll()  # initialize watermark

    channel.history = [
        _msg("103.0", "bot echo", bot_id="B1"),        # own bot message
        _msg("102.0", "second"),
        _msg("101.5", "intruder", user="U_STRANGER"),  # not the owner
        _msg("101.0", "first"),
        _msg("100.0", "seed"),                         # already seen
    ]
    polled = channel.poll()
    assert [m["text"] for m in polled] == ["first", "second"]  # oldest→newest
    assert polled[0]["channel_id"] == "D1" and polled[0]["sender"] == "U_OWNER"
    assert channel._watermarks()["D1"] == "103.0"
    assert channel.poll() == []  # nothing new → nothing reprocessed


def test_slack_send_replies_to_dm(settings):
    channel = FakeSlack(settings, [])
    channel.send("hello", in_reply_to={"channel_id": "D1"})
    channel.send("push with no inbound message")  # falls back to opening the DM
    assert [p["channel"] for p in channel.posted] == ["D1", "D1"]
    assert channel.posted[0]["text"] == "hello"
