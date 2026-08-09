"""Every IMAP connection must carry a read deadline.

`imaplib` blocks inside `_get_response()` from construction onward, so an
unbounded socket turns a half-dead mail host into an indefinite stall. In the
daemon that matters disproportionately: `EmailChannel.poll` runs on the SINGLE
chat-poll thread, which also drives reminders, routines and the daily/weekly
job fan-out for every tenant, so one wedged mailbox stops all of it. The
`try/except` around the poll isolates exceptions, not blocks — and no test can
express a hang (the suite has no pytest-timeout), which is why this asserts the
bound at the construction site instead.
"""
import imaplib
from datetime import datetime, timezone

import pytest

from assistant.agent.chat.email_channel import EmailChannel
from assistant.agent.collectors.gmail import GmailCollector


class _Recorder:
    """Stand-in for IMAP4_SSL that records how it was constructed, then aborts
    the poll body so nothing else has to be faked."""

    calls: list = []

    def __init__(self, host, port, *a, **kw):
        type(self).calls.append({"host": host, "port": port, "timeout": kw.get("timeout")})
        raise RuntimeError("stop here — construction is what we're asserting")


@pytest.fixture
def recorder(monkeypatch):
    _Recorder.calls = []
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _Recorder)
    return _Recorder


def test_email_channel_bounds_its_imap_socket(settings, recorder):
    settings.smtp_user, settings.smtp_password = "o@example.com", "pw"
    settings.imap_host, settings.imap_port = "imap.example.com", 993
    ch = EmailChannel(settings, ["o@example.com"])
    if not ch.enabled:
        pytest.skip("EmailChannel disabled under this fixture")
    with pytest.raises(RuntimeError):
        ch.poll()
    assert recorder.calls and recorder.calls[0]["timeout"] == 30


def test_gmail_collector_bounds_its_imap_socket(settings, recorder):
    settings.smtp_user, settings.smtp_password = "o@example.com", "pw"
    c = GmailCollector(settings)
    if not c.enabled:
        pytest.skip("GmailCollector disabled under this fixture")
    with pytest.raises(RuntimeError):
        c.collect(datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert recorder.calls and recorder.calls[0]["timeout"] == 30
