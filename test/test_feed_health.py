"""Bounded feed gathering and durable cooldown regressions."""

import json
import multiprocessing
import stat
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

from assistant.agent.research import feed_health, feeds
from assistant.agent.research.feed_health import FeedHealthStore, source_fingerprint
from assistant.agent.research.pipeline import _bounded_feed_workers, _gather_feed_items


_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><item><title>one</title>
<link>https://items.example/one</link><description>summary</description>
</item></channel></rss>"""


def _write_sources(path, sources):
    path.write_text(yaml.safe_dump({"sources": sources}, sort_keys=False))


class _Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


def _claim_in_process(payload):
    """Spawn-safe helper exercising the real cross-process flock boundary."""
    shared_dir, source, now_iso = payload
    now = datetime.fromisoformat(now_iso)
    return FeedHealthStore(
        Path(shared_dir), cooldown_hours=72, now=lambda: now,
    ).claim(source).mode


def test_fetch_feed_uses_one_exact_phase_timeout(monkeypatch):
    calls = []

    class Response:
        text = _RSS

        @staticmethod
        def raise_for_status():
            return None

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(feeds.httpx, "get", fake_get)
    assert len(feeds.fetch_feed("https://feed.example/rss")) == 1
    assert len(calls) == 1  # no nested/application retry
    timeout = calls[0][1]["timeout"]
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == \
        (5.0, 10.0, 5.0, 2.0)
    assert calls[0][1]["follow_redirects"] is True


def test_fetch_feed_retains_timeout_override_compatibility(monkeypatch):
    """Existing callers may still supply the helper's historical timeout."""
    calls = []

    class Response:
        text = _RSS

        @staticmethod
        def raise_for_status():
            return None

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(feeds.httpx, "get", fake_get)
    assert len(feeds.fetch_feed("https://feed.example/rss", timeout=17)) == 1
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 17
    assert calls[0][1]["follow_redirects"] is True


def test_gather_default_four_workers_preserves_configured_order(
        settings, monkeypatch):
    sources = [
        {"name": f"source-{index}", "url": f"https://feed.example/{index}",
         "lang": "en"}
        for index in range(5)
    ]
    _write_sources(settings.sources_file, sources)

    lock = threading.Lock()
    active = 0
    max_active = 0
    started = []
    four_started = threading.Event()
    release_fast = threading.Event()
    release_first = threading.Event()
    fifth_finished = threading.Event()

    def fake_fetch(url):
        nonlocal active, max_active
        index = int(url.rsplit("/", 1)[1])
        with lock:
            active += 1
            max_active = max(max_active, active)
            started.append(index)
            if active == 4:
                four_started.set()
        try:
            if index == 0:
                assert release_first.wait(5)
            elif index < 4:
                assert release_fast.wait(5)
            else:
                fifth_finished.set()
            return [{"title": f"item-{index}",
                     "url": f"https://items.example/{index}",
                     "published": "", "summary": ""}]
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(feeds, "fetch_feed", fake_fetch)
    health = {}
    with ThreadPoolExecutor(max_workers=1) as caller:
        result = caller.submit(_gather_feed_items, settings, health)
        assert four_started.wait(5)
        with lock:
            assert active == max_active == 4
            assert set(started) == {0, 1, 2, 3}
        # Sources 1..3 finish first, source 4 then takes their free slot, while
        # source 0 deliberately completes last.
        release_fast.set()
        assert fifth_finished.wait(5)
        assert not result.done()
        release_first.set()
        items = result.result(timeout=5)

    assert [item["title"] for item in items] == \
        [f"item-{index}" for index in range(5)]
    assert list(health) == [f"source-{index}" for index in range(5)]


def test_health_labels_are_globally_unique_and_reserve_existing_notes(
        settings, monkeypatch):
    sources = [
        {"name": name, "url": f"https://feed.example/{index}", "lang": "en"}
        for index, name in enumerate(("arxiv", "x", "x", "x #2"))
    ]
    _write_sources(settings.sources_file, sources)
    config = SimpleNamespace(
        sources_file=settings.sources_file,
        shared_dir=settings.shared_dir,
        research_feed_workers=1,
        research_feed_cooldown_hours=0,
    )

    def fake_fetch(url):
        index = int(url.rsplit("/", 1)[1])
        return [{"title": f"item-{index}",
                 "url": f"https://items.example/{index}",
                 "published": "", "summary": ""}]

    monkeypatch.setattr(feeds, "fetch_feed", fake_fetch)
    health = {"arxiv": "7 candidates from 1 query"}
    items = _gather_feed_items(config, health)

    assert health["arxiv"] == "7 candidates from 1 query"
    expected = ["arxiv #2", "x", "x #2", "x #2 #2"]
    assert [item["source"] for item in items] == expected
    assert [key for key, value in health.items() if value.startswith("ok:")] == expected
    assert len(health) == 5


def test_worker_and_cooldown_numeric_inputs_are_safely_bounded(tmp_path):
    assert _bounded_feed_workers(0) == 1
    assert _bounded_feed_workers(10**9) == 32
    assert _bounded_feed_workers(float("inf")) == 4
    assert _bounded_feed_workers(True) == 4

    defaulted = FeedHealthStore(tmp_path / "defaulted", cooldown_hours=float("inf"))
    bounded = FeedHealthStore(tmp_path / "bounded", cooldown_hours=10**100)
    assert defaulted.cooldown == timedelta(hours=72)
    assert bounded.cooldown == timedelta(days=365)


def test_extreme_numeric_state_fails_open_instead_of_crashing(tmp_path):
    source = {"name": "feed", "url": "https://feed.example/rss"}
    store = FeedHealthStore(tmp_path / "shared")
    fingerprint = source_fingerprint(source)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        '{"version":1,"sources":{"' + fingerprint
        + '":{"consecutive_failures":1e999,"generation":1e999}}}',
        encoding="utf-8",
    )

    attempt = store.claim(source)
    assert attempt.mode == "fetch" and attempt.generation == 1
    entry = json.loads(store.path.read_text())["sources"][fingerprint]
    assert entry == {"consecutive_failures": 0, "generation": 1}


def test_feed_health_cooldown_restart_half_open_and_fingerprint(tmp_path):
    clock = _Clock()
    shared = tmp_path / "shared"
    source = {"name": "Private feed",
              "url": "https://api.example/rss?token=super-secret", "lang": "en"}
    store = FeedHealthStore(shared, cooldown_hours=72, now=clock)

    for generation in range(1, 4):
        attempt = store.claim(source)
        assert attempt.mode == "fetch" and attempt.generation == generation
        store.record_failure(attempt)

    # A fresh process/store observes the same durable quarantine.
    restarted = FeedHealthStore(shared, cooldown_hours=72, now=clock)
    cooling = restarted.claim(source)
    assert cooling.mode == "cooling"
    persisted = store.path.read_text()
    assert "api.example" not in persisted
    assert "super-secret" not in persisted
    assert "Private feed" not in persisted
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600

    # Route/config changes get fresh fingerprints and bypass immediately.
    changed_url = {**source, "url": "https://api.example/v2/rss"}
    changed_config = {**source, "lang": "zh"}
    assert restarted.claim(changed_url).mode == "fetch"
    assert restarted.claim(changed_config).mode == "fetch"
    assert restarted.claim(source).mode == "cooling"

    # Policy is part of the opaque scope too: tenants with different positive
    # cooldowns or future threshold policy cannot mutate one another's state.
    short_policy = FeedHealthStore(shared, cooldown_hours=24, now=clock)
    threshold_policy = FeedHealthStore(
        shared, cooldown_hours=72, now=clock, failure_threshold=4,
    )
    assert short_policy.claim(source).mode == "fetch"
    assert threshold_policy.claim(source).mode == "fetch"
    assert short_policy.claim(source).fingerprint != cooling.fingerprint
    assert threshold_policy.claim(source).fingerprint != cooling.fingerprint

    # At the boundary, the lock admits exactly one half-open probe even when
    # several freshly constructed stores race as independent processes would.
    clock.advance(hours=72)
    stores = [FeedHealthStore(shared, cooldown_hours=72, now=clock)
              for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(pool.map(lambda health: health.claim(source), stores))
    assert [a.mode for a in attempts].count("probe") == 1
    assert [a.mode for a in attempts].count("skipped") == 7

    probe = next(a for a in attempts if a.mode == "probe")
    restarted.record_failure(probe)
    assert restarted.claim(source).mode == "cooling"
    clock.advance(hours=72)
    recovered = restarted.claim(source)
    assert recovered.mode == "probe"
    restarted.record_success(recovered)
    assert restarted.claim(source).mode == "fetch"


def test_real_processes_admit_only_one_half_open_probe(tmp_path):
    clock = _Clock()
    shared = tmp_path / "shared"
    source = {"name": "feed", "url": "https://feed.example/rss"}
    store = FeedHealthStore(shared, cooldown_hours=72, now=clock)
    for _ in range(3):
        attempt = store.claim(source)
        store.record_failure(attempt)
    clock.advance(hours=72)

    payload = (str(shared), source, clock.value.isoformat())
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=6, mp_context=context) as executor:
        futures = [executor.submit(_claim_in_process, payload) for _ in range(6)]
        modes = [future.result(timeout=15) for future in futures]

    assert modes.count("probe") == 1
    assert modes.count("skipped") == 5


def test_older_fetch_failure_cannot_regress_newer_success(tmp_path):
    """A late ordinary result must be generation-guarded like a probe result."""
    store = FeedHealthStore(tmp_path / "shared", now=_Clock())
    source = {"name": "feed", "url": "https://feed.example/rss"}
    older = store.claim(source)
    newer = store.claim(source)
    assert newer.generation > older.generation

    store.record_success(newer)
    store.record_failure(older)  # completes late, after the newer success

    state = json.loads(store.path.read_text())
    entry = state["sources"][older.fingerprint]
    assert entry == {"consecutive_failures": 0,
                     "generation": newer.generation}
    following = store.claim(source)
    assert following.mode == "fetch"
    assert following.generation == newer.generation + 1


def test_gather_health_distinguishes_failure_cooling_probe_and_skip(
        settings, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(feed_health, "_utc_now", clock)
    source = {"url": "https://feed.example/rss?token=do-not-print", "lang": "en"}
    _write_sources(settings.sources_file, [source])
    config = SimpleNamespace(
        sources_file=settings.sources_file,
        shared_dir=settings.shared_dir,
        research_feed_workers=1,
        research_feed_cooldown_hours=72,
    )
    attempts = 0

    def fail(_url):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("https://feed.example/rss?token=do-not-print")

    monkeypatch.setattr(feeds, "fetch_feed", fail)
    for _ in range(3):
        health = {}
        assert _gather_feed_items(config, health) == []
        assert health == {"feed 1": "FAILED: fetch; TimeoutError"}

    health = {}
    assert _gather_feed_items(config, health) == []
    assert attempts == 3
    assert "cooling" in health["feed 1"] and "skipped" in health["feed 1"]
    assert "feed.example" not in str(health)

    clock.advance(hours=72)
    health = {}
    _gather_feed_items(config, health)
    assert attempts == 4
    assert health == {"feed 1": "FAILED: probe; TimeoutError"}

    # Claim the next expired probe without completing it; a concurrent gather
    # must report a skip and make no duplicate HTTP attempt.
    clock.advance(hours=72)
    held = FeedHealthStore(settings.shared_dir, now=clock).claim(source)
    assert held.mode == "probe"
    health = {}
    _gather_feed_items(config, health)
    assert attempts == 4
    assert "skipped" in health["feed 1"] and "probe" in health["feed 1"]


def test_workers_one_cooldown_zero_is_serial_stateless_rollback(
        settings, monkeypatch):
    source = {"name": "source", "url": "https://feed.example/rss"}
    _write_sources(settings.sources_file, [source])
    config = SimpleNamespace(
        sources_file=settings.sources_file,
        shared_dir=settings.shared_dir,
        research_feed_workers=1,
        research_feed_cooldown_hours=0,
    )
    caller_thread = threading.get_ident()
    seen_threads = []

    def fail(_url):
        seen_threads.append(threading.get_ident())
        raise TimeoutError("down")

    monkeypatch.setattr(feeds, "fetch_feed", fail)
    for _ in range(4):
        health = {}
        assert _gather_feed_items(config, health) == []
        assert health["source"] == "FAILED: fetch; TimeoutError"

    assert seen_threads == [caller_thread] * 4
    assert not (settings.shared_dir / "research-feed-health.json").exists()


def test_cooldown_zero_temporarily_bypasses_but_preserves_prior_state(tmp_path):
    clock = _Clock()
    shared = tmp_path / "shared"
    source = {"name": "source", "url": "https://feed.example/rss"}
    enabled = FeedHealthStore(shared, cooldown_hours=72, now=clock)
    for _ in range(3):
        attempt = enabled.claim(source)
        enabled.record_failure(attempt)
    before = enabled.path.read_bytes()
    assert enabled.claim(source).mode == "cooling"

    bypass = FeedHealthStore(shared, cooldown_hours=0, now=clock)
    successful_fetch = bypass.claim(source)
    assert successful_fetch.mode == "fetch"
    bypass.record_success(successful_fetch)
    assert enabled.path.read_bytes() == before

    # The same positive policy resumes its old state after the temporary bypass.
    assert FeedHealthStore(shared, cooldown_hours=72, now=clock).claim(source).mode == \
        "cooling"
