"""UID validation + per-user path containment (multi-user isolation, §4.3)."""
import pytest

from assistant.platform.uidsafe import user_data_dir, validate_uid


def test_validate_uid_accepts_opaque_ids():
    assert validate_uid("abc123") == "abc123"
    assert validate_uid("a" * 64) == "a" * 64


@pytest.mark.parametrize("bad", [
    "", "abc", "a" * 65,            # too short / too long
    "AbC1", "a/b1", "..", "a.b1",   # case, separator, traversal, dot
    "a-b1", "a b1", "../etc", "x\0", None, 123,
])
def test_validate_uid_rejects_bad(bad):
    with pytest.raises(ValueError):
        validate_uid(bad)


def test_user_data_dir_contained(tmp_path):
    root = tmp_path / "users"
    p = user_data_dir(root, "alice1")
    assert p.name == "alice1" and p.parent.name == "users"
    assert p.resolve().is_relative_to(root.resolve())


def test_user_data_dir_rejects_traversal(tmp_path):
    with pytest.raises(ValueError):
        user_data_dir(tmp_path / "users", "../etc")


def test_user_data_dir_rejects_symlink_escape(tmp_path):
    root = tmp_path / "users"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "evil1").symlink_to(outside)          # a valid-looking uid, but a symlink out
    with pytest.raises(ValueError):
        user_data_dir(root, "evil1")
