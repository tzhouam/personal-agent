from datetime import date

import yaml

from ..llm import LLM
from ..profile_store import ProfileStore

_MAX_OBSERVATIONS = 120

_SYSTEM = """You maintain a structured profile of your owner (a software/ML engineer).
Given the current profile.yaml and today's activity observations, emit patch operations.

Respond with ONLY a JSON object: {"ops": [...], "notes": "<=2 sentences"}

Allowed ops (any other op, or any op touching identity/education/experience/preferences, is rejected):
  {"op": "bump_last_seen", "section": "skills|interests|projects", "name": "<entry name/topic>"}
  {"op": "add_evidence", "section": "skills|projects", "name": "...", "evidence": "<short cited fact, include obs URL/repo>"}
  {"op": "add_skill", "name": "...", "evidence": ["..."]}                  # starts as level=emerging
  {"op": "add_interest", "topic": "...", "weight": 0.5}
  {"op": "add_project", "name": "...", "role": "...", "highlights": ["..."], "evidence": ["..."]}
  {"op": "update_highlight", "section": "projects", "name": "...", "highlight": "<concrete, resume-worthy achievement>"}
  {"op": "mark_dormant", "section": "skills|interests", "name": "..."}     # never used on same-day evidence

Rules:
- Every claim must be grounded in the observations shown — never invent.
- Prefer bump_last_seen / add_evidence on existing entries over creating new ones.
- New skills/interests only when observations show sustained or significant engagement, not a single visit.
- Respect preferences.avoid_topics: never add or reactivate skills/interests/projects in those
  areas; entries already dormant stay dormant unless strong fresh evidence contradicts it.
- At most 15 ops. Empty ops list is a valid answer for an uneventful day."""


def update_profile(llm: LLM, store: ProfileStore, observations: list[dict]) -> dict:
    profile = store.load()
    today = date.today().isoformat()

    obs_lines = [
        f"[obs-{i}] {o.get('ts', '')} {o.get('source')}/{o.get('kind')}: "
        f"{o.get('title')} ({o.get('url') or 'no url'})"
        for i, o in enumerate(observations[:_MAX_OBSERVATIONS])
    ]
    dropped = max(0, len(observations) - _MAX_OBSERVATIONS)
    prompt = (
        f"Today is {today}.\n\n## Current profile.yaml\n```yaml\n"
        f"{yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)}\n```\n\n"
        f"## Today's observations ({len(observations)} total"
        + (f", showing first {_MAX_OBSERVATIONS}" if dropped else "")
        + ")\n"
        + ("\n".join(obs_lines) or "(no activity observed)")
    )

    result = llm.complete_json(prompt, system=_SYSTEM, max_tokens=8000)
    ops = result.get("ops", []) if isinstance(result, dict) else []

    profile, applied, rejected = store.apply_ops(profile, ops, today=today)
    diff = store.save(profile, f"daily update {today} ({len(applied)} ops)") if applied else ""

    return {
        "profile_diff": diff,
        "profile_ops": applied,
        "rejected_ops": rejected,
        "notes": result.get("notes", "") if isinstance(result, dict) else "",
    }
