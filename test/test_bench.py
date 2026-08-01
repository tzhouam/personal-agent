"""PA-Mix v1 harness tests (doc/BENCHMARKS.md): the executor-override seam
never leaks, the sandbox executes only allowlisted actions on scratch stores
and records the rest, bench settings are hermetic, the network guard denies
at the transport, the stats are deterministic and paired, and the golden
tracks run end-to-end against scripted fake LLMs producing an isolated
results run + report card."""

import json
import socket

import pytest

from assistant.agent.actions.registry import execute
from assistant.bench import stats
from assistant.bench.results import RunStore
from assistant.bench.run import render_report, run_tracks
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

_PERFECT_SCRIPT = {
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
    "提醒我明天": [{"type": "set_reminder", "message": "面试", "when": "+1d"}],
    "每个工作日早上": [{"type": "create_routine", "task": "发天气",
                      "time": "07:30", "days": "workdays"}],
    "取消提醒 m2": [{"type": "cancel_reminder", "id": "m2"}],
    "伙食花了多少": [{"type": "finance_summary"}],
    "六月份": [{"type": "query_transactions", "month": "2026-06"}],
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
               "category": "shopping"}],

    "知道了 dfremm1": [{"type": "acknowledge_failure", "id": "dfremm1"}],
}


def test_golden_actions_track_end_to_end(settings, tmp_path):
    summary = run_tracks(
        ["golden-actions"], settings, reps=1,
        llm_factory=lambda s: ScriptedLLM(_PERFECT_SCRIPT),
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


def test_results_run_isolated_and_report_renders(settings, tmp_path):
    summary = run_tracks(
        ["golden-actions"], settings, reps=3,
        llm_factory=lambda s: ScriptedLLM(_PERFECT_SCRIPT),
        results_root=tmp_path / "results", guard_network=False)
    run_dir = tmp_path / "results" / summary["run_id"]
    assert (run_dir / "summary.json").exists()
    assert (run_dir / "golden-actions.items.jsonl").exists()
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
    assert track.manifest()["label"] == "derived"   # no provenance → derived
    assert track._score("52.5", 45.0) == 1.0
    assert track._score("60", 45.0) == 0.0
    assert 0 < track._score("55", 45.0) < 1
