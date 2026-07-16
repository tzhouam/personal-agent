"""Cross-user global evolve — user-agnostic shared lessons from all active
users' evidence, privacy-filtered (multi-user §12b layer 2)."""
import json

import pytest

from assistant.config import Settings
from assistant.lessons_store import shared_store
from assistant.registry import UserRegistry
from assistant.tasks.global_evolve import _looks_user_specific, global_evolve


class FakeLLM:
    def __init__(self, result=None):
        self.result = result or {"lessons": [], "note": "reviewed"}
        self.prompts = []
        self.systems = []

    def complete_json(self, prompt, system=None, **kw):
        self.prompts.append(prompt)
        self.systems.append(system)
        return self.result


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    """Root settings + registry with two active users (each with evidence) and
    one disabled user."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    reg = UserRegistry(data_dir)
    for uid in ("alice1", "bob123", "carol1"):
        reg.add_user(uid)
    reg.set_status("carol1", "disabled")

    def seed(uid, text):
        d = data_dir / "users" / uid
        (d / "sessions").mkdir(parents=True)
        (d / "sessions" / "s.json").write_text(json.dumps(
            {"session": "x", "turns": [{"ts": "2099-01-01T00:00:00",
                                        "owner": text,
                                        "assistant": "couldn't do that (retry)"}]}))

    seed("alice1", "记一下午饭")
    seed("bob123", "set a reminder")
    seed("carol1", "should never appear")
    # alice also has a recent run trace with a slow + truncated llm span
    run = data_dir / "users" / "alice1" / "runs" / "run-20990101-000000"
    run.mkdir(parents=True)
    (run / "trace.jsonl").write_text("\n".join(json.dumps(s) for s in [
        {"t": "span", "name": "phase", "start": 0, "end": 100, "dur_ms": 100_000,
         "attr": {"phase": "research"}},
        {"t": "span", "name": "llm", "start": 0, "end": 60, "dur_ms": 60_000,
         "attr": {"model": "test-model", "prompt_tokens": 10,
                  "completion_tokens": 5, "stop_reason": "max_tokens"}},
    ]))
    return Settings(_env_file=None), data_dir


def test_gathers_all_active_users_and_traces(deployment):
    root, data_dir = deployment
    llm = FakeLLM({"lessons": [{"rule": "When an action fails, retry once with "
                                        "corrected params before apologizing.",
                                "why": "alice1+bob123 retries"}], "note": "ok"})
    result = global_evolve(root, llm)
    prompt = llm.prompts[0]
    assert "## user alice1" in prompt and "## user bob123" in prompt
    assert "should never appear" not in prompt          # disabled carol excluded
    assert "[FRICTION]" in prompt                       # session friction marker
    assert "test-model" in prompt and "max_tokens" in prompt  # trace evidence
    assert "USER-AGNOSTIC" in llm.systems[0]
    # the lesson landed in the SHARED store with a G id
    assert result["users"] == 2 and len(result["learned"]) == 1
    stored = shared_store(root).active()
    assert stored[0]["id"] == "G1" and stored[0]["source"] == "evolve"
    assert (data_dir / "shared" / "lessons" / "lessons.yaml").exists()


def test_privacy_filter_rejects_user_specific_rules(deployment):
    root, _ = deployment
    reg = UserRegistry(root.data_dir)
    reg.bind_channel("alice1", "email", "alice@example.com")
    llm = FakeLLM({"lessons": [
        {"rule": "Always remind alice1 to hydrate.", "why": "x"},
        {"rule": "Send reports to alice@example.com weekly.", "why": "x"},
        {"rule": "Confirm the date before setting any reminder.", "why": "x"},
    ], "note": "ok"})
    result = global_evolve(root, llm)
    assert result["rejected"] == 2
    rules = [l["rule"] for l in shared_store(root).active()]
    assert rules == ["Confirm the date before setting any reminder."]


def test_looks_user_specific_matches_display_and_channel(deployment):
    root, _ = deployment
    reg = UserRegistry(root.data_dir)
    reg.remove_user("carol1")
    reg.add_user("carol2", display="Xiao Ming")
    assert _looks_user_specific("remind Xiao Ming daily", reg) is True
    assert _looks_user_specific("mentions bob123 explicitly", reg) is True
    assert _looks_user_specific("verify amounts before logging", reg) is False


def test_single_user_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "single_user")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    result = global_evolve(Settings(_env_file=None), FakeLLM())
    assert result == {"reviewed": 0, "users": 0, "proposed": [], "learned": [],
                      "rejected": 0}
    assert not (tmp_path / "shared" / "lessons").exists()


def test_one_broken_user_never_blocks_the_pass(deployment, monkeypatch):
    root, data_dir = deployment
    # alice's sessions dir becomes a FILE → her evidence pass raises
    import shutil
    shutil.rmtree(data_dir / "users" / "alice1" / "sessions")
    shutil.rmtree(data_dir / "users" / "alice1" / "runs")
    (data_dir / "users" / "alice1" / "sessions").write_text("corrupt")
    llm = FakeLLM()
    result = global_evolve(root, llm)
    assert result["users"] == 1                          # bob still reviewed
    assert "## user bob123" in llm.prompts[0]


def test_no_evidence_short_circuits_without_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    UserRegistry(tmp_path).add_user("alice1")
    llm = FakeLLM()
    result = global_evolve(Settings(_env_file=None), llm)
    assert result["users"] == 0 and llm.prompts == []    # no evidence → no call
