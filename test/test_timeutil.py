"""Temporal anchor — the one line of real-world time appended to every LLM
prompt tail (deterministic: frozen datetimes, never the live clock)."""
from datetime import datetime, timedelta, timezone

from assistant.platform import timeutil
from assistant.platform.timeutil import temporal_anchor

_HKT = timezone(timedelta(hours=8), "HKT")
_FROZEN = datetime(2026, 7, 17, 9, 32, 41, tzinfo=_HKT)   # a Friday


def test_anchor_renders_date_time_offset_weekday():
    line = temporal_anchor(now=_FROZEN)
    assert line == "[temporal anchor] Now: 2026-07-17 09:32 +0800 (Friday, HKT)"


def test_anchor_minute_granularity_is_stable():
    # seconds differ → identical suffix (rapid same-turn calls share the tail)
    a = temporal_anchor(now=_FROZEN)
    b = temporal_anchor(now=_FROZEN.replace(second=5))
    assert a == b


def test_anchor_tolerates_platform_tzname_forms():
    # an unnamed tz reports a synthesized name (e.g. "UTC-05:00") — the line
    # must render cleanly whatever form the platform returns (never parsed)
    naked = datetime(2026, 7, 17, 9, 32, tzinfo=timezone(timedelta(hours=-5)))
    line = temporal_anchor(now=naked)
    assert line.startswith("[temporal anchor] Now: 2026-07-17 09:32 -0500 (Friday")


def test_default_clock_is_aware_local(monkeypatch):
    monkeypatch.setattr(timeutil, "_now", lambda: _FROZEN)
    assert "2026-07-17 09:32" in temporal_anchor()
