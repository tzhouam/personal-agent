"""Multi-user registry — the roster that authenticated identity resolves against.

Maps a **channel identity** (`weixin` `accountId`, or a `email` mailbox address)
to exactly one opaque `uid`, holds the **hash** of the single bridge↔daemon token,
and tracks per-user status. YAML-backed (`DATA_DIR/users.yaml`) with **atomic**
writes; channel bindings are **unique** (an id can't map to two users).

Auth *resolves* identity — a uid/account_id is never accepted from a caller. See
doc/DESIGN_MULTI_USER.md §4.1, Appendix A.1.
"""

import hashlib
import hmac
from pathlib import Path

import yaml

from .uidsafe import validate_uid


def hash_token(token: str) -> str:
    """SHA-256 hex of a token — what the registry stores (never the plaintext)."""
    return hashlib.sha256(str(token).encode()).hexdigest()


class UserRegistry:
    """`users.yaml`: `{bridge_token_hash, users: [{uid, display, status,
    channels: [{channel, id}]}]}`. Status is `active | deleting | disabled`."""

    def __init__(self, data_dir: Path):
        """Bind to `data_dir/users.yaml` (the roster is deployment-global)."""
        self.path = Path(data_dir) / "users.yaml"

    def _load(self) -> dict:
        """Parsed roster, or an empty scaffold when missing/unreadable."""
        if not self.path.exists():
            return {"bridge_token_hash": "", "users": []}
        data = yaml.safe_load(self.path.read_text()) or {}
        data.setdefault("bridge_token_hash", "")
        data.setdefault("users", [])
        return data

    def _save(self, data: dict) -> None:
        """Atomic write (temp + replace) so a concurrent reader never sees a
        half-written roster."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        tmp.replace(self.path)

    # ── resolution (read paths) ──────────────────────────────────────
    def by_channel(self, channel: str, external_id: str) -> str | None:
        """The uid an authenticated `(channel, external_id)` maps to, only if the
        user is `active`; else None."""
        ext = str(external_id)
        for u in self._load()["users"]:
            if u.get("status") == "active":
                for c in u.get("channels", []):
                    if c.get("channel") == channel and str(c.get("id")) == ext:
                        return u["uid"]
        return None

    def by_mailbox(self, address: str) -> str | None:
        """The uid owning an email `address` (case-insensitive)."""
        return self.by_channel("email", str(address).strip().lower())

    def verify_bridge_token(self, token: str) -> bool:
        """Constant-time check of a presented bridge token against the stored
        hash. False for an empty token or an unset hash — an empty token is
        **never** open access (§A.2)."""
        stored = self._load().get("bridge_token_hash") or ""
        return bool(token) and bool(stored) and hmac.compare_digest(hash_token(token), stored)

    def active(self) -> list[str]:
        """uids of all `active` users (what the scheduler fans out over)."""
        return [u["uid"] for u in self._load()["users"] if u.get("status") == "active"]

    def users(self) -> list[dict]:
        """All user records (uid, display, status, channels) — the read API for
        admin listings; callers never touch `_load` directly."""
        return list(self._load()["users"])

    def get(self, uid: str) -> dict | None:
        """The full record for `uid`, or None."""
        return next((u for u in self._load()["users"] if u["uid"] == uid), None)

    def status(self, uid: str) -> str | None:
        """`uid`'s status (`active`/`deleting`/`disabled`), or None if unknown."""
        u = self.get(uid)
        return u.get("status") if u else None

    # ── administration (write paths) ─────────────────────────────────
    def add_user(self, uid: str, display: str = "") -> str:
        """Register a new `active` user; raise if the uid already exists."""
        uid = validate_uid(uid)
        data = self._load()
        if any(u["uid"] == uid for u in data["users"]):
            raise ValueError(f"uid already registered: {uid!r}")
        data["users"].append({"uid": uid, "display": str(display),
                              "status": "active", "channels": []})
        self._save(data)
        return uid

    def bind_channel(self, uid: str, channel: str, external_id: str) -> None:
        """Bind `(channel, external_id)` to `uid`. Enforces global uniqueness:
        the id must not already belong to another user."""
        ext = str(external_id).strip()
        if channel == "email":
            ext = ext.lower()
        data = self._load()
        for u in data["users"]:
            for c in u.get("channels", []):
                if c["channel"] == channel and str(c["id"]) == ext and u["uid"] != uid:
                    raise ValueError(f"{channel} id {ext!r} already bound to {u['uid']!r}")
        target = next((u for u in data["users"] if u["uid"] == uid), None)
        if target is None:
            raise KeyError(uid)
        chans = target.setdefault("channels", [])
        if not any(c["channel"] == channel and str(c["id"]) == ext for c in chans):
            chans.append({"channel": channel, "id": ext})
        self._save(data)

    def clear_channels(self, uid: str) -> None:
        """Drop **all** of a user's channel bindings — the credential-revocation
        step of the deletion protocol (§14), so nothing can re-authenticate to
        this uid before the record is removed."""
        data = self._load()
        u = next((x for x in data["users"] if x["uid"] == uid), None)
        if u is None:
            raise KeyError(uid)
        u["channels"] = []
        self._save(data)

    def set_status(self, uid: str, status: str) -> None:
        """Set a user's status (e.g. `deleting` during the delete protocol)."""
        data = self._load()
        u = next((x for x in data["users"] if x["uid"] == uid), None)
        if u is None:
            raise KeyError(uid)
        u["status"] = status
        self._save(data)

    def remove_user(self, uid: str) -> None:
        """Drop the user's record + channel bindings (called **last** in the
        deletion protocol, after jobs/creds are already revoked)."""
        data = self._load()
        data["users"] = [u for u in data["users"] if u["uid"] != uid]
        self._save(data)

    def set_bridge_token(self, token: str) -> None:
        """Store only the **hash** of the bridge token (the bridge keeps the
        plaintext)."""
        data = self._load()
        data["bridge_token_hash"] = hash_token(token)
        self._save(data)
