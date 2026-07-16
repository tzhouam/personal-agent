"""Authenticated identity resolution — a request → exactly one uid, never a
caller-supplied one (multi-user §4.1, §6.1, Appendix A.2)."""
import pytest

from assistant.config import DEFAULT_UID, Settings
from assistant.identity import Unauthorized, context_for, resolve_uid
from assistant.registry import UserRegistry


def _single(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path / "data")


def _multi(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return Settings.for_user("alice1").model_copy(update={"data_dir": tmp_path})


# ── single_user: always DEFAULT_UID, token irrelevant ────────────────────
def test_single_user_always_default_uid(tmp_path):
    base = _single(tmp_path)
    assert resolve_uid(None, None, base) == DEFAULT_UID
    assert resolve_uid("whatever", {"account_id": "x"}, base) == DEFAULT_UID


# ── multi_tenant: bridge token mandatory, then accountId → uid ───────────
def test_multi_tenant_requires_valid_bridge_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    base = Settings.for_user("alice1").model_copy(update={"data_dir": tmp_path})
    reg = UserRegistry(tmp_path)
    reg.add_user("alice1")
    reg.bind_channel("alice1", "weixin", "wx-A")
    reg.set_bridge_token("bridge-secret")

    # no token → refused (an unset/empty token is NEVER open access)
    with pytest.raises(Unauthorized):
        resolve_uid(None, {"account_id": "wx-A"}, base, reg)
    with pytest.raises(Unauthorized):
        resolve_uid("", {"account_id": "wx-A"}, base, reg)
    # wrong token → refused
    with pytest.raises(Unauthorized):
        resolve_uid("nope", {"account_id": "wx-A"}, base, reg)
    # valid token + known accountId → the bound uid
    assert resolve_uid("bridge-secret", {"account_id": "wx-A"}, base, reg) == "alice1"


def test_multi_tenant_forged_account_id_without_token_rejected(tmp_path, monkeypatch):
    base = _multi(tmp_path, monkeypatch)
    reg = UserRegistry(tmp_path)
    reg.add_user("alice1")
    reg.bind_channel("alice1", "weixin", "wx-A")
    reg.set_bridge_token("bridge-secret")
    # a caller who guesses the accountId but lacks the bridge token gets nothing
    with pytest.raises(Unauthorized):
        resolve_uid(None, {"account_id": "wx-A", "channel": "weixin"}, base, reg)


def test_multi_tenant_unknown_account_id_rejected(tmp_path, monkeypatch):
    base = _multi(tmp_path, monkeypatch)
    reg = UserRegistry(tmp_path)
    reg.set_bridge_token("bridge-secret")
    # valid bridge token but no user for this account → no default fallback
    with pytest.raises(Unauthorized):
        resolve_uid("bridge-secret", {"account_id": "ghost"}, base, reg)
    # missing account_id entirely → also refused
    with pytest.raises(Unauthorized):
        resolve_uid("bridge-secret", {}, base, reg)


def test_multi_tenant_channel_defaults_to_weixin(tmp_path, monkeypatch):
    base = _multi(tmp_path, monkeypatch)
    reg = UserRegistry(tmp_path)
    reg.add_user("bob123")
    reg.bind_channel("bob123", "weixin", "wx-B")
    reg.set_bridge_token("t")
    # channel omitted → weixin
    assert resolve_uid("t", {"account_id": "wx-B"}, base, reg) == "bob123"
    # explicit non-weixin channel with no such binding → refused
    with pytest.raises(Unauthorized):
        resolve_uid("t", {"account_id": "wx-B", "channel": "sms"}, base, reg)


def test_context_for_returns_scoped_settings(tmp_path, monkeypatch):
    base = _multi(tmp_path, monkeypatch)
    reg = UserRegistry(tmp_path)
    reg.add_user("alice1")
    reg.bind_channel("alice1", "weixin", "wx-A")
    reg.set_bridge_token("t")
    ctx = context_for("t", {"account_id": "wx-A"}, base, reg)
    assert ctx.uid == "alice1"
    assert ctx.settings.uid == "alice1"
    assert ctx.settings.deployment_mode == "multi_tenant"
    assert ctx.settings.data_dir.name == "alice1"
