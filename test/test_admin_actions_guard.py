"""Shared/admin actions (e.g. `reboot`) are refused to tenants in multi_tenant —
on both the chat-action path and direct /actions dispatch — and never even
*advertised* in tenant prompts (multi-user §10)."""
import pytest

from assistant.actions import prompt_block, run_action
from assistant.actions.registry import execute
from assistant.chat.agent import system_prompt
from assistant.config import Settings


def _mt(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return Settings.for_user("alice1")


def test_reboot_refused_via_run_action_in_multi_tenant(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="admin-only"):
        run_action("reboot", {}, _mt(tmp_path, monkeypatch))


def test_reboot_refused_via_chat_actions_in_multi_tenant(tmp_path, monkeypatch):
    out = execute([{"type": "reboot"}], _mt(tmp_path, monkeypatch))
    assert len(out) == 1 and "admin-only" in out[0]


def test_reboot_allowed_in_single_user(settings, monkeypatch):
    # single_user (one owner) keeps reboot as a normal action — just don't spawn
    from assistant.actions import handlers
    monkeypatch.setattr(handlers.subprocess, "Popen", lambda *a, **k: None)
    assert "restart" in run_action("reboot", {}, settings).lower() \
        or "重启" in run_action("reboot", {}, settings)


# ── the prompt must not advertise what dispatch refuses (§10) ─────────────

def test_prompt_block_omits_admin_actions_for_tenants(settings, tmp_path, monkeypatch):
    assert '"reboot"' in prompt_block()                 # legacy no-arg = full set
    assert '"reboot"' in prompt_block(settings)         # single_user unchanged
    assert '"reboot"' not in prompt_block(_mt(tmp_path, monkeypatch))
    # only the admin action disappears — per-user actions stay advertised
    assert '"trigger_run"' in prompt_block(_mt(tmp_path, monkeypatch))


def test_system_prompt_is_mode_aware(settings, tmp_path, monkeypatch):
    single = system_prompt(settings)
    # single_user: today's prompt — reboot instruction + example, JSON shape intact
    assert "emit\nreboot" in single and '{"type": "reboot"}' in single
    assert '{"reply": "<chat reply>", "actions": []}' in single  # template render kept literal braces

    mt = system_prompt(_mt(tmp_path, monkeypatch))
    # multi_tenant: no reboot instruction, no reboot example — admin note instead
    assert "emit\nreboot" not in mt and '"type": "reboot"' not in mt
    assert "admin-only" in mt
    assert '{"reply": "<chat reply>", "actions": []}' in mt


def test_task_runner_system_prompt_is_mode_aware(settings, tmp_path, monkeypatch):
    from assistant.task_runner import run_task

    class CaptureLLM:
        def __init__(self):
            self.systems = []

        def complete_json(self, prompt, system=None, **kw):
            self.systems.append(system)
            return {"finish": "done", "thought": "x"}

    llm = CaptureLLM()
    run_task("check something", _mt(tmp_path, monkeypatch), llm=llm, notify=False)
    assert llm.systems and '"reboot"' not in llm.systems[0]   # tenant task: not advertised

    llm2 = CaptureLLM()
    run_task("check something", settings, llm=llm2, notify=False)
    assert llm2.systems and '"reboot"' in llm2.systems[0]     # single_user: unchanged
