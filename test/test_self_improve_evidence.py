"""The self-improve evidence extractor — mode-aware data roots (multi-user
§12b layer 3): per-active-user sections in multi_tenant, root in single_user,
empty output on a quiet window (the harness's skip contract)."""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "self_improve_evidence.py"


def _run(env_extra, days="7"):
    import os
    env = dict(os.environ, **env_extra)
    out = subprocess.run([sys.executable, str(SCRIPT), days],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _seed_friction(data_dir: Path):
    (data_dir / "sessions").mkdir(parents=True, exist_ok=True)
    (data_dir / "sessions" / "s.json").write_text(json.dumps({"turns": [
        {"ts": datetime.now().isoformat(), "owner": "别再搞错时间",
         "assistant": "rejected — couldn't parse"}]}))


def test_multi_tenant_reads_each_active_user(tmp_path):
    from assistant.platform.registry import UserRegistry

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    reg = UserRegistry(data_dir)
    reg.add_user("alice1")
    reg.add_user("bob123")
    reg.add_user("carol1")
    reg.set_status("carol1", "disabled")
    _seed_friction(data_dir / "users" / "alice1")
    _seed_friction(data_dir / "users" / "bob123")
    _seed_friction(data_dir / "users" / "carol1")   # disabled → must not appear
    out = _run({"DEPLOYMENT_MODE": "multi_tenant", "DATA_DIR": str(data_dir)})
    assert "## user alice1" in out and "## user bob123" in out
    assert "carol1" not in out
    assert "friction" in out


def test_single_user_reads_root_without_user_sections(tmp_path):
    data_dir = tmp_path / "data"
    _seed_friction(data_dir)
    out = _run({"DEPLOYMENT_MODE": "single_user", "DATA_DIR": str(data_dir)})
    assert "friction" in out and "## user" not in out


def test_quiet_window_prints_nothing(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    out = _run({"DEPLOYMENT_MODE": "single_user", "DATA_DIR": str(data_dir)})
    assert out == ""                                 # harness skips the Opus call


def test_shell_harness_syntax():
    script = SCRIPT.parent / "self-improve.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


def test_structured_labels_beat_keyword_heuristics(tmp_path):
    data_dir = tmp_path / "data"
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
    out = _run({"DEPLOYMENT_MODE": "single_user", "DATA_DIR": str(data_dir)})
    assert "查不到" in out and "[friction]" in out
    assert "summary sent" not in out
    assert "done" in out
