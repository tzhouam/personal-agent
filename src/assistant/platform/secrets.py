"""Secret masking for anything durable.

A single chokepoint (`mask_secrets`) that redacts credential-shaped substrings to
a legible-but-useless fingerprint (`ghp_…aB12`). Wired into the two places a
pasted token could otherwise persist: the chat-history writer (`SessionStore`)
and the trace writer. A token still transits the one live LLM request that routes
a `connect_github` turn — unavoidable for natural-language routing — but nothing
on disk retains it.
"""

import re

# Prefixed GitHub tokens only (classic ghp_/gho_/ghu_/ghs_/ghr_ + 36 chars, and
# fine-grained github_pat_…). The bare-40-hex classic form is deliberately NOT
# matched — it is indistinguishable from a commit sha and masking those would
# corrupt legitimate history.
_TOKEN_RE = re.compile(r"(github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{36})")


def _mask_one(match: re.Match) -> str:
    tok = match.group(0)
    if tok.startswith("github_pat_"):
        return f"github_pat_…{tok[-4:]}"
    return f"{tok[:4]}…{tok[-4:]}"   # ghp_ / gho_ … keep the 4-char prefix


def mask_secrets(text):
    """Return `text` with any credential-shaped substring masked to
    `<prefix>…<last4>`. Non-strings and empties pass through unchanged, so this
    is safe to drop into a serialization path."""
    if not text:
        return text
    return _TOKEN_RE.sub(_mask_one, str(text))


# Token *finder* — deliberately more permissive than the masker (allows longer
# tails) so a real, full token pasted in chat is extracted verbatim.
_FIND_RE = re.compile(r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{36,}")


def find_github_token(text) -> str:
    """The first full GitHub token in `text`, or ''. Also retries with spaces and
    newlines stripped, so a paste that wrapped mid-token still resolves. This is
    the RELIABLE source of a token — never trust an LLM to echo a 90-char secret
    (on a retry it emits the masked `github_pat_…KFh` from history)."""
    raw = str(text or "")
    for candidate in (raw, raw.replace(" ", "").replace("\n", "").replace("\t", "")):
        match = _FIND_RE.search(candidate)
        if match:
            return match.group(0)
    return ""


def looks_like_github_token(value) -> str | None:
    """Return the token if `value` is a syntactically valid, ASCII GitHub token,
    else None. Rejects a masked/partial token (the `…` fails `isascii`), so it
    never reaches an HTTP header (which would crash on the non-ASCII char)."""
    value = str(value or "").strip()
    if value and value.isascii() and _FIND_RE.fullmatch(value):
        return value
    return None
