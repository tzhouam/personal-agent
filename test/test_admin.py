"""Admin operations — roster management, the ordered deletion protocol, and
single-user migration (multi-user §10, §14)."""
import pytest

from assistant import admin
from assistant.config import Settings
from assistant.jobs import JobQueue
from assistant.registry import UserRegistry


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return Settings(_env_file=None), tmp_path


def test_add_bind_list(root):
    settings, data_dir = root
    admin.add_user(settings, "alice1", "Alice")
    admin.bind_channel(settings, "alice1", "weixin", "wx-A")
    assert (data_dir / "users" / "alice1").is_dir()          # data dir created
    listed = admin.list_users(settings)
    assert "alice1" in listed and "active" in listed and "weixin:wx-A" in listed


def test_add_user_requires_multi_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "single_user")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        admin.add_user(Settings(_env_file=None), "alice1")


def test_set_bridge_token_refuses_empty(root):
    settings, _ = root
    with pytest.raises(ValueError):
        admin.set_bridge_token(settings, "")
    admin.set_bridge_token(settings, "tok")
    assert UserRegistry(settings.data_dir).verify_bridge_token("tok")


def test_delete_user_follows_ordered_protocol(root):
    settings, data_dir = root
    admin.add_user(settings, "alice1")
    admin.bind_channel(settings, "alice1", "weixin", "wx-A")
    udir = data_dir / "users" / "alice1"
    (udir / "profile").mkdir(parents=True)
    (udir / "profile" / "me.yaml").write_text("secret")
    # a queued job exists for alice
    q = JobQueue(settings.shared_dir)
    jid = q.enqueue("alice1", "run", {})

    msg = admin.delete_user(settings, "alice1")
    assert "deleted 'alice1'" in msg
    reg = UserRegistry(settings.data_dir)
    assert reg.get("alice1") is None                 # registry entry removed last
    assert not udir.exists()                          # data dir gone
    assert q.get(jid)["state"] == "cancelled"         # queued work cancelled, not run
    # the freed accountId can now be bound to someone else
    admin.add_user(settings, "bob123")
    admin.bind_channel(settings, "bob123", "weixin", "wx-A")


def test_delete_user_can_export_first(root, tmp_path):
    settings, data_dir = root
    admin.add_user(settings, "carol1")
    (data_dir / "users" / "carol1" / "profile").mkdir(parents=True)
    (data_dir / "users" / "carol1" / "profile" / "x.yaml").write_text("keep me")
    export_prefix = tmp_path / "carol-backup"
    msg = admin.delete_user(settings, "carol1", export_to=export_prefix)
    assert "exported" in msg
    assert export_prefix.with_suffix(".tar.gz").exists()
    assert not (data_dir / "users" / "carol1").exists()


def test_delete_unknown_user_raises(root):
    settings, _ = root
    with pytest.raises(KeyError):
        admin.delete_user(settings, "ghost1")


def test_migrate_single_user_dry_run_then_move(root):
    settings, data_dir = root
    # simulate an existing single-user data dir at the root
    (data_dir / "profile").mkdir(parents=True)
    (data_dir / "profile" / "me.yaml").write_text("owner")
    (data_dir / "events.db").write_text("db")
    (data_dir / "state.json").write_text("{}")

    plan = admin.migrate_single_user(settings, "owner1", dry_run=True)
    assert plan.startswith("[dry-run]") and "profile" in plan
    assert (data_dir / "profile").exists()           # dry-run moved nothing

    admin.migrate_single_user(settings, "owner1")
    dest = data_dir / "users" / "owner1"
    assert (dest / "profile" / "me.yaml").read_text() == "owner"
    assert (dest / "events.db").exists() and (dest / "state.json").exists()
    assert not (data_dir / "profile").exists()       # moved out of the root
    assert UserRegistry(data_dir).status("owner1") == "active"


def test_migrate_skips_infra_and_refuses_existing_uid(root):
    settings, data_dir = root
    admin.add_user(settings, "taken1")
    with pytest.raises(ValueError):
        admin.migrate_single_user(settings, "taken1")


def test_shared_lessons_list_and_retire(root):
    settings, _ = root
    from assistant.lessons_store import shared_store

    assert admin.list_shared_lessons(settings) == "(no active shared lessons)"
    shared_store(settings).learn("verify dates before reminders",
                                 why="alice1+bob123 evidence", source="evolve")
    listed = admin.list_shared_lessons(settings)
    assert "[G1]" in listed and "verify dates" in listed and "why:" in listed
    assert "retired shared lesson G1" == admin.retire_shared_lesson(settings, "G1")
    assert admin.list_shared_lessons(settings) == "(no active shared lessons)"
    assert "no active shared lesson" in admin.retire_shared_lesson(settings, "G9")


def test_shared_lessons_require_multi_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "single_user")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        admin.list_shared_lessons(Settings(_env_file=None))
