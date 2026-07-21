"""First-contact self-onboarding for multi_tenant deployments.

A new person is admitted in two operator-gated steps: they scan the operator's
OpenClaw login QR (so their WeChat becomes an account on the gateway), then their
first message to the bot carries a **one-time invite code** the operator issued
(`assistant admin invite`). The onboarding state machine (`handle`) validates the
code, asks for a display name, and provisions the tenant — generating an opaque
uid, binding the accountId, creating the data dir, seeding a minimal profile, and
writing an empty `config.env` skeleton (never inheriting anyone's credentials).

Everything here is deployment-global (the user does not exist yet), so the stores
live under `shared/` (un-versioned, like the job queue and the registry) and every
mutating transaction is serialized by the codebase's own per-path flock
(`locks._path_lock`) around a millisecond-scale commit — never across the LLM or
the conversation. single_user is unaffected; the daemon only reaches this path in
multi_tenant, behind a valid bridge token, for an unknown accountId, on `/chat`.
"""

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from assistant.platform.config import Settings
from assistant.platform.locks import _path_lock
from assistant.platform.registry import UserRegistry, hash_token
from assistant.platform.uidsafe import user_data_dir, validate_uid

log = logging.getLogger("assistant")

# Typeable code alphabet — no 0/O/1/I/L ambiguity; 3×4 chars ≈ 62 bits.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_MAX_CODE_ATTEMPTS = 5          # bad codes before an unknown account goes silent
_MAX_NAME = 40


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lock_path(shared_dir: Path) -> Path:
    """The single onboarding transaction lock (shared, deployment-global)."""
    return Path(shared_dir) / "onboarding.lock"


def generate_code() -> str:
    """A fresh human-typeable invite code, e.g. `K7PQ-M9RT-3XVW`."""
    groups = ["".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
              for _ in range(3)]
    return "-".join(groups)


def _normalize_code(text: str) -> str:
    """Canonicalize a user-typed code: uppercase, keep only alphabet chars,
    regroup into 4-4-4 (so spacing/casing/dashes don't matter)."""
    kept = [c for c in str(text).upper() if c in _CODE_ALPHABET]
    return "-".join("".join(kept[i:i + 4]) for i in range(0, len(kept), 4)) if kept else ""


class InviteStore:
    """`shared/invites.yaml`: one-time invite codes stored **hashed** (never the
    plaintext), single-use with an expiry. States: `open → reserved → used`
    (reserved binds the code to the accountId that presented it, so a
    provisioning retry by that same account continues without re-issuing)."""

    def __init__(self, shared_dir: Path):
        self.shared_dir = Path(shared_dir)
        self.path = self.shared_dir / "invites.yaml"

    def _load(self) -> dict:
        if not self.path.exists():
            return {"invites": []}
        return yaml.safe_load(self.path.read_text()) or {"invites": []}

    def _save(self, data: dict) -> None:
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        tmp.replace(self.path)

    def create(self, ttl_days: int = 7) -> str:
        """Mint and store a new open invite; returns the plaintext code (shown
        once — only the hash is persisted)."""
        code = generate_code()
        with _path_lock(_lock_path(self.shared_dir)):
            data = self._load()
            data["invites"].append({
                "code_hash": hash_token(code), "status": "open",
                "created": _now().isoformat(),
                "expires": (_now() + timedelta(days=max(1, int(ttl_days)))).isoformat()})
            self._save(data)
        return code

    def _match(self, data: dict, code: str) -> dict | None:
        h = hash_token(_normalize_code(code))
        return next((i for i in data["invites"] if i["code_hash"] == h), None)

    def reserve(self, code: str, account_id: str) -> str:
        """Reserve a code to `account_id`. Returns `reserved` (freshly or
        already by this same account), `used`, `expired`, or `bad` (unknown /
        reserved by someone else). Atomic under the shared lock."""
        if not _normalize_code(code):
            return "bad"
        with _path_lock(_lock_path(self.shared_dir)):
            data = self._load()
            inv = self._match(data, code)
            if inv is None:
                return "bad"
            if inv["status"] == "used":
                return "used"
            if inv.get("expires", "") < _now().isoformat():
                return "expired"
            if inv["status"] == "reserved":
                return "reserved" if inv.get("reserved_by") == account_id else "bad"
            inv.update(status="reserved", reserved_by=account_id,
                       reserved_at=_now().isoformat())
            self._save(data)
            return "reserved"

    def mark_used(self, account_id: str) -> None:
        """Consume the invite reserved by `account_id` (called only after
        provisioning commits). Idempotent."""
        with _path_lock(_lock_path(self.shared_dir)):
            data = self._load()
            for inv in data["invites"]:
                if inv.get("reserved_by") == account_id and inv["status"] == "reserved":
                    inv.update(status="used", used_at=_now().isoformat())
            self._save(data)

    def active(self) -> list[dict]:
        """Open, unexpired invites (for `admin invites` listing) — no hashes
        or plaintext, just status + timestamps."""
        now = _now().isoformat()
        return [{"status": i["status"], "created": i.get("created"),
                 "expires": i.get("expires")}
                for i in self._load()["invites"]
                if i["status"] == "open" and i.get("expires", "") >= now]


class OnboardingStore:
    """`shared/onboarding.yaml`: the transient per-accountId onboarding session
    (`{state, attempts, started}`; states `awaiting_code → awaiting_name`).
    Persisted (not in-memory) so a mid-onboarding daemon restart — frequent in
    this deployment — doesn't drop the conversation. Un-versioned like the rest
    of `shared/`."""

    def __init__(self, shared_dir: Path):
        self.shared_dir = Path(shared_dir)
        self.path = self.shared_dir / "onboarding.yaml"

    def _load(self) -> dict:
        if not self.path.exists():
            return {"sessions": {}}
        return yaml.safe_load(self.path.read_text()) or {"sessions": {}}

    def _save(self, data: dict) -> None:
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        tmp.replace(self.path)

    def get(self, account_id: str) -> dict:
        return self._load()["sessions"].get(str(account_id),
                                            {"state": "awaiting_code", "attempts": 0})

    def set(self, account_id: str, **fields) -> None:
        with _path_lock(_lock_path(self.shared_dir)):
            data = self._load()
            data["sessions"][str(account_id)] = fields
            self._save(data)

    def clear(self, account_id: str) -> None:
        with _path_lock(_lock_path(self.shared_dir)):
            data = self._load()
            data["sessions"].pop(str(account_id), None)
            self._save(data)


def _new_uid(reg: UserRegistry) -> str:
    """A fresh opaque uid (16 hex, `validate_uid`-safe), collision-checked."""
    for _ in range(5):
        uid = uuid.uuid4().hex[:16]
        if reg.get(uid) is None:
            return validate_uid(uid)
    raise RuntimeError("could not generate a unique uid")


_CONFIG_ENV_SKELETON = """\
# {uid} ({display}) — personal identity/credentials.
# NEVER inherited from the shared .env (PERSONAL_ENV_FIELDS). Fill in what this
# user wants enabled; empty = that feature stays off (safe defaults).
GITHUB_TOKEN=
GITHUB_USER=
SMTP_USER=
SMTP_PASSWORD=
DIGEST_TO=
WEBSITE_REPO=
WEBSITE_PASSWORD=
MARKS_REPO=
MARKS_PUSH_TOKEN=
RESUME_REMOTE_URL=
ANNOUNCE_ACCOUNT=
ANNOUNCE_TO=
"""


# Seeding a new tenant's profile.yaml is agent-owned (the ProfileStore lives in
# the agent layer). This platform module provisions the tenant but delegates the
# profile seed to an injected `(profile_dir, display, uid) -> None` callback the
# agent registers (see `agent.wiring`), keeping onboarding.py agent-free.
_profile_seeder = None


def set_profile_seeder(seeder) -> None:
    """Register the agent-side profile seeder used during provisioning."""
    global _profile_seeder
    _profile_seeder = seeder


def provision_user(base_settings: Settings, account_id: str, display: str) -> str:
    """Transactionally create a new tenant for `account_id` with display name
    `display`. Under the shared lock: mint an opaque uid, register + bind the
    account, create the data dir, seed a minimal profile and an EMPTY
    `config.env` skeleton (no credentials copied from anyone). On **any**
    failure after `add_user`, roll back (remove the record + the partial dir)
    and re-raise, so a failure never leaves a half-provisioned user. Returns the
    uid."""
    if _profile_seeder is None:
        raise RuntimeError("profile seeder not configured — import "
                           "assistant.agent.wiring to register it")

    reg = UserRegistry(base_settings.data_dir)
    with _path_lock(_lock_path(base_settings.shared_dir)):
        uid = _new_uid(reg)
        udir = user_data_dir(base_settings.data_dir / "users", uid)
        reg.add_user(uid, display=display)
        try:
            reg.bind_channel(uid, "weixin", account_id)
            udir.mkdir(parents=True, exist_ok=True)
            # Derive paths from `base_settings` (its own data_dir), NOT
            # Settings.for_user(uid) — the latter re-reads the repo .env and
            # would resolve to the real deployment root, breaking test
            # isolation and any non-default base. profile_dir mirrors
            # Settings.profile_dir = <data_dir>/profile.
            profile_dir = udir / "profile"
            _profile_seeder(profile_dir, display, uid)
            cfg = udir / "config.env"
            if not cfg.exists():
                cfg.touch(mode=0o600)
                cfg.write_text(_CONFIG_ENV_SKELETON.format(uid=uid, display=display))
        except Exception:
            log.exception("provision_user %s failed — rolling back", uid)
            try:
                reg.remove_user(uid)
            finally:
                import shutil

                if udir.exists():
                    shutil.rmtree(udir, ignore_errors=True)
            raise
    return uid


SAFE = "系统暂时不可用，请稍后再试 🙏"
_ASK_CODE = "你好！请发送邀请码开始使用 👋"
_BAD_CODE = "邀请码无效或已过期，请检查后重发 🙏"
_ASK_NAME = "邀请码有效 ✅ 请回复你想让我怎么称呼你（昵称）"


def handle(account_id: str, text: str, base_settings: Settings) -> str:
    """Run one onboarding turn for an unknown `account_id` and return the reply.
    Only reached in multi_tenant, behind a valid bridge token, for an accountId
    with no bound user (identity.onboarding_candidate). Two steps: a valid
    invite code → ask for a name → provision the tenant. Bad codes are bounded
    (then the account goes quiet); an already-bound account never reaches here."""
    account_id = str(account_id)
    reg = UserRegistry(base_settings.data_dir)
    if reg.by_channel("weixin", account_id):     # defensive: already onboarded
        return SAFE
    store = OnboardingStore(base_settings.shared_dir)
    invites = InviteStore(base_settings.shared_dir)
    session = store.get(account_id)
    text = str(text or "").strip()

    if session.get("state") == "awaiting_name":
        name = text[:_MAX_NAME].strip()
        if not name:
            return _ASK_NAME
        try:
            uid = provision_user(base_settings, account_id, name)
            invites.mark_used(account_id)
            store.clear(account_id)
        except Exception:
            log.exception("onboarding provision failed for %s", account_id)
            return "创建账户时出错了，请稍后再发一次你的名字 🙏"
        log.info("onboarded %s → %s", account_id, uid)
        return (f"欢迎，{name}！你的助理已就绪 🎉\n"
                "现在就能用：记账、记录饮食运动、待办、提醒、找资料、跑多步任务。\n"
                "要开启 GitHub / 邮件摘要 / 个人网站等功能，需要把对应凭据加到你的配置里"
                "（管理员协助）。直接跟我说话就行。")

    # awaiting_code (default)
    result = invites.reserve(text, account_id)
    if result == "reserved":
        store.set(account_id, state="awaiting_name", attempts=0)
        return _ASK_NAME
    attempts = int(session.get("attempts", 0)) + 1
    if attempts >= _MAX_CODE_ATTEMPTS:
        store.set(account_id, state="awaiting_code", attempts=attempts)
        return SAFE                              # bound abuse — go quiet
    store.set(account_id, state="awaiting_code", attempts=attempts)
    return _BAD_CODE if text else _ASK_CODE
