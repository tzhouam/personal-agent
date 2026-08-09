"""Secret masking for anything durable.

A single chokepoint (`mask_secrets`) that redacts credential-shaped substrings to
a legible-but-useless fingerprint (`ghp_…aB12`). Wired into the three places a
pasted token could otherwise persist: the chat-history writer (`SessionStore`),
the trace writer, and the **daemon log** — the poll loops log inbound message
text, so a token pasted over email used to land verbatim in a never-rotated,
world-readable `serve.log`. That sink is covered twice: at the call sites and by
`serve._MaskingFilter` on the root handlers, so a future log line cannot
reintroduce it. A token still transits the one live LLM request that routes a
`connect_github` turn — unavoidable for natural-language routing — and, by
design, the owner's own `config.env` (0600). Nothing else on disk retains it.
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


# Token *finder*. Classic tokens have a FIXED length (gh[pousr]_ + exactly 36
# base62 chars — verify against GitHub's token-formats docs when they evolve);
# fine-grained github_pat_ has a documented shape of 82 chars of [A-Za-z0-9_]
# after the prefix. Bounded quantifiers + boundary guards, because the old
# open-ended `{20,}`/`{36,}` finder happily swallowed glued prose: stripping
# whitespace from "github_pat_XXX… thanks a lot" produced a "token" ending in
# `thanksalot`, GitHub 401'd, and the owner was told to check their token
# (2026-07 audit finding F19). A format GitHub changes degrades to an honest
# "no token found — paste it on a line by itself", never a corrupted
# credential.
_CLASSIC_LEN = 36
_FINE_LEN = 82
_FIND_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?:github_pat_[A-Za-z0-9_]{{{_FINE_LEN}}}"
    rf"|gh[pousr]_[A-Za-z0-9]{{{_CLASSIC_LEN}}})(?![A-Za-z0-9_])")
# The wrapped-paste recovery pass strips whitespace, where boundary guards
# can't distinguish token from glued prose — so it demands the ENTIRE
# stripped text be one token, nothing else.
_FULL_RE = re.compile(
    rf"(?:github_pat_[A-Za-z0-9_]{{{_FINE_LEN}}}"
    rf"|gh[pousr]_[A-Za-z0-9]{{{_CLASSIC_LEN}}})")


def find_github_token(text) -> str:
    """The first full GitHub token in `text`, or ''. Pass 1 finds a properly
    delimited token in the raw text (prose around it is fine). Pass 2 handles
    a paste that WRAPPED mid-token: whitespace is stripped, and the result
    counts only when the entire stripped text is exactly one token — glued
    prose is ambiguous and returns '' (the caller asks for a clean re-paste)
    rather than risking a corrupted credential. This is the RELIABLE source
    of a token — never trust an LLM to echo a 90-char secret (on a retry it
    emits the masked `github_pat_…KFh` from history)."""
    raw = str(text or "")
    match = _FIND_RE.search(raw)
    if match:
        return match.group(0)
    stripped = re.sub(r"\s+", "", raw)
    if _FULL_RE.fullmatch(stripped):
        return stripped
    return ""


def looks_like_github_token(value) -> str | None:
    """Return the token if `value` is a syntactically valid, ASCII GitHub token,
    else None. Rejects a masked/partial token (the `…` fails `isascii`), so it
    never reaches an HTTP header (which would crash on the non-ASCII char)."""
    value = str(value or "").strip()
    if value and value.isascii() and _FIND_RE.fullmatch(value):
        return value
    return None
