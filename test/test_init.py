"""`assistant init` wizard + `--check` doctor."""

from pathlib import Path

import assistant.init_wizard as iw
from assistant.init_wizard import FAIL, OK, SKIP, WARN, probe_email, probe_llm, probe_marks, probe_model_routing, probe_search, run_check, run_wizard, upsert_env


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

def test_probe_llm_force_probes_quarantined_route(settings, monkeypatch):
    calls = []

    class FakeLLM:
        def __init__(self, configured):
            assert configured.anthropic_api_key == "key"

        def force_probe(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return "ok"

    monkeypatch.setattr("assistant.platform.llm.LLM", FakeLLM)
    status, detail = probe_llm(settings.model_copy(
        update={"anthropic_api_key": "key"}))

    assert status == OK and "answers" in detail
    assert calls == [("Reply with the single word: ok", {"max_tokens": 1500})]

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


def test_probe_model_routing_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ROLES", raising=False)
    monkeypatch.delenv("LLM_MIXTURE", raising=False)
    env = tmp_path / ".env"
    env.write_text("OTHER=1\n")
    status, detail = probe_model_routing(None, env_files=(env,))
    assert status == SKIP and "unset" in detail


def test_probe_model_routing_malformed_multiline(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ROLES", raising=False)
    monkeypatch.delenv("LLM_MIXTURE", raising=False)
    env = tmp_path / ".env"
    # the classic trap: multi-line JSON without single quotes — dotenv sees
    # only the first physical line
    env.write_text('LLM_MIXTURE={"members":[\n  {"model":"m1"},{"model":"m2"}]}\n')
    status, detail = probe_model_routing(None, env_files=(env,))
    assert status == FAIL
    assert "malformed JSON" in detail and "single quotes" in detail
    assert env.name in detail  # names the source


def test_probe_model_routing_valid_summary_no_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ROLES", raising=False)
    monkeypatch.delenv("LLM_MIXTURE", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        'LLM_ROLES={"chat": {"model": "mimo-v2.5"}, '
        '"research": {"model": "qwen3.6-plus", "api_key": "sk-SECRET-123"}}\n'
        'LLM_MIXTURE={"members": [{"model": "m1"}, {"model": "m2"}], '
        '"aggregator": {"model": "m2"}, "roles": ["pipeline"]}\n')
    status, detail = probe_model_routing(None, env_files=(env,))
    assert status == OK
    assert "chat→mimo-v2.5" in detail and "research→qwen3.6-plus" in detail
    assert "2 member(s)" in detail and "agg m2" in detail
    assert "sk-SECRET-123" not in detail  # never echo values


def test_probe_model_routing_warnings(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ROLES", raising=False)
    monkeypatch.delenv("LLM_MIXTURE", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        'LLM_ROLES={"chta": {"model": "m"}}\n'   # typo'd role name
        'LLM_MIXTURE={"members": [{"model": "m1"}, {"model": "m2"}], '
        '"roles": ["chat"]}\n')                  # MoA on interactive chat
    status, detail = probe_model_routing(None, env_files=(env,))
    assert status == WARN
    assert "unknown role 'chta'" in detail
    assert "chat role" in detail and "single-model" in detail


def test_probe_model_routing_process_env_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('LLM_ROLES={"chat": {"model": "from-file"}}\n')
    monkeypatch.setenv("LLM_ROLES", '{"chat": {"model": "from-env"}}')
    monkeypatch.delenv("LLM_MIXTURE", raising=False)
    status, detail = probe_model_routing(None, env_files=(env,))
    assert status == OK and "chat→from-env" in detail and "from-file" not in detail


def test_probe_model_routing_live_checks_unique_nondefault_routes(
        settings, tmp_path, monkeypatch):
    """Roles/MoA/review are probed once per canonical route; default is skipped."""
    calls = []

    class FakeLLM:
        def __init__(self, configured):
            assert configured.anthropic_model == "default-model"

        def force_probe_route(self, model, **kwargs):
            calls.append((model, kwargs))
            return "ok"

    monkeypatch.setattr("assistant.platform.llm.LLM", FakeLLM)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    configured = settings.model_copy(update={
        "anthropic_model": "default-model",
        "anthropic_base_url": "https://default.example/anthropic",
        "anthropic_api_key": "DEFAULT_PRIVATE_KEY",
        "llm_roles": {
            "chat": {"model": "route-model",
                     "base_url": "https://ROUTE.example/anthropic/",
                     "api_key": "ROUTE_PRIVATE_KEY"},
            # Exact default route was already exercised by probe_llm.
            "research": {"model": "default-model"}},
        "llm_mixture": {
            "members": [
                # Canonically identical to the chat route above.
                {"model": "route-model",
                 "base_url": "https://route.example:443/anthropic",
                 "api_key": "ROUTE_PRIVATE_KEY"},
                {"model": "member-two", "api_key": "MEMBER_PRIVATE_KEY"}],
            "aggregator": {"model": "agg-model", "api_key": "AGG_PRIVATE_KEY"},
            "roles": ["pipeline"]},
        # Duplicate of the aggregator route.
        "llm_review": {"model": "agg-model", "api_key": "AGG_PRIVATE_KEY"},
    })

    status, detail = probe_model_routing(configured, env_files=(empty_env,))

    assert status == OK
    assert [model for model, _kwargs in calls] == [
        "route-model", "member-two", "agg-model"]
    assert all(call[1]["max_tokens"] == 1500 for call in calls)
    assert "route checks route-model ✓, member-two ✓, agg-model ✓" in detail
    for secret in ("DEFAULT_PRIVATE_KEY", "ROUTE_PRIVATE_KEY",
                   "MEMBER_PRIVATE_KEY", "AGG_PRIVATE_KEY",
                   "https://route.example"):
        assert secret not in detail


def test_probe_model_routing_live_checks_distinct_cheap_model(
        settings, tmp_path, monkeypatch):
    """The implicit haiku/cheap tier is checked when distinct from default."""
    calls = []

    class FakeLLM:
        def __init__(self, configured):
            assert configured.cheap_model == "cheap-model"

        def force_probe_route(self, model, **kwargs):
            calls.append((model, kwargs))
            return "ok"

    monkeypatch.setattr("assistant.platform.llm.LLM", FakeLLM)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    configured = settings.model_copy(update={
        "anthropic_model": "default-model",
        "anthropic_default_haiku_model": "cheap-model",
        "anthropic_base_url": "https://default.example/anthropic",
        "anthropic_api_key": "DEFAULT_PRIVATE_KEY",
        "llm_roles": {}, "llm_mixture": {}, "llm_review": {}})

    status, detail = probe_model_routing(configured, env_files=(empty_env,))

    assert status == OK
    assert [model for model, _kwargs in calls] == ["cheap-model"]
    assert "route checks cheap-model ✓" in detail
    assert "DEFAULT_PRIVATE_KEY" not in detail


def test_probe_model_routing_uses_runtime_normalized_mixture(
        settings, tmp_path, monkeypatch):
    """Invalid members cannot produce a false two-member OK or dead probe."""
    calls = []

    class FakeLLM:
        def __init__(self, _configured):
            pass

        def force_probe_route(self, model, **kwargs):
            calls.append(model)
            return "ok"

    monkeypatch.setattr("assistant.platform.llm.LLM", FakeLLM)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    configured = settings.model_copy(update={
        "anthropic_model": "default-model",
        "anthropic_base_url": "https://default.example/anthropic",
        "anthropic_api_key": "DEFAULT_PRIVATE_KEY",
        "llm_mixture": {
            "members": [{"model": 7}, {"model": "only-valid"}],
            "aggregator": {"model": 9}, "roles": ["pipeline"]}})

    status, detail = probe_model_routing(configured, env_files=(empty_env,))

    assert status == WARN
    assert calls == ["only-valid"]
    assert "mixture 1 member(s)" in detail
    assert "2 member(s)" not in detail
    assert "dropped" in detail and "fewer than 2 members" in detail
    assert "route checks only-valid ✓" in detail


def test_probe_model_routing_empty_route_is_warning_not_success(
        settings, tmp_path, monkeypatch):
    """An empty explicit probe is surfaced and never rendered as recovered."""
    class FakeLLM:
        def __init__(self, _configured):
            pass

        def force_probe_route(self, model, **kwargs):
            return "  "

    monkeypatch.setattr("assistant.platform.llm.LLM", FakeLLM)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    configured = settings.model_copy(update={
        "anthropic_model": "default-model",
        "llm_roles": {"task": {"model": "optional-model"}}})

    status, detail = probe_model_routing(configured, env_files=(empty_env,))

    assert status == WARN
    assert "route checks optional-model ? empty" in detail
    assert "optional-model ✓" not in detail


def test_probe_model_routing_optional_failure_is_bounded_and_private(
        settings, tmp_path, monkeypatch):
    """A configured-route failure is blocking but reports no provider payload."""
    secret_key = "OPTIONAL_ROUTE_PRIVATE_KEY"
    secret_url = "https://private-route.example/anthropic"
    secret_message = "PRIVATE_PROVIDER_MESSAGE"
    long_model = "m" * 90

    class SecretPermissionError(Exception):
        status_code = 403
        body = {"error": {"message": "PRIVATE_RESPONSE_BODY"}}

    class FakeLLM:
        def __init__(self, configured):
            self.configured = configured

        def force_probe_route(self, model, **kwargs):
            assert kwargs["api_key"] == secret_key
            assert kwargs["base_url"] == secret_url
            raise SecretPermissionError(secret_message)

    monkeypatch.setattr("assistant.platform.llm.LLM", FakeLLM)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    configured = settings.model_copy(update={
        "anthropic_model": "default-model",
        "llm_roles": {"task": {"model": long_model,
                                "base_url": secret_url,
                                "api_key": secret_key}}})

    status, detail = probe_model_routing(configured, env_files=(empty_env,))

    assert status == FAIL
    assert f"{'m' * 64} ✗ SecretPermissionError HTTP 403" in detail
    assert "m" * 65 not in detail
    assert detail.count("SecretPermissionError HTTP 403") == 1
    for forbidden in (secret_key, secret_url, secret_message,
                      "PRIVATE_RESPONSE_BODY"):
        assert forbidden not in detail


def test_probe_model_routing_redacts_malicious_config_identifiers(
        tmp_path, monkeypatch):
    """No displayed role/model/mixture identifier can smuggle a URL or key."""
    monkeypatch.delenv("LLM_ROLES", raising=False)
    monkeypatch.delenv("LLM_MIXTURE", raising=False)
    monkeypatch.delenv("LLM_REVIEW", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        'LLM_ROLES={"https://PRIVATE-ROLE.example\\nline": '
        '{"model":"sk-PRIVATE-MODEL-KEY"}}\n'
        'LLM_MIXTURE={"members":[{"model":"https://PRIVATE-MODEL.example"},'
        '{"model":"safe-model"}],"roles":["token-PRIVATE-ROLE"]}\n'
        'LLM_REVIEW={"model":"credential_PRIVATE_REVIEW"}\n')

    status, detail = probe_model_routing(None, env_files=(env,))

    assert status == WARN
    assert "[redacted]" in detail
    assert "\n" not in detail and "https://" not in detail
    for forbidden in ("PRIVATE-ROLE", "PRIVATE-MODEL", "PRIVATE_REVIEW",
                      "sk-PRIVATE-MODEL-KEY", "token-PRIVATE-ROLE"):
        assert forbidden not in detail


def test_probe_model_routing_odd_mixture_shapes_warn_without_crashing(
        tmp_path, monkeypatch):
    """Valid JSON scalars/dicts cannot crash the tolerant routing doctor."""
    monkeypatch.delenv("LLM_ROLES", raising=False)
    monkeypatch.delenv("LLM_MIXTURE", raising=False)
    monkeypatch.delenv("LLM_REVIEW", raising=False)
    env = tmp_path / ".env"
    payloads = [
        '{"members":1,"roles":1,"aggregator":1}',
        ('{"members":{"model":"PRIVATE_MEMBER"},'
         '"roles":"https://PRIVATE-ROLE.example"}'),
    ]

    for payload in payloads:
        env.write_text(f"LLM_MIXTURE={payload}\n")
        status, detail = probe_model_routing(None, env_files=(env,))
        assert status == WARN
        assert '"members" must be a list' in detail
        assert '"roles" must be a list' in detail
        assert "\n" not in detail
        assert "PRIVATE_MEMBER" not in detail
        assert "PRIVATE-ROLE" not in detail and "https://" not in detail


def test_probe_model_routing_none_stays_offline(tmp_path, monkeypatch):
    """The raw-parser test seam never constructs an LLM or touches a provider."""
    class MustNotConstruct:
        def __init__(self, _settings):
            raise AssertionError("offline parser constructed an LLM")

    monkeypatch.setattr("assistant.platform.llm.LLM", MustNotConstruct)
    monkeypatch.delenv("LLM_ROLES", raising=False)
    monkeypatch.delenv("LLM_MIXTURE", raising=False)
    monkeypatch.delenv("LLM_REVIEW", raising=False)
    env = tmp_path / ".env"
    env.write_text('LLM_ROLES={"chat":{"model":"offline-model"}}\n')
    status, detail = probe_model_routing(None, env_files=(env,))
    assert status == OK and "offline-model" in detail


def test_probe_llm_failure_omits_provider_message(settings, monkeypatch):
    """The default recovery probe keeps its status but uses safe error metadata."""
    class DefaultAuthError(Exception):
        status_code = 401

    class FakeLLM:
        def __init__(self, _settings):
            pass

        def force_probe(self, prompt, **kwargs):
            raise DefaultAuthError("PRIVATE_DEFAULT_PROVIDER_MESSAGE")

    monkeypatch.setattr("assistant.platform.llm.LLM", FakeLLM)
    status, detail = probe_llm(settings.model_copy(update={
        "anthropic_api_key": "key", "anthropic_model": "default-model"}))
    assert status == FAIL
    assert detail == "model default-model failed: DefaultAuthError HTTP 401"
    assert "PRIVATE_DEFAULT_PROVIDER_MESSAGE" not in detail


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
