"""The self-improve evidence extractor — mode-aware data roots (multi-user
§12b layer 3): per-active-user sections in multi_tenant, root in single_user,
empty output on a quiet window (the harness's skip contract)."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "self_improve_evidence.py"


def _run(env_extra, days="7"):
    import os
    env = dict(os.environ, **env_extra)
    home = Path(env["HOME"]).resolve()
    data_dir = Path(env["DATA_DIR"]).resolve()
    assert data_dir.is_relative_to(home), (data_dir, home)
    out = subprocess.run([sys.executable, str(SCRIPT), days],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _scratch(tmp_path, mode="single_user"):
    """A subprocess HOME whose entire agent data tree stays test-local."""
    home = tmp_path / "home"
    data_dir = home / ".personal-agent"
    data_dir.mkdir(parents=True)
    assert data_dir.resolve().is_relative_to(home.resolve())
    return {"HOME": str(home), "DATA_DIR": str(data_dir),
            "DEPLOYMENT_MODE": mode}, data_dir


def _seed_friction(data_dir: Path):
    (data_dir / "sessions").mkdir(parents=True, exist_ok=True)
    (data_dir / "sessions" / "s.json").write_text(json.dumps({"turns": [
        {"ts": datetime.now().isoformat(), "owner": "别再搞错时间",
         "assistant": "rejected — couldn't parse"}]}))


def test_multi_tenant_reads_each_active_user(tmp_path):
    from assistant.platform.registry import UserRegistry

    env, data_dir = _scratch(tmp_path, "multi_tenant")
    reg = UserRegistry(data_dir)
    reg.add_user("alice1")
    reg.add_user("bob123")
    reg.add_user("carol1")
    reg.set_status("carol1", "disabled")
    _seed_friction(data_dir / "users" / "alice1")
    _seed_friction(data_dir / "users" / "bob123")
    _seed_friction(data_dir / "users" / "carol1")   # disabled → must not appear
    out = _run(env)
    assert "## user alice1" in out and "## user bob123" in out
    assert "carol1" not in out
    assert "friction" in out


def test_single_user_reads_root_without_user_sections(tmp_path):
    env, data_dir = _scratch(tmp_path)
    _seed_friction(data_dir)
    out = _run(env)
    assert "friction" in out and "## user" not in out


def test_quiet_window_prints_nothing(tmp_path):
    env, _ = _scratch(tmp_path)
    out = _run(env)
    assert out == ""                                 # harness skips the Opus call


def test_shell_harness_syntax():
    script = SCRIPT.parent / "self-improve.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


def test_structured_labels_beat_keyword_heuristics(tmp_path):
    env, data_dir = _scratch(tmp_path)
    (data_dir / "sessions").mkdir(parents=True)
    (data_dir / "sessions" / "s.json").write_text(json.dumps({"turns": [
        # fail-labeled, clean text → surfaces as friction
        {"ts": datetime.now().isoformat(), "owner": "查一下天气",
         "assistant": "我现在查不到呢", "outcome": "fail"},
        # success-labeled turn mentioning "failed" → suppressed
        {"ts": datetime.now().isoformat(), "owner": "总结CI",
         "assistant": "the build failed twice, summary sent", "outcome": "success"},
        # owner verdict dissatisfied → friction even though labeled success
        {"ts": datetime.now().isoformat(), "owner": "hmm ok",
         "assistant": "done", "outcome": "success", "owner_verdict": "dissatisfied"},
    ]}))
    out = _run(env)
    assert "查不到" in out and "[friction]" in out
    assert "summary sent" not in out
    assert "done" in out


def test_session_recency_compares_aware_and_naive_instants(tmp_path):
    env, data_dir = _scratch(tmp_path)
    now_utc = datetime.now(timezone.utc)
    local_now = datetime.now()
    far_east = timezone(timedelta(hours=14))
    far_west = timezone(-timedelta(hours=12))
    turns = [
        {"ts": (now_utc - timedelta(hours=1)).astimezone(far_west).isoformat(),
         "owner": "RECENT-AWARE", "assistant": "failed"},
        {"ts": (local_now - timedelta(hours=1)).isoformat(),
         "owner": "RECENT-NAIVE", "assistant": "failed"},
        {"ts": (now_utc - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
         "owner": "RECENT-Z", "assistant": "failed"},
        {"ts": (now_utc - timedelta(days=30)).astimezone(far_east).isoformat(),
         "owner": "OLD-AWARE", "assistant": "failed"},
        {"ts": (local_now - timedelta(days=30)).isoformat(),
         "owner": "OLD-NAIVE", "assistant": "failed"},
        {"ts": "not-a-timestamp", "owner": "MALFORMED", "assistant": "failed"},
        {"owner": "MISSING-LEGACY", "assistant": "failed"},
    ]
    (data_dir / "sessions").mkdir(parents=True)
    (data_dir / "sessions" / "s.json").write_text(json.dumps({"turns": turns}))

    out = _run(env, days="7")
    for marker in ("RECENT-AWARE", "RECENT-NAIVE", "RECENT-Z", "MISSING-LEGACY"):
        assert marker in out
    for marker in ("OLD-AWARE", "OLD-NAIVE", "MALFORMED"):
        assert marker not in out


def _seed_run(data_dir: Path, name: str, spans: list[dict], *, digest=None,
              research=None) -> Path:
    run = data_dir / "runs" / name
    run.mkdir(parents=True)
    (run / "trace.jsonl").write_text(
        "\n".join(json.dumps(span) for span in spans) + "\n")
    if digest is not None:
        (run / "digest.json").write_text(json.dumps(digest))
    if research is not None:
        (run / "research.json").write_text(json.dumps(research))
    return run


def test_trace_evidence_uses_timestamps_and_reports_aggregate_health(tmp_path):
    env, data_dir = _scratch(tmp_path)
    now = datetime.now().timestamp()
    old = (datetime.now() - timedelta(days=30)).timestamp()

    from assistant.agent.events_store import EventsStore

    events = EventsStore(data_dir / "events.db")
    events.record_metrics("moa-now", "moa", {
        "members_total": 2, "members_attempted": 1, "members_skipped": 1,
        "members_failed": 0, "proposals_ok": 1, "proposals_final": 1,
        "aggregator_attempted": 0, "aggregator_skipped": 1,
        "aggregator_failed": 0, "aggregator_ok": 0, "degraded": 1,
        "fallback_used": 1, "abandoned": 0,
    })
    events.close()

    # A lexically ancient directory with recent spans must be included. Artifact
    # strings are deliberately sensitive-looking: aggregate evidence must never
    # copy owner titles, summaries, URLs, prompts, or item bodies into the brief.
    _seed_run(data_dir, "run-20000101-000000", [
        {"t": "span", "name": "llm", "start": now - 80, "end": now - 20,
         "dur_ms": 60_000, "attr": {"model": "model-a",
                                             "stop_reason": "max_tokens"}},
        {"t": "span", "name": "llm", "start": now - 15, "end": now - 5,
         "dur_ms": 10_000, "attr": {"model": "model-b",
                                             "stop_reason": "end_turn"}},
        {"t": "span", "name": "tool", "start": now - 10, "end": now - 9,
         "dur_ms": 1_000, "attr": {"tool": "x", "error_type": "TimeoutError"}},
        {"t": "span", "name": "mixture", "start": now - 8, "end": now - 4,
         "dur_ms": 4_000, "attr": {"members_total": 2, "proposals_ok": 1,
            "aggregator_ok": 0, "fallback_used": 1, "abandoned": 1}},
    ], digest={
        "total": 10, "overflow": 2, "llm_requested": 7, "llm_triaged": 6,
        "fallback_count": 1,
        "sections": {"red": [{"title": "OWNER-DIGEST-SECRET"}]},
    }, research={
        "papers": [
            {"title": "OWNER-PAPER-SECRET", "summary": "ready"},
            {"title": "OWNER-PAPER-SECRET-2"},
        ],
        "industry": [{"title": "OWNER-FEED-SECRET", "takeaway": "ready"}],
        "chinese": [{"title": "OWNER-FEED-SECRET-2"}],
        "score_requested": 10, "score_completed": 8, "score_fallback": 2,
        "summary_requested": 4, "summary_completed": 3, "summary_fallback": 1,
        "source_health": {"private-a": "ok: 3 items",
                          "private-b": "ok: 2 items",
                          "private-c": "FAILED: TimeoutError",
                          "note": "OWNER-HEALTH-SECRET"},
    })

    # A lexically future directory whose recorded spans are old must be excluded;
    # actual trace timestamps outrank the folder name.
    stale_run = _seed_run(data_dir, "run-20990101-000000", [
        {"t": "span", "name": "llm", "start": old, "end": old + 1,
         "dur_ms": 1_000, "attr": {"model": "stale-model",
                                           "stop_reason": "max_tokens"}},
    ], digest={"total": 999, "overflow": 0, "llm_triaged": 0},
       research={"papers": [{"title": "STALE-OWNER-SECRET"}]})
    for name in ("digest.json", "research.json"):
        os.utime(stale_run / name, (old, old))

    out = _run(env, days="7")
    assert "runs=1" in out and "LLM calls=2" in out
    assert "slow (>45s)=1" in out and "truncations=1" in out
    assert "explicit span errors=1" in out
    assert "Durable MoA: calls=1 (instrumented=1, legacy=0)" in out
    assert "Instrumented MoA: member slots attempted=1/2" in out
    assert "aggregators successful=0/1" in out
    assert "skipped=1" in out and "degraded=1" in out
    assert "MoA health:" not in out                    # no trace double count
    assert "LLM-triaged=6/7" in out and "deterministic fallbacks=1" in out
    assert "scored=8/10 (fallbacks=2)" in out
    assert "summarized=3/4 (fallbacks=1)" in out
    assert "Research source health: healthy=2/3" in out
    for secret in ("OWNER-", "private-a", "private-b", "private-c",
                   "stale-model"):
        assert secret not in out


def test_artifacts_use_their_phase_span_or_own_mtime_for_recency(tmp_path):
    env, data_dir = _scratch(tmp_path)
    now = datetime.now().timestamp()
    old = (datetime.now() - timedelta(days=30)).timestamp()

    # A current unrelated phase must not pull stale artifacts from a resumed run
    # into the window. The recent digest phase does admit only its own artifact.
    phase_run = _seed_run(data_dir, "run-20000101-000001", [
        {"t": "span", "name": "phase", "start": now - 2, "end": now - 1,
         "attr": {"phase": "digest"}},
        {"t": "span", "name": "phase", "start": old, "end": old + 1,
         "attr": {"phase": "research"}},
    ], digest={"llm_requested": 2, "llm_triaged": 2, "fallback_count": 0},
       research={"score_requested": 99, "score_completed": 0, "score_fallback": 99,
                 "summary_requested": 99, "summary_completed": 0,
                 "summary_fallback": 99})
    for name in ("digest.json", "research.json"):
        os.utime(phase_run / name, (old, old))

    # A rewritten artifact remains observable even with an otherwise old trace.
    mtime_run = _seed_run(data_dir, "run-20000101-000002", [
        {"t": "span", "name": "phase", "start": old, "end": old + 1,
         "attr": {"phase": "digest"}},
        {"t": "span", "name": "phase", "start": old, "end": old + 1,
         "attr": {"phase": "research"}},
    ], digest={"llm_requested": 3, "llm_triaged": 2, "fallback_count": 1},
       research={"score_requested": 4, "score_completed": 3, "score_fallback": 1,
                 "summary_requested": 2, "summary_completed": 1,
                 "summary_fallback": 1})
    os.utime(mtime_run / "research.json", (old, old))

    out = _run(env, days="7")
    assert "runs=2" in out
    assert "LLM-triaged=4/5" in out and "deterministic fallbacks=1" in out
    assert "scored=" not in out and "Research legacy coverage" not in out


def test_mixed_research_schemas_report_source_health_once(tmp_path):
    env, data_dir = _scratch(tmp_path)
    now = datetime.now().timestamp()
    phase = [{"t": "span", "name": "phase", "start": now - 2,
              "end": now - 1, "attr": {"phase": "research"}}]
    _seed_run(data_dir, "run-20000101-000003", phase, research={
        "score_requested": 2, "score_completed": 1, "score_fallback": 1,
        "summary_requested": 1, "summary_completed": 1, "summary_fallback": 0,
        "source_health": {"a": "ok: 1 item", "b": "FAILED: timeout"},
    })
    _seed_run(data_dir, "run-20000101-000004", phase, research={
        "papers": [{"summary": "ready"}, {}],
        "industry": [{"takeaway": "ready"}],
        "source_health": {"c": "ok: 1 item", "d": "ok: 2 items"},
    })

    out = _run(env, days="7")
    assert out.count("Research source health:") == 1
    assert "Research source health: healthy=3/4; runs=2" in out


def test_durable_moa_surfaces_even_without_run_traces(tmp_path):
    """Interactive-only windows still inform self-improvement via events.db."""
    env, data_dir = _scratch(tmp_path)
    from assistant.agent.events_store import EventsStore

    events = EventsStore(data_dir / "events.db")
    events.record_metrics("moa-chat", "moa", {
        "members_total": 2, "members_attempted": 1, "members_skipped": 1,
        "members_failed": 0, "proposals_ok": 1, "proposals_final": 1,
        "aggregator_attempted": 0, "aggregator_skipped": 1,
        "aggregator_failed": 0, "aggregator_ok": 0, "degraded": 1,
        "fallback_used": 1, "abandoned": 0,
    })
    events.close()

    out = _run(env, days="7")
    assert "Durable MoA: calls=1" in out
    assert "aggregators successful=0/1" in out


def test_legacy_moa_rows_keep_failed_aggregation_denominator(tmp_path):
    """Pre-observability rows must say 0/1, not hide the failure as 0/0."""
    env, data_dir = _scratch(tmp_path)
    from assistant.agent.events_store import EventsStore

    events = EventsStore(data_dir / "events.db")
    events.record_metrics("moa-old", "moa", {
        "members_total": 2, "proposals_ok": 1, "aggregator_ok": 0,
        "fallback_used": 1, "abandoned": 0,
    })
    events.close()

    out = _run(env, days="7")
    assert "calls=1 (instrumented=0, legacy=1)" in out
    assert "aggregators successful=0/1" in out


def test_durable_moa_pivots_interleaved_calls_and_multilayer_slots(tmp_path):
    """Rows are metric-shaped; reports must reconstruct call-shaped records."""
    env, data_dir = _scratch(tmp_path)
    from assistant.agent.events_store import EventsStore

    events = EventsStore(data_dir / "events.db")
    first = datetime.now(timezone.utc).isoformat()
    second = (datetime.now(timezone.utc) + timedelta(microseconds=1)).isoformat()
    instrumented = {
        "members_total": 2, "members_attempted": 4, "members_skipped": 0,
        "members_failed": 1, "proposals_ok": 3, "proposals_final": 1,
        "aggregator_attempted": 1, "aggregator_skipped": 0,
        "aggregator_failed": 0, "aggregator_ok": 1, "degraded": 1,
        "fallback_used": 0, "abandoned": 0,
    }
    legacy = {
        "members_total": 3, "proposals_ok": 2, "aggregator_ok": 0,
        "fallback_used": 1, "abandoned": 0,
    }
    # Deliberately alternate rows from the two calls and reuse the day-keyed
    # run id. Timestamp is the record_metrics batch identity.
    rows = []
    left = list(instrumented.items())
    right = list(legacy.items())
    for index in range(max(len(left), len(right))):
        if index < len(left):
            rows.append(("moa-same-day", first, "moa", *left[index]))
        if index < len(right):
            rows.append(("moa-same-day", second, "moa", *right[index]))
    events.conn.executemany(
        "INSERT INTO metrics (run_id, ts, step, name, value) VALUES (?, ?, ?, ?, ?)",
        rows)
    events.conn.commit()
    events.close()

    out = _run(env, days="7")
    assert "calls=2 (instrumented=1, legacy=1)" in out
    assert "Instrumented MoA: member slots attempted=4/4" in out
    assert "proposer successes=3; final proposals=1" in out
    assert "Legacy MoA (partial schema): proposer successes=2" in out
    assert "aggregators successful=1/2" in out


def test_trace_only_multilayer_moa_has_no_invalid_ratio(tmp_path):
    """Legacy trace fallback must not divide cumulative work by one layer."""
    env, data_dir = _scratch(tmp_path)
    now = datetime.now().timestamp()
    _seed_run(data_dir, "run-20000101-000005", [{
        "t": "span", "name": "mixture", "start": now - 2, "end": now - 1,
        "dur_ms": 1_000,
        "attr": {"members_total": 2, "proposals_ok": 4,
                 "aggregator_ok": 1, "fallback_used": 0, "abandoned": 0},
    }])

    out = _run(env, days="7")
    assert "MoA health: calls=1; proposer successes=4; configured members=2" in out
    assert "proposals=4/2" not in out
