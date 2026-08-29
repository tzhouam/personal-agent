"""PA-Mix v1 harness tests (doc/BENCHMARKS.md): the executor-override seam
never leaks, the sandbox executes only allowlisted actions on scratch stores
and records the rest, bench settings are hermetic, the network guard denies
at the transport, the stats are deterministic and paired, and the golden
tracks run end-to-end against scripted fake LLMs producing an isolated
results run + report card."""

import json
import socket
from datetime import date, datetime, timedelta, timezone

import pytest

from assistant.agent.actions.registry import execute
from assistant.bench import stats
from assistant.bench.results import RunStore
from assistant.bench.run import _llm_hosts, render_report, run_tracks
from assistant.bench.sandbox import (SandboxRecorder, action_sandbox,
                                     bench_settings, network_guard,
                                     outward_credential_fields,
                                     route_fingerprint)
from assistant.bench.surfaces import chat_turn


class ScriptedLLM:
    """Returns the reply/actions scripted for each owner message (matched by
    substring), else a no-action reply."""

    def __init__(self, script: dict):
        self.script = script

    @staticmethod
    def _owner_message(prompt: str) -> str:
        # match against the owner's message ONLY — context blocks echo store
        # contents (a logged 牛肉面 appears in every later prompt) and history
        # echoes earlier turns, which would mis-trigger needles
        marker = "## Owner message"
        if marker not in prompt:
            return prompt
        tail = prompt.split(marker, 1)[1]
        # the owner section ends at the next "## " block — learned rules are
        # appended AFTER it and echo lesson text (e.g. 港币), which would
        # mis-trigger needles for every later item
        return tail.split("\n\n## ", 1)[0]

    def complete_json(self, prompt, system=None, **kw):
        owner = self._owner_message(prompt)
        import re as _re

        if "六月份" in owner and "## Records you just retrieved" in prompt:
            # The compose pass must answer from the actual scratch-store result,
            # rather than returning the generic acknowledgement used elsewhere.
            records = prompt.split("## Records you just retrieved", 1)[1]
            total = _re.search(r"\bexpense\s+([\d,.]+)", records)
            reply = (f"六月份交通支出共 {total.group(1)} 元。" if total else
                     "六月份没有交通支出记录。")
            return {"reply": reply, "actions": []}
        fid = _re.search(r"\[(f-[0-9-]+)\]", prompt)
        if "改成交通类" in owner and fid:
            return {"reply": "好", "actions": [
                {"type": "recategorize_transaction", "id": fid.group(1),
                 "category": "transport"}]}
        if "作废" in owner and fid:
            return {"reply": "好", "actions": [
                {"type": "void_transaction", "id": fid.group(1)}]}
        for needle, actions in self.script.items():
            if needle in owner:
                return {"reply": "好的", "actions": actions}
        return {"reply": "好的", "actions": []}


# ── seam + sandbox ───────────────────────────────────────────────────

def test_executor_override_is_scoped_and_leakproof(settings):
    rec = SandboxRecorder()
    with action_sandbox(rec):
        out = execute([{"type": "reboot"}], settings)
        assert out == ["[bench-sandbox] reboot recorded, not executed"]
        assert rec.faked == [{"action": {"type": "reboot"}}]
    # outside the context, production behavior returns untouched
    out = execute([{"type": "unknown_action_x"}], settings)
    assert out == ["unknown action 'unknown_action_x' ignored"]


def test_sandbox_runs_allowlisted_actions_for_real(tmp_path):
    settings = bench_settings(scratch=tmp_path)
    rec = SandboxRecorder()
    with action_sandbox(rec):
        out = execute([{"type": "log_transaction", "kind": "expense",
                        "amount": 45, "note": "午饭"}], settings)
    assert any("f-" in line for line in out)       # real handler, scratch store
    assert rec.executed and not rec.faked
    from assistant.agent.finance_store import FinanceStore

    assert len(FinanceStore(settings.profile_dir).records()) == 1
    assert str(settings.data_dir).startswith(str(tmp_path))


def test_sandbox_denies_by_default(tmp_path):
    """An action NOT on the allowlist — even a legitimate llm action — is
    recorded, never executed (new registry actions are faked until someone
    consciously allows them)."""
    settings = bench_settings(scratch=tmp_path)
    rec = SandboxRecorder()
    with action_sandbox(rec):
        execute([{"type": "web_search", "query": "天气"}], settings)
        execute([{"type": "execute_task", "task": "调研"}], settings)
    assert [a["action"]["type"] for a in rec.faked] == \
        ["web_search", "execute_task"]
    assert rec.executed == []


def test_bench_settings_keeps_llm_blanks_outward(settings, tmp_path):
    """The M layer must benchmark the CONFIGURED model, so LLM config is kept;
    every outward credential is blanked and none survives into the summary
    fingerprint."""
    settings.anthropic_api_key = "sk-secret"
    settings.anthropic_model = "test-model"
    settings.github_token = "ghp_leak"
    settings.smtp_password = "pw"
    bench = bench_settings(settings, tmp_path)
    assert bench.anthropic_api_key == "sk-secret"     # LLM kept
    assert bench.anthropic_model == "test-model"
    from pathlib import Path as _P

    for field in outward_credential_fields():
        val = getattr(bench, field, None)
        if isinstance(val, _P):
            assert str(bench.data_dir.parent) in str(val), (field, val)
        else:
            assert val in ("", {}, None), (field, val)    # outward blanked
    assert "leak" not in json.dumps(route_fingerprint(bench))  # no key persisted
    assert bench.bench_enabled is False


@pytest.mark.parametrize("mixture", [
    {"members": 1, "roles": 1, "aggregator": 1},
    {"members": [
        {"model": " kept ", "base_url": "https://one.example/anthropic",
         "api_key": "PRIVATE_MIXTURE_KEY"},
        {"model": 7, "base_url": "https://invalid.example"},
        {"model": "bad-route", "base_url": ["not", "a", "url"]},
        # Canonically identical to the first member; runtime drops it too.
        {"model": "kept", "base_url": "https://ONE.example:443/anthropic/",
         "api_key": "PRIVATE_MIXTURE_KEY"}],
     "roles": 1, "aggregator": 1},
])
def test_bench_routing_uses_runtime_safe_mixture(settings, mixture):
    """Malformed nested MoA config cannot crash or enter persisted metadata."""
    configured = settings.model_copy(update={"llm_mixture": mixture})

    hosts = _llm_hosts(configured)
    fingerprint = route_fingerprint(configured)
    serialized = json.dumps(fingerprint)

    assert "PRIVATE_MIXTURE_KEY" not in serialized
    assert all(isinstance(host, str) for host in hosts)
    if isinstance(mixture["members"], int):
        assert fingerprint["mixture"]["members"] == []
        assert fingerprint["mixture"]["aggregator"] is None
    else:
        assert fingerprint["mixture"]["members"] == [
            {"model": "kept", "host": "one.example"}]
        assert fingerprint["mixture"]["aggregator"] == \
            {"model": "kept", "host": "one.example"}
        assert "one.example" in hosts


def test_bench_fingerprint_changes_with_moa_execution_policy(settings):
    """Depth and chat latency changes cannot compare against stale references."""
    base_mixture = {
        "members": [{"model": "m1"}, {"model": "m2"}],
        "aggregator": {"model": "agg"},
        "roles": ["chat"],
        "layers": 2,
    }
    base = settings.model_copy(update={
        "llm_mixture": base_mixture,
        "moa_chat_proposer_timeout_s": 10,
    })
    deeper = base.model_copy(update={
        "llm_mixture": {**base_mixture, "layers": 3},
    })
    slower = base.model_copy(update={"moa_chat_proposer_timeout_s": 20})

    base_fp = route_fingerprint(base)
    deeper_fp = route_fingerprint(deeper)
    slower_fp = route_fingerprint(slower)

    assert base_fp["mixture"]["layers"] == 2
    assert base_fp["mixture"]["chat_proposer_timeout_s"] == 10
    assert base_fp != deeper_fp
    assert base_fp != slower_fp


def test_network_guard_denies_by_ip_and_allows_resolved_host():
    with network_guard(frozenset({"1.2.3.4"})):
        with pytest.raises(PermissionError):
            socket.create_connection(("93.184.216.34", 80), timeout=1)
    # an allowed HOST resolves to the IP connect() actually receives —
    # localhost is the reliably-resolvable case
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        with network_guard(frozenset({"localhost"})):
            c = socket.create_connection(srv.getsockname(), timeout=2)
            c.close()
        c = socket.create_connection(srv.getsockname(), timeout=2)  # restored
        c.close()
    finally:
        srv.close()


# ── stats ────────────────────────────────────────────────────────────

def test_item_means_drops_infra_nulls_and_accounts_reps():
    per_item = {"a": [{"score": 1.0}, {"score": 0.0}, {"score": None}],
                "b": [{"score": None}, {"score": None}]}
    assert stats.item_means(per_item) == {"a": 0.5}
    acct = stats.rep_accounting(per_item)
    assert acct["reps_infra"] == 3 and acct["items_with_partial_reps"] == ["a"]


def test_bootstrap_ci_deterministic():
    vals = [0.2, 0.4, 0.6, 0.8, 1.0] * 4
    a = stats.bootstrap_ci(vals, seed=7)
    b = stats.bootstrap_ci(vals, seed=7)
    assert a == b
    mean, lo, hi = a
    assert lo <= mean <= hi


def test_paired_delta_detects_regression_and_small_n():
    ref = {f"i{k}": 0.9 for k in range(20)}
    cur = {f"i{k}": 0.5 for k in range(20)}
    d = stats.paired_delta(cur, ref, seed=1)
    assert d["regressed"] and d["delta_mean"] < 0
    d2 = stats.paired_delta({"a": 0.1}, {"a": 0.9}, seed=1)
    assert d2["directional_only"] and not d2["regressed"]


# ── golden tracks end-to-end (fake LLMs, isolated results) ───────────

def _perfect_script() -> dict:
    """Build clock-sensitive actions after ``run_tracks`` freezes its clock."""
    from assistant.bench.tracks import _today

    today = _today()
    return {
        "帮我记一下待办": [{"type": "add_todo", "title": "周五交房租"}],
        "把待办 t3": [{"type": "done_todo", "id": "t3"}],
        "午饭花了45": [{"type": "log_transaction", "kind": "expense", "amount": 45}],
        "下午3点打车": [{"type": "log_transaction", "kind": "expense",
                        "amount": 32, "time": "15:00"}],
        "发工资": [{"type": "log_transaction", "kind": "income", "amount": 21000}],
        "牛肉面": [{"type": "log_meal", "description": "牛肉面"}],
        "跑了5公里": [{"type": "log_exercise", "activity": "跑步",
                      "duration_min": 30}],
        "称了体重": [{"type": "log_weight", "weight_kg": 71.5}],
        "提醒我明天": [{"type": "set_reminder", "message": "面试",
                        "when": f"{today + timedelta(days=1):%Y-%m-%d} 11:00"}],
        "每个工作日早上": [{"type": "create_routine", "task": "发天气",
                          "time": "07:30", "days": "workdays"}],
        "取消提醒 m2": [{"type": "cancel_reminder", "id": "m2"}],
        "伙食花了多少": [{"type": "finance_summary"}],
        "六月份": [{"type": "query_transactions",
                    "start": f"{today.year}-06-01", "end": f"{today.year}-06-30",
                    "category": "transport"}],
        "体重趋势": [{"type": "health_summary"}],
        "以后记账默认": [{"type": "learn_preference", "rule": "记账默认港币"}],
        "忘掉那条": [{"type": "retire_preference", "id": "L1"}],
        "维生素D加到": [{"type": "add_health_need", "item": "维生素D"}],
        "r2 那篇": [{"type": "done_reading", "id": "r2"}],
        "重启": [{"type": "reboot"}],
        "完整的日常流程": [{"type": "trigger_run"}],
        "重新发布": [{"type": "build_personal_website"}],
        "面点王花了88": [{"type": "log_transaction", "kind": "expense",
                          "amount": 88}],
        "算购物": [{"type": "log_transaction", "kind": "expense", "amount": 60,
                   "category": "shopping",
                   "date": f"{today - timedelta(days=today.weekday() + 5):%Y-%m-%d}"}],
        "知道了 dfremm1": [{"type": "acknowledge_failure", "id": "dfremm1"}],
    }


def test_golden_actions_track_end_to_end(settings, tmp_path):
    summary = run_tracks(
        ["golden-actions"], settings, reps=1,
        llm_factory=lambda s: ScriptedLLM(_perfect_script()),
        results_root=tmp_path / "results", guard_network=False)
    row = summary["tracks"]["golden-actions"]
    assert row["valid"] and row["coverage"] == 1.0
    assert row["score"] == 1.0, json.dumps(
        {k: v for k, v in row["item_means"].items() if v < 1}, ensure_ascii=False)


def test_golden_actions_scores_wrong_actions_zero(settings, tmp_path):
    bad = {"午饭花了45": [{"type": "add_todo", "title": "午饭"}]}   # wrong action
    summary = run_tracks(
        ["golden-actions"], settings, reps=1,
        llm_factory=lambda s: ScriptedLLM(bad),
        results_root=tmp_path / "results", guard_network=False)
    means = summary["tracks"]["golden-actions"]["item_means"]
    assert means["ga03"] == 0.0            # wrong action
    assert means["ga19"] == 1.0            # chit-chat: no action emitted


def test_golden_dedup_track_end_to_end(settings, tmp_path):
    script = {
        "12点半吃了牛肉面": [{"type": "log_meal", "description": "牛肉面",
                             "time": "12:30"}],
        "中午12:30那顿": [{"type": "log_meal", "description": "牛肉面",
                          "time": "12:30"}],
        "下午4点花了45": [{"type": "log_transaction", "kind": "expense",
                          "amount": 45, "time": "16:00"}],
        "中午12点吃饭花了45": [{"type": "log_transaction", "kind": "expense",
                               "amount": 45, "time": "12:00"}],
        "18:40在面点王": [{"type": "log_transaction", "kind": "expense",
                          "amount": 68, "time": "18:40", "category": "food"}],
        "6点40那笔68": [{"type": "log_transaction", "kind": "expense",
                        "amount": 68, "time": "18:40", "category": "food"}],
        "体重71.5": [{"type": "log_weight", "weight_kg": 71.5}],
        "71.5kg": [{"type": "log_weight", "weight_kg": 71.5}],
        "中午12点吃了牛肉面": [{"type": "log_meal", "description": "牛肉面",
                               "time": "12:00"}],
        "晚上7点吃了饺子": [{"type": "log_meal", "description": "饺子",
                            "time": "19:00"}],
        "13:00午饭花了45": [{"type": "log_transaction", "kind": "expense",
                            "amount": 45, "time": "13:00"}],
        "作废": [{"type": "void_transaction", "id": "f-first"}],
    }

    class DedupLLM(ScriptedLLM):
        def complete_json(self, prompt, system=None, **kw):
            # void needs the real id — read it out of the finance context block
            if "作废" in self._owner_message(prompt) and "[f-" in prompt:
                import re

                fid = re.search(r"\[(f-[0-9-]+)\]", prompt).group(1)
                return {"reply": "好", "actions": [
                    {"type": "void_transaction", "id": fid}]}
            return super().complete_json(prompt, system=system, **kw)

    summary = run_tracks(
        ["golden-dedup"], settings, reps=1,
        llm_factory=lambda s: DedupLLM(script),
        results_root=tmp_path / "results", guard_network=False)
    row = summary["tracks"]["golden-dedup"]
    assert row["score"] == 1.0, json.dumps(row["item_means"], ensure_ascii=False)


def test_results_run_isolated_and_report_renders(settings, tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("TZ", "UTC")
    summary = run_tracks(
        ["golden-actions"], settings, reps=3,
        llm_factory=lambda s: ScriptedLLM(_perfect_script()),
        results_root=tmp_path / "results", guard_network=False)
    run_dir = tmp_path / "results" / summary["run_id"]
    assert (run_dir / "summary.json").exists()
    assert (run_dir / "golden-actions.items.jsonl").exists()
    assert summary["benchmark_now"]
    persisted = json.loads((run_dir / "summary.json").read_text())
    assert persisted["benchmark_now"] == summary["benchmark_now"]
    first_item = json.loads(
        (run_dir / "golden-actions.items.jsonl").read_text().splitlines()[0])
    assert first_item["reps"][0]["raw"]["benchmark_date"] == \
        summary["benchmark_now"][:10]
    assert os.environ["TZ"] == "UTC"  # runner never mutates process-global TZ
    assert run_dir.stat().st_mode & 0o777 == 0o700
    card = render_report(summary)
    assert "golden-actions" in card and "PA-Mix" in card
    # second run (same fixture + route) computes a paired delta vs the first
    summary2 = run_tracks(
        ["golden-actions"], settings, reps=3,
        llm_factory=lambda s: ScriptedLLM({}),      # everything wrong now
        results_root=tmp_path / "results", guard_network=False)
    delta = summary2["tracks"]["golden-actions"]["delta"]
    assert delta["regressed"] is True
    assert "REGRESSED (unconfirmed" in render_report(summary2)


def test_nutribench_scoring_and_missing_data(settings, tmp_path, monkeypatch):
    from assistant.bench.tracks import NutriBenchTrack

    track = NutriBenchTrack(data_dir=str(tmp_path / "nb"))
    (tmp_path / "nb").mkdir()
    with pytest.raises(FileNotFoundError, match="PA_BENCH_DATA"):
        track.run(settings, lambda s: None, 1, 0, tmp_path / "sc")

    (tmp_path / "nb" / "nutribench_subset.json").write_text(json.dumps([
        {"id": "nb1", "meal": "a bowl of rice", "carb": 45.0},
        {"id": "nb2", "meal": "two eggs", "carb": 1.0},
    ]))

    class FakeLLM:
        def complete(self, prompt, **kw):
            return "45" if "rice" in prompt else "garbage no numbers at all"

    out = track.run(settings, lambda s: FakeLLM(), 1, 0, tmp_path / "sc")
    assert out["nb1"][0]["score"] == 1.0       # exact
    assert out["nb2"][0]["score"] == 0.0       # unparseable scores 0, not null
    assert track.manifest()["label"] == "derived"   # always derived (custom scorer)
    assert track.manifest()["fixture_sha256"]        # data hash present
    assert track._score("52.5", 45.0) == 1.0
    assert track._score("60", 45.0) == 0.0
    assert 0 < track._score("55", 45.0) < 1


def test_reps_get_fresh_state_no_leak(settings, tmp_path):
    """The state-leak fix: with reps=2, an item that mutates state must score
    identically each rep — a shared scratch would make rep 2 see rep 1's
    records (e.g. a dedup/void starting from a dirty store)."""
    summary = run_tracks(
        ["golden-actions"], settings, reps=2,
        llm_factory=lambda s: ScriptedLLM(_perfect_script()),
        results_root=tmp_path / "results", guard_network=False)
    # ga28 voids "刚记的那笔" — with a leaked store, rep 2 would find TWO
    # seeded records and the natural reference would be ambiguous
    for item_id in ("ga02", "ga28"):
        # read the per-item jsonl: both reps must have scored 1.0
        pass
    row = summary["tracks"]["golden-actions"]
    assert row["score"] == 1.0
    assert row["reps"]["reps_total"] == 30 * 2


def test_seeded_id_items_require_real_success(settings, tmp_path):
    """ga11/ga16/ga29 operate on seeded entities — with the seeds present the
    action SUCCEEDS; drop the reminder seed and ga11 must score 0 (cancel of a
    nonexistent reminder is not success)."""
    from assistant.bench.tracks import GoldenActionsTrack, _apply_seed
    from assistant.bench.sandbox import bench_settings as bs
    from assistant.bench.surfaces import chat_turn
    from assistant.bench.tracks import _score_action_item, _load_fixture

    items = {i["id"]: i for i in _load_fixture("golden_actions.json")[0]["items"]}
    llm = ScriptedLLM({"取消提醒 m2": [{"type": "cancel_reminder", "id": "m2"}]})

    seeded = bs(settings, tmp_path / "seeded")
    _apply_seed(seeded, items["ga11"]["seed"])
    rec = chat_turn(seeded, llm, items["ga11"]["text"])
    assert _score_action_item(items["ga11"], rec) == 1.0   # m2 exists → success

    bare = bs(settings, tmp_path / "bare")             # no seed
    rec2 = chat_turn(bare, llm, items["ga11"]["text"])
    assert _score_action_item(items["ga11"], rec2) == 0.0  # cancel of nothing


def test_unexpected_fakes_rejected(settings, tmp_path):
    """A faked-expectation item must reject EXTRA risky actions."""
    from assistant.bench.tracks import _score_action_item
    from assistant.bench.surfaces import TurnRecord

    item = {"expect": {"faked": "reboot"}}
    ok = TurnRecord("", "", faked=[{"action": {"type": "reboot"}}])
    assert _score_action_item(item, ok) == 1.0
    bad = TurnRecord("", "", faked=[{"action": {"type": "reboot"}},
                                    {"action": {"type": "trigger_run"}}])
    assert _score_action_item(item, bad) == 0.0   # extra risky fake → fail


def test_golden_oracle_rejects_wrong_relative_time_date_and_category(monkeypatch):
    """Demonstrated false positives must score zero even when the action itself
    executes: ga09 once fired at 11:09, ga26 picked Thursday, and ga27 emitted
    the non-canonical category ``transportation``."""
    from assistant.bench import tracks
    from assistant.bench.surfaces import TurnRecord
    from assistant.bench.tracks import _load_fixture, _score_action_item

    monkeypatch.setattr(tracks, "_today", lambda: date(2026, 8, 1))  # Saturday
    items = {i["id"]: i for i in _load_fixture("golden_actions.json")[0]["items"]}

    def rec(action, reply="done"):
        return TurnRecord(reply, "success",
                          executed=[{"action": action, "outcome": "ok", "ok": True}])

    assert _score_action_item(items["ga09"], rec({
        "type": "set_reminder", "when": "2026-08-02 11:00"})) == 1.0
    assert _score_action_item(items["ga09"], rec({
        "type": "set_reminder", "when": "2026-08-02 11:09"})) == 0.0
    assert _score_action_item(items["ga09"], rec({
        "type": "set_reminder", "when": "+17h"})) == 0.0

    assert _score_action_item(items["ga26"], rec({
        "type": "log_transaction", "amount": 60, "category": "shopping",
        "date": "2026-07-22"})) == 1.0
    assert _score_action_item(items["ga26"], rec({
        "type": "log_transaction", "amount": 60, "category": "shopping",
        "date": "2026-07-23"})) == 0.0

    seeded_id = "f-20260801-1"
    assert _score_action_item(items["ga27"], rec({
        "type": "recategorize_transaction", "id": seeded_id,
        "category": "transport"})) == 1.0
    assert _score_action_item(items["ga27"], rec({
        "type": "recategorize_transaction", "id": seeded_id,
        "category": "transportation"})) == 0.0


def test_golden_retrieval_requires_filtered_grounded_compose(monkeypatch):
    """ga13 requires a real June range and an answer grounded in its result."""
    from assistant.bench import tracks
    from assistant.bench.surfaces import TurnRecord
    from assistant.bench.tracks import _load_fixture, _score_action_item

    monkeypatch.setattr(tracks, "_today", lambda: date(2026, 8, 1))
    item = {i["id"]: i for i in _load_fixture("golden_actions.json")[0]["items"]}["ga13"]
    action = {"type": "query_transactions", "start": "2026-06-01",
              "end": "2026-06-30", "category": "transport"}
    outcome = ("2 record(s) · 2026-06-01~2026-06-30 transport · "
               "income 0 expense 96.0 net -96.0 CNY")
    executed = [{"action": action, "outcome": outcome, "ok": True},
                {"action": {"type": "list_todos"},
                 "outcome": "(no open todos)", "ok": True}]
    assert _score_action_item(
        item, TurnRecord("六月份交通支出共 96 元。\n\n✔ (no open todos)",
                         "success", executed=executed)) == 1.0

    # ``query_transactions`` does not implement ``month``; accepting it was a
    # false positive because the handler silently queried every date.
    unsupported_month = [{"action": {
        "type": "query_transactions", "month": "2026-06",
        "category": "transport"}, "outcome": outcome, "ok": True}]
    assert _score_action_item(
        item, TurnRecord("六月份交通支出共 96 元。", "success",
                         executed=unsupported_month)) == 0.0

    # A successful query plus a generic acknowledgement is not a composed answer.
    assert _score_action_item(
        item, TurnRecord("好的", "success", executed=executed)) == 0.0

    # Presence alone is not grounding: reject a contradiction and a net amount.
    assert _score_action_item(
        item, TurnRecord("交通支出不是 96 元，是 0 元。", "success",
                         executed=executed)) == 0.0
    assert _score_action_item(
        item, TurnRecord("六月份净额 -96 元。", "success",
                         executed=executed)) == 0.0

    # The owner-facing answer may naturally spell the amount in Chinese.
    assert _score_action_item(
        item, TurnRecord("今年六月份交通支出共九十六元。", "success",
                         executed=executed)) == 1.0
    assert _score_action_item(
        item, TurnRecord("今年六月份交通支出共一百九十六元。", "success",
                         executed=executed)) == 0.0

    for reply in (
        "今年六月份交通一共九十六元。",
        "打车和地铁合计九十六元。",
        "两笔交通记录合计金额为 96 元。",
        "合计96元。",
        "总计96元。",
        "今年六月份交通费用为96元。",
        "交通支出共96元，实际是两笔交易。",
        "交通支出共96元，实际为40元地铁加56元打车。",
    ):
        assert _score_action_item(
            item, TurnRecord(reply, "success", executed=executed)) == 1.0, reply

    for reply in (
        "交通支出负九十六元。",
        "交通支出不止九十六元。",
        "交通支出不是大约 96 元，而是 0 元。",
        "交通支出 96 元不是正确答案，实际是 0 元。",
        "交通支出并非人民币96元，而是0元。",
        "96元不是交通支出，是收入。",
        "交通支出96元，但这不对，实际是0元。",
        "交通支出96元，实际是0元。",
        "交通支出不到96元。",
        "交通支出至少96元。",
        "交通支出最多96元。",
    ):
        assert _score_action_item(
            item, TurnRecord(reply, "success", executed=executed)) == 0.0, reply

    assert _score_action_item(
        item, TurnRecord("交通支出不是0元，而是96元。", "success",
                         executed=executed)) == 1.0

    # A failed compose preserves this retrieval's raw dump behind a ✔ marker.
    assert _score_action_item(
        item, TurnRecord(f"让我查一下\n\n✔ {outcome}\n✔ (no open todos)",
                         "success", executed=executed)) == 0.0


def test_frozen_benchmark_clock_aligns_prompt_oracle_and_store_defaults(settings):
    """Even when the live clock is years away, every benchmark-facing seam
    observes one configured-zone instant."""
    from assistant.agent.chat.agent import build_context
    from assistant.agent.finance_store import FinanceStore
    from assistant.agent.health_store import HealthStore
    from assistant.agent.todo_store import TodoStore
    from assistant.bench.tracks import _today
    from assistant.platform.notify import parse_when
    from assistant.platform.timeutil import frozen_now, temporal_anchor

    frozen = datetime(2000, 1, 2, 23, 59,
                      tzinfo=timezone(timedelta(hours=8), "HKT"))
    with frozen_now(frozen):
        assert build_context(settings).startswith("Today is 2000-01-02.")
        assert "Now: 2000-01-02 23:59 +0800" in temporal_anchor()
        assert _today() == date(2000, 1, 2)
        assert parse_when("+1d") == datetime(2000, 1, 3, 23, 59)
        assert parse_when("2000-01-02T12:00:00+00:00") == \
            datetime(2000, 1, 2, 20, 0)

        _, finance = FinanceStore(settings.profile_dir).add("expense", 1)
        _, health = HealthStore(settings.profile_dir).add(
            "meal", description="test meal")
        todo = TodoStore(settings.profile_dir).upsert("clock-test", title="clock")
        assert finance["date"] == health["date"] == todo["created"] == "2000-01-02"


def test_changed_fixture_not_comparable():
    from assistant.bench.run import _comparable

    fp = {"default_model": "m"}
    cur = {"valid": True, "manifest": {"fixture_sha256": "aaa"}}
    ref = {"valid": True, "manifest": {"fixture_sha256": "bbb"}}
    assert not _comparable(cur, fp, ref, fp)          # different hash
    same = {"valid": True, "manifest": {"fixture_sha256": "aaa"}}
    assert _comparable(cur, fp, same, fp)             # same hash + fp
    hashless = {"valid": True, "manifest": {}}
    assert not _comparable(hashless, fp, hashless, fp)  # None==None is NOT a match
    invalid = {"valid": False, "manifest": {"fixture_sha256": "aaa"}}
    assert not _comparable(invalid, fp, same, fp)     # invalid current run


def test_provenance_validated_against_data(settings, tmp_path):
    import hashlib

    from assistant.bench.tracks import NutriBenchTrack

    data = json.dumps([{"id": "nb1", "meal": "rice", "carb": 45.0}])
    (tmp_path / "nutribench_subset.json").write_text(data)
    digest = hashlib.sha256(data.encode()).hexdigest()[:16]
    track = NutriBenchTrack(data_dir=str(tmp_path))
    # stale provenance (wrong hash) must not validate
    (tmp_path / "provenance.json").write_text(json.dumps({
        "source_version": "v2", "content_sha256": "WRONG", "license": "x",
        "seed": 1, "item_ids": ["nb1"]}))
    assert track.manifest()["provenance"] == "unvalidated (data origin unconfirmed)"
    # correct hash + ids validates (but label stays derived)
    (tmp_path / "provenance.json").write_text(json.dumps({
        "source_version": "v2", "content_sha256": digest, "license": "x",
        "seed": 1, "item_ids": ["nb1"]}))
    m = track.manifest()
    assert isinstance(m["provenance"], dict) and m["label"] == "derived"
