"""The `Settings.for_user` isolation seam + deployment mode (multi-user §4, §6.1)."""
import pytest

from assistant.config import DEFAULT_UID, Settings


def test_single_user_is_the_default_and_backward_compatible():
    s = Settings(_env_file=None, anthropic_api_key="x")
    assert s.deployment_mode == "single_user"
    assert s.uid == DEFAULT_UID


def test_single_user_for_user_is_a_noop(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "single_user")
    s = Settings.for_user()                       # ≡ Settings()
    assert s.uid == DEFAULT_UID and s.deployment_mode == "single_user"
    # a non-default uid is meaningless in single-user mode
    with pytest.raises(ValueError):
        Settings.for_user("someuser1")


def test_multi_tenant_scopes_data_dir_and_derived_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = Settings.for_user("alice1")
    assert s.uid == "alice1" and s.deployment_mode == "multi_tenant"
    assert s.data_dir.name == "alice1" and s.data_dir.parent.name == "users"
    # every derived path follows the per-user data_dir for free
    assert s.profile_dir == s.data_dir / "profile"
    assert s.events_db == s.data_dir / "events.db"
    assert s.state_file == s.data_dir / "state.json"


def test_multi_tenant_rejects_bad_or_missing_uid(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        Settings.for_user("../etc")               # path traversal
    with pytest.raises(ValueError):
        Settings.for_user(None)                    # NO default fallback in multi_tenant


def test_two_users_get_distinct_data_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    a = Settings.for_user("alice1")
    b = Settings.for_user("bob123")
    assert a.data_dir != b.data_dir
    assert a.profile_dir != b.profile_dir


# ── personal fields never inherit (§4.1; live incident 2026-07-16: a new
# user's first run collected the owner's GitHub+Gmail via the shared .env) ──
def test_multi_tenant_personal_fields_never_inherit(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # owner identity in the shared config — set as env vars, the STRONGEST
    # inheritance channel (env vars outrank every env file)
    monkeypatch.setenv("GITHUB_TOKEN", "owner-secret")
    monkeypatch.setenv("SMTP_USER", "owner@example.com")
    monkeypatch.setenv("WECHAT_ANNOUNCE", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "shared-infra-key")
    s = Settings.for_user("alice1")
    assert s.github_token == ""                     # never the owner's
    assert s.smtp_user == "" and s.recipient == ""  # no email identity/recipient
    assert s.wechat_announce is False               # no owner-channel pings
    # browser history points into HER dir, never the host owner's profile
    assert s.chrome_history_path == s.data_dir / "chrome" / "History"
    # non-personal infra still inherits — one shared LLM/search config
    assert s.tavily_api_key == "shared-infra-key"


def test_multi_tenant_own_config_env_beats_shared_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "owner-secret")
    cfg = tmp_path / "users" / "bob123" / "config.env"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("GITHUB_TOKEN='bobs-token'\nGITHUB_USER=bob\n"
                   "WECHAT_ANNOUNCE=true\nSMTP_PASSWORD=\n")
    s = Settings.for_user("bob123")
    assert s.github_token == "bobs-token"     # his own, not the owner's env var
    assert s.github_user == "bob"
    assert s.wechat_announce is True          # coerced bool from config.env
    assert s.smtp_password == ""              # KEY= (empty) counts as unset
    assert s.smtp_user == ""                  # fields he didn't set stay reset


def test_single_user_inheritance_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "single_user")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "owner-secret")
    s = Settings.for_user()
    assert s.github_token == "owner-secret"   # today's behavior, byte-identical
