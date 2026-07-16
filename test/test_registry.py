"""The multi-user registry — roster, channel routing, bridge-token verification
(multi-user §4.1, Appendix A.1)."""
import pytest

from assistant.registry import UserRegistry, hash_token


@pytest.fixture
def reg(tmp_path):
    return UserRegistry(tmp_path)


def test_empty_registry_resolves_nothing(reg):
    assert reg.active() == []
    assert reg.by_channel("weixin", "acctA") is None
    assert reg.get("nobody") is None
    assert reg.status("nobody") is None
    assert reg.verify_bridge_token("anything") is False  # no hash set → closed


def test_add_bind_and_resolve(reg):
    reg.add_user("alice1", display="Alice")
    reg.bind_channel("alice1", "weixin", "wx-acct-A")
    assert reg.active() == ["alice1"]
    assert reg.get("alice1")["display"] == "Alice"
    # the public read API for admin listings
    assert [u["uid"] for u in reg.users()] == ["alice1"]
    assert reg.by_channel("weixin", "wx-acct-A") == "alice1"
    # wrong channel or unknown id → no match
    assert reg.by_channel("email", "wx-acct-A") is None
    assert reg.by_channel("weixin", "wx-acct-Z") is None


def test_duplicate_uid_rejected(reg):
    reg.add_user("alice1")
    with pytest.raises(ValueError):
        reg.add_user("alice1")


def test_channel_binding_is_globally_unique(reg):
    reg.add_user("alice1")
    reg.add_user("bob123")
    reg.bind_channel("alice1", "weixin", "shared-acct")
    # the same accountId must not map to a second user (cross-tenant hijack)
    with pytest.raises(ValueError):
        reg.bind_channel("bob123", "weixin", "shared-acct")
    # re-binding the same pair to the SAME user is idempotent, not an error
    reg.bind_channel("alice1", "weixin", "shared-acct")
    assert reg.get("alice1")["channels"] == [{"channel": "weixin", "id": "shared-acct"}]


def test_bind_channel_unknown_uid_raises(reg):
    with pytest.raises(KeyError):
        reg.bind_channel("ghost1", "weixin", "x")


def test_email_binding_is_case_insensitive(reg):
    reg.add_user("alice1")
    reg.bind_channel("alice1", "email", "Alice@Example.COM")
    assert reg.by_mailbox("alice@example.com") == "alice1"
    assert reg.by_mailbox("  ALICE@EXAMPLE.COM  ") == "alice1"


def test_only_active_users_resolve(reg):
    reg.add_user("alice1")
    reg.bind_channel("alice1", "weixin", "wx-A")
    reg.set_status("alice1", "deleting")
    # a suspended/deleting user is never routed to
    assert reg.by_channel("weixin", "wx-A") is None
    assert reg.active() == []
    assert reg.status("alice1") == "deleting"
    reg.set_status("alice1", "active")
    assert reg.by_channel("weixin", "wx-A") == "alice1"


def test_set_status_unknown_uid_raises(reg):
    with pytest.raises(KeyError):
        reg.set_status("ghost1", "disabled")


def test_remove_user_drops_record_and_bindings(reg):
    reg.add_user("alice1")
    reg.bind_channel("alice1", "weixin", "wx-A")
    reg.remove_user("alice1")
    assert reg.get("alice1") is None
    assert reg.by_channel("weixin", "wx-A") is None
    # the accountId is now free to bind to someone else
    reg.add_user("bob123")
    reg.bind_channel("bob123", "weixin", "wx-A")
    assert reg.by_channel("weixin", "wx-A") == "bob123"


def test_bridge_token_stores_only_hash(reg, tmp_path):
    reg.set_bridge_token("s3cret-bridge")
    raw = (tmp_path / "users.yaml").read_text()
    assert "s3cret-bridge" not in raw          # plaintext never persisted
    assert hash_token("s3cret-bridge") in raw  # only the hash


def test_verify_bridge_token_constant_time_semantics(reg):
    reg.set_bridge_token("right-token")
    assert reg.verify_bridge_token("right-token") is True
    assert reg.verify_bridge_token("wrong-token") is False
    assert reg.verify_bridge_token("") is False        # empty is never open
    assert reg.verify_bridge_token(None) is False


def test_atomic_save_leaves_no_partial_file(reg, tmp_path):
    reg.add_user("alice1")
    reg.set_bridge_token("t")
    # a fresh registry over the same dir reads the persisted roster back
    reg2 = UserRegistry(tmp_path)
    assert reg2.active() == ["alice1"]
    assert reg2.verify_bridge_token("t") is True
    # no leftover temp file
    assert not list(tmp_path.glob("*.tmp"))
