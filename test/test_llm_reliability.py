"""LLM retry, structured-output recovery, and durable route-health policy."""

import sqlite3

import pytest

import assistant.platform.llm as llm_mod
from assistant.platform.config import Settings
from assistant.platform.llm import CompletionText, LLM
from assistant.platform.llm import RouteHealthPersistenceError
from assistant.platform.llm import RouteQuarantinedError
from assistant.platform.llm import StructuredOutputTruncatedError
from assistant.platform.llm_health import credential_fingerprint
from assistant.platform.llm_health import route_scopes


class _Resp:
    """Minimal Anthropic response carrying text and a stop reason."""

    def __init__(self, text="ok", stop_reason="end_turn"):
        """Build one text block for the wrapper's normal extraction path."""
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = stop_reason
        self.usage = None


class _Err(Exception):
    """Provider-like failure with structured status/body metadata."""

    def __init__(self, status, body=None, message="PRIVATE_EXCEPTION_MESSAGE"):
        """Keep private text available to catch accidental persistence."""
        super().__init__(message)
        self.status_code = status
        self.body = body


@pytest.fixture(autouse=True)
def _isolate_policy(monkeypatch):
    """Keep sleeps instant and process-local breaker state test-independent."""
    monkeypatch.setattr(llm_mod, "_sleep", lambda _seconds: None)
    llm_mod._reset_breaker()
    yield
    llm_mod._reset_breaker()


def _settings(tmp_path, **kwargs):
    """Build one route configuration rooted entirely in the test directory."""
    values = {
        "data_dir": tmp_path / "data",
        "anthropic_api_key": "route-key-a",
        "anthropic_base_url": "https://provider.example/anthropic",
        "anthropic_model": "model-a",
    }
    values.update(kwargs)
    return Settings(_env_file=None, **values)


def _install_factory(monkeypatch, handler, made=None):
    """Install an Anthropic factory dispatching every create to ``handler``."""
    made = [] if made is None else made

    class Client:
        def __init__(self, client_kwargs):
            self.client_kwargs = client_kwargs
            self.messages = type("Messages", (), {})()
            self.messages.create = self.create

        def create(self, **request):
            return handler(self.client_kwargs, request)

    def factory(**client_kwargs):
        made.append(client_kwargs)
        return Client(client_kwargs)

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", factory)
    return made


def test_sdk_retries_are_disabled_on_every_client(monkeypatch, tmp_path):
    """SDK defaults cannot multiply the platform's three-attempt ceiling."""
    made = _install_factory(monkeypatch, lambda _client, _request: _Resp())
    llm = LLM(_settings(tmp_path, llm_roles={
        "research": {"model": "other", "base_url": "https://other.example",
                     "api_key": "other-key"}}))
    llm._resolve("research", None)
    assert len(made) == 2
    assert all(client["max_retries"] == 0 for client in made)


@pytest.mark.parametrize("failure", [_Err(429), _Err(503)])
def test_transient_status_retries_three_total_with_one_four_waits(
        monkeypatch, tmp_path, failure):
    """429/5xx get exactly two platform waits and three total attempts."""
    calls = []
    waits = []

    def handler(_client, _request):
        calls.append(1)
        if len(calls) < 3:
            raise failure
        return _Resp("recovered")

    _install_factory(monkeypatch, handler)
    monkeypatch.setattr(llm_mod, "_sleep", waits.append)
    assert LLM(_settings(tmp_path)).complete("PRIVATE_PROMPT") == "recovered"
    assert len(calls) == 3
    assert waits == [1, 4]


def test_transport_retries_at_most_three_total(monkeypatch, tmp_path):
    """Transport exhaustion is bounded even when every attempt fails."""
    import httpx

    calls = []
    waits = []
    error = httpx.ConnectError(
        "PRIVATE_EXCEPTION_MESSAGE",
        request=httpx.Request("POST", "https://provider.example/messages"))

    def handler(_client, _request):
        calls.append(1)
        raise error

    _install_factory(monkeypatch, handler)
    monkeypatch.setattr(llm_mod, "_sleep", waits.append)
    with pytest.raises(httpx.ConnectError):
        LLM(_settings(tmp_path)).complete("PRIVATE_PROMPT")
    assert len(calls) == 3
    assert waits == [1, 4]


@pytest.mark.parametrize("status", [401, 403])
def test_auth_and_permission_are_never_retried(monkeypatch, tmp_path, status):
    """Terminal 401/403 failures make one provider attempt and wait zero times."""
    calls = []
    waits = []

    def handler(_client, _request):
        calls.append(1)
        raise _Err(status)

    _install_factory(monkeypatch, handler)
    monkeypatch.setattr(llm_mod, "_sleep", waits.append)
    with pytest.raises(_Err):
        LLM(_settings(tmp_path)).complete("PRIVATE_PROMPT")
    assert len(calls) == 1
    assert waits == []


def test_call_retains_stop_reason_on_string_result(monkeypatch, tmp_path):
    """Structured recovery sees stop metadata without breaking string callers."""
    _install_factory(monkeypatch, lambda _client, _request: _Resp(
        '{"partial":', "max_tokens"))
    result = LLM(_settings(tmp_path)).complete("json")
    assert result == '{"partial":'
    assert isinstance(result, str)
    assert result.stop_reason == "max_tokens"


def test_end_turn_json_error_repairs_once_at_same_budget(monkeypatch, tmp_path):
    """Syntax failure gets feedback, prior output, and no budget escalation."""
    llm = LLM(_settings(tmp_path))
    scripted = [CompletionText("NOT_PRIVATE_JSON", "end_turn"),
                CompletionText('{"ok": true}', "end_turn")]
    calls = []

    def complete(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return scripted.pop(0)

    monkeypatch.setattr(llm, "complete", complete)
    assert llm.complete_json("make json", max_tokens=700) == {"ok": True}
    assert len(calls) == 2
    assert calls[0][1]["max_tokens"] == calls[1][1]["max_tokens"] == 700
    assert "JSON repair feedback" in calls[1][0]
    assert "NOT_PRIVATE_JSON" in calls[1][0]


def test_max_tokens_json_error_retries_once_at_double_capped_budget(
        monkeypatch, tmp_path):
    """Truncation reruns the original request at a larger, capped budget."""
    llm = LLM(_settings(tmp_path))
    scripted = [CompletionText('{"partial":', "max_tokens"),
                CompletionText('{"ok": 1}', "end_turn")]
    calls = []

    def complete(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return scripted.pop(0)

    monkeypatch.setattr(llm, "complete", complete)
    assert llm.complete_json("make json", max_tokens=9000) == {"ok": 1}
    assert [call[1]["max_tokens"] for call in calls] == [9000, 16000]
    assert calls[0][0] == calls[1][0] == "make json"


def test_16k_json_truncation_fails_without_identical_retry(monkeypatch, tmp_path):
    """A capped truncation is explicit and never repeats the same budget."""
    llm = LLM(_settings(tmp_path))
    calls = []

    def complete(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return CompletionText('{"partial":', "max_tokens")

    monkeypatch.setattr(llm, "complete", complete)
    with pytest.raises(StructuredOutputTruncatedError, match="16000-token cap"):
        llm.complete_json("make json", max_tokens=16000)
    assert len(calls) == 1


def test_second_truncation_fails_after_exactly_one_retry(monkeypatch, tmp_path):
    """The enlarged structured call cannot start a second repair ladder."""
    llm = LLM(_settings(tmp_path))
    calls = []

    def complete(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return CompletionText('{"partial":', "max_tokens")

    monkeypatch.setattr(llm, "complete", complete)
    with pytest.raises(StructuredOutputTruncatedError, match="one retry"):
        llm.complete_json("make json", max_tokens=8000)
    assert [call[1]["max_tokens"] for call in calls] == [8000, 16000]


def test_401_quarantine_is_provider_scoped_and_survives_restart(
        monkeypatch, tmp_path):
    """A broken credential suppresses every model after rebuilding ``LLM``."""
    calls = []

    def handler(_client, request):
        calls.append(request["model"])
        raise _Err(401)

    _install_factory(monkeypatch, handler)
    settings = _settings(tmp_path)
    with pytest.raises(_Err):
        LLM(settings).complete("first")
    rebuilt = LLM(settings.model_copy(update={"anthropic_model": "model-b"}))
    with pytest.raises(RouteQuarantinedError) as caught:
        rebuilt.complete("second")
    assert caught.value.scope == "prov"
    assert calls == ["model-a"]


def test_403_is_model_scoped_unless_provider_code_is_auth(
        monkeypatch, tmp_path):
    """Ordinary entitlement and explicit credential failures have distinct scope."""
    calls = []
    outcome = {"failure": _Err(403)}

    def handler(_client, request):
        calls.append(request["model"])
        failure = outcome.pop("failure", None)
        if failure:
            raise failure
        return _Resp("allowed")

    _install_factory(monkeypatch, handler)
    settings = _settings(tmp_path)
    with pytest.raises(_Err):
        LLM(settings).complete("first")
    assert LLM(settings.model_copy(update={
        "anthropic_model": "model-b"})).complete("second") == "allowed"
    with pytest.raises(RouteQuarantinedError) as caught:
        LLM(settings).complete("third")
    assert caught.value.scope == "model"

    # A separately configured route whose 403 explicitly names auth failure
    # poisons the provider/credential, not only the requested model.
    auth_settings = _settings(
        tmp_path, data_dir=tmp_path / "auth-data",
        anthropic_base_url="https://auth.example/anthropic")
    outcome["failure"] = _Err(403, {"error": {
        "type": "Authentication-Error", "message": "PRIVATE_BODY"}})
    with pytest.raises(_Err):
        LLM(auth_settings).complete("auth")
    with pytest.raises(RouteQuarantinedError) as auth_caught:
        LLM(auth_settings.model_copy(update={
            "anthropic_model": "model-b"})).complete("blocked")
    assert auth_caught.value.scope == "prov"


def test_key_and_url_changes_bypass_old_quarantine(monkeypatch, tmp_path):
    """Health identity follows effective configuration, not a global provider name."""
    calls = []
    fail_first = {"value": True}

    def handler(_client, request):
        calls.append(request["model"])
        if fail_first["value"]:
            fail_first["value"] = False
            raise _Err(401)
        return _Resp("fresh-route")

    _install_factory(monkeypatch, handler)
    settings = _settings(tmp_path)
    with pytest.raises(_Err):
        LLM(settings).complete("first")
    changed_key = settings.model_copy(update={"anthropic_api_key": "route-key-b"})
    assert LLM(changed_key).complete("new key") == "fresh-route"
    changed_url = settings.model_copy(update={
        "anthropic_base_url": "https://other-provider.example/anthropic"})
    assert LLM(changed_url).complete("new url") == "fresh-route"
    assert len(calls) == 3


def test_quarantine_is_deployment_shared_across_user_settings(
        monkeypatch, tmp_path):
    """Per-user LLM objects converge on one deployment ``shared`` database."""
    calls = []

    def handler(_client, request):
        calls.append(request["model"])
        raise _Err(401)

    _install_factory(monkeypatch, handler)
    root = tmp_path / "deployment"
    alice = _settings(tmp_path, data_dir=root / "users" / "alice",
                      deployment_mode="multi_tenant")
    bob = _settings(tmp_path, data_dir=root / "users" / "bob",
                    deployment_mode="multi_tenant")
    assert alice.shared_dir == bob.shared_dir == root / "shared"
    with pytest.raises(_Err):
        LLM(alice).complete("alice")
    with pytest.raises(RouteQuarantinedError):
        LLM(bob).complete("bob")
    assert calls == ["model-a"]


def test_health_database_persists_no_secrets_messages_bodies_or_prompts(
        monkeypatch, tmp_path):
    """Only canonical route identifiers cross the SQLite boundary."""
    secret = "SUPER_SECRET_API_KEY"
    prompt = "PRIVATE_OWNER_PROMPT"
    body_secret = "PRIVATE_PROVIDER_BODY"
    userinfo_secret = "PRIVATE_URL_CREDENTIAL"
    path_secret = "PRIVATE_PATH_CREDENTIAL"
    query_secret = "PRIVATE_QUERY_CREDENTIAL"
    fragment_secret = "PRIVATE_FRAGMENT_CREDENTIAL"

    def handler(_client, _request):
        raise _Err(401, {"error": {"type": "authentication_error",
                                    "message": body_secret}})

    _install_factory(monkeypatch, handler)
    settings = _settings(
        tmp_path, anthropic_api_key=secret,
        anthropic_base_url=(
            f"https://user:{userinfo_secret}@PROVIDER.EXAMPLE:443/"
            f"{path_secret}/?token={query_secret}#{fragment_secret}"))
    with pytest.raises(_Err):
        LLM(settings).complete(prompt)

    db = settings.shared_dir / "llm_health.db"
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT base_url, credential_fp, scope, model "
            "FROM route_quarantine").fetchone()
    assert row[0].startswith("https://provider.example|route_sha256=")
    assert row[1] == credential_fingerprint(secret)
    assert row[2:] == ("prov", "")
    persisted = b"".join(path.read_bytes() for path in db.parent.glob("llm_health.db*"))
    for forbidden in (secret, prompt, body_secret, userinfo_secret,
                      path_secret, query_secret, fragment_secret,
                      "PRIVATE_EXCEPTION_MESSAGE"):
        assert forbidden.encode() not in persisted


def test_url_userinfo_path_and_query_changes_bypass_route_health_identity():
    """Hidden URL configuration participates in identity without being stored."""
    original = route_scopes(
        "https://user:credential-a@PROVIDER.example:443/tenant-a/?token=a",
        "api-key", "model-a")
    same = route_scopes(
        "https://user:credential-a@provider.EXAMPLE/tenant-a?token=a",
        "api-key", "model-a")
    changed_userinfo = route_scopes(
        "https://user:credential-b@provider.example/tenant-a?token=a",
        "api-key", "model-a")
    changed_path = route_scopes(
        "https://user:credential-a@provider.example/tenant-b?token=a",
        "api-key", "model-a")
    changed_query = route_scopes(
        "https://user:credential-a@provider.example/tenant-a?token=b",
        "api-key", "model-a")

    assert original == same
    assert original != changed_userinfo
    assert original != changed_path
    assert original != changed_query


def test_force_probe_clears_durable_and_transient_route_health(
        monkeypatch, tmp_path):
    """A successful explicit probe makes the route immediately MoA-eligible."""
    calls = []

    def handler(_client, request):
        calls.append(request["model"])
        if request["model"] == "agg":
            return _Resp("SYNTH")
        return _Resp(f"proposal-{request['model']}")

    _install_factory(monkeypatch, handler)
    settings = _settings(tmp_path, llm_mixture={
        "members": [{"model": "model-a"}, {"model": "model-b"}],
        "aggregator": {"model": "agg"}, "roles": ["pipeline"]})
    llm = LLM(settings)
    scopes = llm_mod._route_scopes(settings, None, None, "model-a")
    llm._health.quarantine(scopes, "prov")
    for _ in range(settings.moa_member_fail_threshold):
        mode, generations, claimed = llm_mod._breaker_check(
            scopes, settings.moa_member_cooldown_s)
        llm_mod._breaker_record(
            scopes, generations, claimed, "prov",
            settings.moa_member_fail_threshold, settings.moa_member_cooldown_s)
    assert llm._quarantine_scope(scopes) == "prov"
    assert llm_mod._breaker_check(scopes, settings.moa_member_cooldown_s)[0] == "open"

    assert llm.force_probe("check", model="model-a") == "proposal-model-a"
    assert llm._quarantine_scope(scopes) is None
    assert llm_mod._breaker_check(scopes, settings.moa_member_cooldown_s)[0] == "closed"
    calls.clear()
    assert LLM(settings).complete("go", role="pipeline") == "SYNTH"
    assert {"model-a", "model-b", "agg"}.issubset(calls)


def test_failed_force_probe_does_not_clear_quarantine(monkeypatch, tmp_path):
    """Recovery remains fail-closed until the provider actually succeeds."""
    calls = []

    def handler(_client, _request):
        calls.append(1)
        raise _Err(401)

    _install_factory(monkeypatch, handler)
    settings = _settings(tmp_path)
    llm = LLM(settings)
    scopes = llm_mod._route_scopes(settings, None, None, "model-a")
    llm._health.quarantine(scopes, "prov")
    with pytest.raises(_Err):
        llm.force_probe("check")
    with pytest.raises(RouteQuarantinedError):
        LLM(settings).complete("normal")
    assert calls == [1]


def test_empty_force_probe_keeps_durable_and_transient_quarantine(
        monkeypatch, tmp_path):
    """An empty provider response is not proof of recovery at either layer."""
    _install_factory(monkeypatch, lambda _client, _request: _Resp("  "))
    settings = _settings(tmp_path)
    llm = LLM(settings)
    scopes = llm_mod._route_scopes(settings, None, None, "model-a")
    llm._health.quarantine(scopes, "prov")
    for _ in range(settings.moa_member_fail_threshold):
        mode, generations, claimed = llm_mod._breaker_check(
            scopes, settings.moa_member_cooldown_s)
        llm_mod._breaker_record(
            scopes, generations, claimed, "prov",
            settings.moa_member_fail_threshold, settings.moa_member_cooldown_s)

    assert llm.force_probe("check") == "  "
    assert llm._quarantine_scope(scopes) == "prov"
    assert llm_mod._breaker_check(
        scopes, settings.moa_member_cooldown_s)[0] == "open"


def test_force_probe_clear_failure_is_safe_and_keeps_transient_open(
        monkeypatch, tmp_path):
    """Durable clear failure is bounded and cannot falsely reset the breaker."""
    _install_factory(monkeypatch, lambda _client, _request: _Resp("ok"))
    settings = _settings(tmp_path)
    llm = LLM(settings)
    scopes = llm_mod._route_scopes(settings, None, None, "model-a")
    llm._health.quarantine(scopes, "prov")
    for _ in range(settings.moa_member_fail_threshold):
        mode, generations, claimed = llm_mod._breaker_check(
            scopes, settings.moa_member_cooldown_s)
        llm_mod._breaker_record(
            scopes, generations, claimed, "prov",
            settings.moa_member_fail_threshold, settings.moa_member_cooldown_s)

    def fail_clear(_scopes):
        raise RuntimeError("PRIVATE_DATABASE_FAILURE")

    monkeypatch.setattr(llm._health, "clear_route", fail_clear)
    with pytest.raises(RouteHealthPersistenceError) as caught:
        llm.force_probe("check")

    assert str(caught.value) == "LLM route quarantine clear failed"
    assert "PRIVATE_DATABASE_FAILURE" not in str(caught.value)
    assert llm._quarantine_scope(scopes) == "prov"
    assert llm_mod._breaker_check(
        scopes, settings.moa_member_cooldown_s)[0] == "open"


def test_explicit_route_probe_clears_only_that_model_quarantine(
        monkeypatch, tmp_path):
    """The doctor can recover a non-default 403 without clearing sibling models."""
    calls = []

    def handler(client, request):
        calls.append((client, request["model"]))
        return _Resp("ok")

    _install_factory(monkeypatch, handler)
    settings = _settings(tmp_path)
    llm = LLM(settings)
    base_url = "https://optional.example/anthropic"
    api_key = "optional-route-key"
    target = llm_mod._route_scopes(settings, base_url, api_key, "target-model")
    sibling = llm_mod._route_scopes(settings, base_url, api_key, "sibling-model")
    llm._health.quarantine(target, "model")
    llm._health.quarantine(sibling, "model")

    assert llm.force_probe_route(
        "target-model", base_url=base_url, api_key=api_key) == "ok"

    assert llm._quarantine_scope(target) is None
    assert llm._quarantine_scope(sibling) == "model"
    assert calls[-1][0]["base_url"] == base_url
    assert calls[-1][0]["api_key"] == api_key
    assert calls[-1][0]["max_retries"] == 0
    assert calls[-1][1] == "target-model"


def test_durable_health_gate_skips_single_answer_synthesis_and_degrades(
        monkeypatch, tmp_path):
    """A quarantined proposer leaves no false one-answer MoA synthesis."""
    calls = []
    metrics = []

    def handler(_client, request):
        calls.append(request["model"])
        if request["model"] == "agg":
            return _Resp("MUST_NOT_SYNTHESIZE")
        return _Resp(f"proposal-{request['model']}")

    _install_factory(monkeypatch, handler)
    settings = _settings(tmp_path, llm_mixture={
        "members": [
            {"model": "m1", "base_url": "https://one.example", "api_key": "k1"},
            {"model": "m2", "base_url": "https://two.example", "api_key": "k2"}],
        "aggregator": {"model": "agg"}, "roles": ["pipeline"]})
    llm = LLM(settings, metrics_sink=lambda *args: metrics.append(args[3]))
    dead = llm_mod._route_scopes(
        settings, "https://two.example", "k2", "m2")
    llm._health.quarantine(dead, "model")

    assert llm.complete("go", role="pipeline") == "proposal-m1"
    assert calls == ["m1"]
    assert metrics[-1]["members_skipped"] == 1
    assert metrics[-1]["proposals_final"] == 1
    assert metrics[-1]["aggregator_attempted"] == 0
    assert metrics[-1]["aggregator_skipped"] == 1
    assert metrics[-1]["degraded"] == 1
