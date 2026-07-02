import re

_GH_REF = re.compile(r"github\.com/[^/]+/[^/]+/(pull|issues|discussions)/(\d+)")
_KIND = {"pull": "PR", "issues": "Issue", "discussions": "Discussion"}


def ref_label(url: str | None, title: str = "", type_hint: str = "") -> str | None:
    """Short link label for an item — '[PR #4803]'-style instead of hyperlinking
    whole sentences. None when no sensible label exists (caller decides fallback)."""
    if not url:
        return None
    match = _GH_REF.search(url)
    if match:
        kind = _KIND[match.group(1)]
        if "rfc" in title.lower() or "rfc" in type_hint.lower():
            kind = "RFC"
        return f"{kind} #{match.group(2)}"
    if "arxiv.org" in url:
        return "Paper"
    if "/releases" in url:
        return "Release"
    return None
