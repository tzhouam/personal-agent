"""UID validation + path containment for multi-user data isolation.

UIDs are **opaque, immutable, and path-safe**: lowercase alphanumerics only, so a
malformed or hostile id can never carry a path separator, `..`, or an absolute
path. Every per-user path derivation is additionally **containment-checked** (the
resolved path must stay inside the users root, and the leaf must not be a
symlink) so nothing can escape via a pre-existing symlink either.

See doc/DESIGN_MULTI_USER.md §4.3. Human display names live in the registry as
metadata — never in a path.
"""

import re
from pathlib import Path

# opaque id: lowercase alnum, 4..64 chars. No separators, dots, or case, so
# traversal ("../"), absolute paths, and "." / ".." are rejected by construction.
_UID_RE = re.compile(r"^[0-9a-z]{4,64}$")


def validate_uid(uid: str) -> str:
    """Return `uid` unchanged if it is a well-formed opaque id, else raise
    `ValueError`. Runs *before* any filesystem use so a bad id never reaches the
    disk layer."""
    if not isinstance(uid, str) or not _UID_RE.match(uid):
        raise ValueError(f"invalid uid: {uid!r}")
    return uid


def user_data_dir(users_root: Path, uid: str) -> Path:
    """`users_root/uid`, guaranteed to stay inside `users_root`.

    Validates the uid, then checks the *resolved* path is contained in the
    resolved root (blocks symlink escapes) and that the leaf itself is not a
    symlink. Returns the (unresolved) path to use so the caller creates it under
    the root. Raises `ValueError` on any containment violation."""
    uid = validate_uid(uid)
    root = Path(users_root).resolve()
    path = root / uid
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"uid path escapes users root: {uid!r}")
    if path.is_symlink():
        raise ValueError(f"uid path is a symlink: {uid!r}")
    return path
