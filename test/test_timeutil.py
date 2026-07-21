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


# ── event-day resolution (per-day records: "昨天" must become an absolute date) ──

import pytest
from datetime import date

from assistant.platform.timeutil import resolve_day, weekday_cn

_D = date(2026, 7, 20)  # a Monday


@pytest.mark.parametrize("token,expected", [
    ("今天", "2026-07-20"), ("today", "2026-07-20"),
    ("昨天", "2026-07-19"), ("昨日", "2026-07-19"), ("yesterday", "2026-07-19"),
    ("前天", "2026-07-18"), ("大前天", "2026-07-17"),
    ("3天前", "2026-07-17"), ("三天前", "2026-07-17"), ("3 days ago", "2026-07-17"),
    ("明天", "2026-07-21"), ("后天", "2026-07-22"),
    ("2026-07-15", "2026-07-15"),          # absolute passthrough
    ("  昨天 ", "2026-07-19"),              # whitespace tolerant
])
def test_resolve_day_resolves(token, expected):
    assert resolve_day(token, _D) == expected


@pytest.mark.parametrize("token", ["", "上上个礼拜三", "sometime", "13月40号", "昨"])
def test_resolve_day_unparseable_is_none(token):
    # None (not today!) so the caller rejects instead of silently mis-dating
    assert resolve_day(token, _D) is None


def test_weekday_cn():
    assert weekday_cn(date(2026, 7, 20)) == "周一"   # Monday
    assert weekday_cn(date(2026, 7, 17)) == "周五"   # Friday
