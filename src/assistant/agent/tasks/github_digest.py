"""GitHub notification triage: sort notifications into red/yellow/white priority
buckets with one-sentence summaries. The LLM refines a deterministic
reason-based pre-bucketing and never invents — anything it drops (or the whole
call on failure) falls back to `_REASON_PRIORITY`. Exports `build_digest`."""

import json

from assistant.platform.llm import LLM
from assistant.agent.profile_store import render_summary

_MAX_TO_LLM = 60

# Deterministic pre-buckets by notification reason — the LLM refines, never
# invents; anything it drops falls back to these.
_REASON_PRIORITY = {
    "review_requested": "red",
    "mention": "red",
    "assign": "red",
    "team_mention": "red",
    "author": "yellow",
    "comment": "yellow",
    "manual": "yellow",
    "state_change": "white",
    "subscribed": "white",
    "ci_activity": "white",
    "security_alert": "red",
}

_SYSTEM = """You triage GitHub notifications for your owner. For each notification decide
priority and write a one-sentence summary in second person ("your PR", "you were asked...").

priority: "red" = owner must act (review requested, mentioned, assigned, CI red on own PR,
security alert); "yellow" = worth reading (activity on owner's threads, releases of deps the
owner uses); "white" = FYI only.

Use the owner profile to judge relevance (their repos and projects matter more).

Respond with ONLY a JSON array:
[{"id": "<notification id>", "priority": "red|yellow|white",
  "summary": "<one sentence>", "action": "<short suggested action or null>",
  "todo": "<short imperative label, max 8 words, e.g. 'Review GGUF plugin migration PR'>"}]
Include every notification id you were given exactly once."""


def build_digest(llm: LLM, profile: dict, notifications: list[dict], activity: list[dict]) -> dict:
    """Triage `notifications` into red/yellow/white sections and return them with
    counts.

    Only the first `_MAX_TO_LLM` go to the model (with `profile` and the owner's
    recent `activity` as relevance context); the rest are appended to white as
    FYI so nothing is silently dropped. Each notification falls back to its
    `_REASON_PRIORITY` bucket and a `[reason] title` summary when the model
    omits it or the call fails, so triage always covers every id."""
    sections = {"red": [], "yellow": [], "white": []}
    if not notifications:
        return {"sections": sections, "total": 0, "overflow": 0}

    head, overflow = notifications[:_MAX_TO_LLM], notifications[_MAX_TO_LLM:]

    activity_recap = "\n".join(f"- {o['title']}" for o in activity[:20]) or "(none)"
    notif_lines = "\n".join(
        json.dumps(
            {k: n[k] for k in ("id", "repo", "reason", "type", "title")}, ensure_ascii=False
        )
        for n in head
    )
    prompt = (
        f"## Owner profile\n{render_summary(profile)}\n\n"
        f"## Owner's own recent activity (context)\n{activity_recap}\n\n"
        f"## Notifications to triage\n{notif_lines}"
    )

    by_id = {str(n["id"]): n for n in head}
    triaged: dict[str, dict] = {}
    try:
        for item in llm.complete_json(prompt, system=_SYSTEM, max_tokens=6000, role="pipeline"):
            nid = str(item.get("id", ""))
            if nid in by_id and item.get("priority") in sections:
                triaged[nid] = item
    except Exception:
        triaged = {}  # full fallback to deterministic buckets below

    for nid, n in by_id.items():
        item = triaged.get(nid)
        priority = item["priority"] if item else _REASON_PRIORITY.get(n["reason"], "white")
        sections[priority].append(
            {
                **n,
                "summary": (item or {}).get("summary") or f"[{n['reason']}] {n['title']}",
                "action": (item or {}).get("action"),
                "todo": (item or {}).get("todo"),
            }
        )

    for n in overflow:  # never silently dropped — surfaced as FYI
        sections["white"].append({**n, "summary": f"[{n['reason']}] {n['title']}", "action": None})

    return {
        "sections": sections,
        "total": len(notifications),
        "overflow": len(overflow),
        "llm_triaged": len(triaged),
    }
