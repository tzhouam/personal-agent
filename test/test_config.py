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
