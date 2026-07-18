"""Authenticated identity resolution — a request resolves to exactly one uid.

Never trusts a caller-supplied uid. In `single_user` everything is `DEFAULT_UID`.
In `multi_tenant` the **bridge token must verify**, and it may then assert a
channel `account_id` (→ registry lookup); a missing/invalid identity is refused
(**no default fallback**). Email is resolved by its own poller from the mailbox
context, so it bypasses this HTTP path. See doc/DESIGN_MULTI_USER.md §4.1, §6.1,
Appendix A.2.
"""

from .config import DEFAULT_UID, Settings
from .registry import UserRegistry


class Unauthorized(Exception):
    """A request could not be resolved to a user (multi_tenant). Callers turn
    this into a 401."""


class UserContext:
    """The resolved `(uid, settings)` a request runs under."""

    __slots__ = ("uid", "settings")

    def __init__(self, uid: str, settings: Settings):
        self.uid = uid
        self.settings = settings


def resolve_uid(bearer_token: str | None, body: dict | None,
                base_settings: Settings, registry: UserRegistry | None = None) -> str:
    """The uid this request resolves to, or raise `Unauthorized`.

    `single_user` → `DEFAULT_UID`. `multi_tenant` → the **bridge token** must
    verify against the registry (an empty token is never open access), and it may
    then assert `body['account_id']` (+ optional `body['channel']`, default
    `weixin`) which the registry maps to a uid. Nothing else selects a user."""
    if base_settings.deployment_mode != "multi_tenant":
        return DEFAULT_UID
    reg = registry or UserRegistry(base_settings.data_dir)
    if not (bearer_token and reg.verify_bridge_token(bearer_token)):
        raise Unauthorized("valid bridge token required")
    channel = str((body or {}).get("channel") or "weixin")
    account_id = str((body or {}).get("account_id") or "").strip()
    uid = reg.by_channel(channel, account_id) if account_id else None
    if not uid:
        raise Unauthorized(f"no active user for {channel}:{account_id or '(none)'}")
    return uid


def context_for(bearer_token: str | None, body: dict | None,
                base_settings: Settings, registry: UserRegistry | None = None) -> UserContext:
    """`resolve_uid` + `Settings.for_user` → a ready `UserContext`."""
    uid = resolve_uid(bearer_token, body, base_settings, registry)
    return UserContext(uid, Settings.for_user(uid))


def onboarding_candidate(bearer_token: str | None, body: dict | None,
                         base_settings: Settings,
                         registry: UserRegistry | None = None) -> str | None:
    """The WeChat `account_id` this request could ONBOARD, or None.

    True only when: multi_tenant, self-onboarding is enabled, the **bridge token
    verifies** (so only the trusted gateway can trigger onboarding), a weixin
    `account_id` is present, and it maps to **no** active user. Anything else —
    a bad token, another channel, an already-bound account — returns None so the
    caller keeps failing closed (401). Used only by the `/chat` route."""
    if base_settings.deployment_mode != "multi_tenant" or not base_settings.self_onboarding:
        return None
    reg = registry or UserRegistry(base_settings.data_dir)
    if not (bearer_token and reg.verify_bridge_token(bearer_token)):
        return None
    if str((body or {}).get("channel") or "weixin") != "weixin":
        return None
    account_id = str((body or {}).get("account_id") or "").strip()
    if not account_id or reg.by_channel("weixin", account_id):
        return None
    return account_id
