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
    omits it or the call fails, so triage always covers every id. The returned
    accounting distinguishes an LLM exception (`llm_error`) from an otherwise
    valid response that omitted requested IDs (`partial_response`)."""
    sections = {"red": [], "yellow": [], "white": []}
    if not notifications:
        return {
            "sections": sections,
            "total": 0,
            "overflow": 0,
            "llm_requested": 0,
            "llm_triaged": 0,
            "fallback_count": 0,
            "degraded": False,
            "fallback_reason_code": "none",
        }

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
    fallback_reason_code = "none"
    try:
        response = llm.complete_json(
            prompt, system=_SYSTEM, max_tokens=6000, role="pipeline")
    except Exception:
        fallback_reason_code = "llm_error"
        response = []
    if not isinstance(response, list):
        fallback_reason_code = "malformed_response"
        response = []
    malformed = False
    for item in response:
        if not isinstance(item, dict):
            malformed = True
            continue
        nid = str(item.get("id", ""))
        priority = item.get("priority")
        summary = item.get("summary")
        action = item.get("action")
        todo = item.get("todo")
        if (nid not in by_id or not isinstance(priority, str)
                or priority not in sections
                or not isinstance(summary, str) or not summary.strip()
                or (action is not None and not isinstance(action, str))
                or (todo is not None and not isinstance(todo, str))):
            malformed = True
            continue
        triaged[nid] = item
    if malformed and fallback_reason_code == "none":
        fallback_reason_code = "malformed_response"

    missing_requested = len(head) - len(triaged)
    # Overflow is deliberately not sent to the LLM, but it is still rendered
    # through the deterministic fallback and therefore belongs in this count.
    fallback_count = len(notifications) - len(triaged)
    if fallback_count and fallback_reason_code == "none":
        fallback_reason_code = "partial_response" if missing_requested else "overflow"

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
        "llm_requested": len(head),
        "llm_triaged": len(triaged),
        "fallback_count": fallback_count,
        "degraded": fallback_count > 0,
        "fallback_reason_code": fallback_reason_code,
    }
