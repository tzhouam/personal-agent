"""`assistant init` wizard + `--check` doctor."""

from pathlib import Path

import assistant.init_wizard as iw
from assistant.init_wizard import (FAIL, OK, SKIP, WARN, probe_email, probe_marks,
                                   probe_search, run_check, run_wizard, upsert_env)


# ── env editing ──────────────────────────────────────────────────────

def test_upsert_env_replaces_uncomments_appends(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# LLM section\nANTHROPIC_API_KEY=old\n# RESEND_API_KEY=\nKEEP=1\n")
    upsert_env(env, "ANTHROPIC_API_KEY", "new")       # replace live line
    upsert_env(env, "RESEND_API_KEY", "rk")           # uncomment template line
    upsert_env(env, "BRAND_NEW", "x")                 # append
    lines = env.read_text().splitlines()
    assert "ANTHROPIC_API_KEY=new" in lines and "old" not in env.read_text()
    assert "RESEND_API_KEY=rk" in lines and "# RESEND_API_KEY=" not in lines
    assert lines[0] == "# LLM section" and "KEEP=1" in lines  # comments/others kept
    assert lines[-1] == "BRAND_NEW=x"


def test_upsert_env_never_matches_substring_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SMTP_USER=me\n")
    upsert_env(env, "USER", "other")
    assert "SMTP_USER=me" in env.read_text() and "\nUSER=other" in env.read_text()


# ── probes (offline ones) ────────────────────────────────────────────

def test_probe_email_paths(settings):
    assert probe_email(settings.model_copy(update={"smtp_user": "", "smtp_password": ""}))[0] == FAIL
    assert probe_email(settings.model_copy(update={"resend_api_key": "rk"}))[0] == OK
    assert probe_email(settings.model_copy(
        update={"smtp_user": "a@b", "smtp_password": "pw"}))[0] == OK


def test_probe_marks_requires_encryption(settings):
    assert probe_marks(settings)[0] == SKIP  # unset → disabled
    naked = settings.model_copy(update={"marks_repo": "o/m", "marks_push_token": "t",
                                        "website_password": ""})
    status, detail = probe_marks(naked)
    assert status == FAIL and "WEBSITE_PASSWORD" in detail


def test_probe_search_fallback_warning(settings):
    assert probe_search(settings)[0] == WARN
    assert probe_search(settings.model_copy(update={"gemini_api_key": "g"})) \
        == (OK, "Gemini grounding configured")


# ── doctor ───────────────────────────────────────────────────────────

def test_run_check_reports_and_exit_code(settings, monkeypatch, capsys):
    monkeypatch.setattr(iw, "STEPS", [
        iw.Step("Good", "", [], lambda s: (OK, "fine")),
        iw.Step("Bad", "", [], lambda s: (FAIL, "broken thing")),
    ])
    monkeypatch.setattr(iw, "EXTRA_CHECKS", [("Extra", lambda s: (WARN, "meh"))])
    assert run_check(settings) == 1
    out = capsys.readouterr().out
    assert "Good" in out and "broken thing" in out and "meh" in out
    assert "1 blocking issue" in out
    # all-green exits 0
    monkeypatch.setattr(iw, "STEPS", [iw.Step("Good", "", [], lambda s: (OK, "fine"))])
    monkeypatch.setattr(iw, "EXTRA_CHECKS", [])
    assert run_check(settings) == 0


# ── wizard flow ──────────────────────────────────────────────────────

def test_wizard_writes_env_and_seeds_aliases(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("# ANTHROPIC_API_KEY=\n")
    data_dir = tmp_path / "data"

    probed = []
    monkeypatch.setattr(iw, "STEPS", [
        iw.Step("LLM", "intro text",
                [("ANTHROPIC_API_KEY", "API key", True),
                 ("ANTHROPIC_MODEL", "model", False)],
                lambda s: probed.append(s.anthropic_api_key) or (OK, "answers")),
    ])
    monkeypatch.setattr(iw, "EXTRA_CHECKS", [])
    answers = iter(["sk-test-123",   # api key
                    "",              # model: keep
                    "n"])            # no profile bootstrap
    monkeypatch.setattr(iw, "_ask", lambda prompt: next(answers, ""))
    # point the wizard's Settings at the temp env + data dir
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    assert run_wizard(None, env_path=env) == 0
    assert "ANTHROPIC_API_KEY=sk-test-123" in env.read_text()
    # probe saw the freshly-written value (once per step + once in final check)
    assert probed == ["sk-test-123", "sk-test-123"]
    assert (data_dir / "profile" / "aliases.yaml").exists()
    out = capsys.readouterr().out
    assert "next steps" in out and "send-test-email" in out


def test_wizard_clear_and_secret_masking(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-veryverysecretkey\n")
    monkeypatch.setattr(iw, "STEPS", [
        iw.Step("LLM", "", [("ANTHROPIC_API_KEY", "API key", True)], None)])
    monkeypatch.setattr(iw, "EXTRA_CHECKS", [])
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    prompts = []
    answers = iter(["-"])  # clear the key; every later prompt keeps defaults

    def fake_ask(prompt):
        prompts.append(prompt)
        return next(answers, "")

    monkeypatch.setattr(iw, "_ask", fake_ask)
    run_wizard(None, env_path=env)
    assert "ANTHROPIC_API_KEY=\n" in env.read_text()           # '-' cleared it
    assert "sk-veryverysecretkey" not in " ".join(prompts)     # masked in prompt
    assert "sk-v…" in prompts[0]
