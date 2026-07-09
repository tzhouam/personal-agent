from datetime import date

import yaml

from ..llm import LLM
from ..profile_store import ProfileStore, append_ops_log, load_aliases, recent_ops, render_initiatives

_MAX_OBSERVATIONS = 120

_SYSTEM = """You maintain a structured profile of your owner (a software/ML engineer).
Given the current profile.yaml, the known initiatives, the last week's applied ops, and today's
activity observations, emit patch operations.

Respond with ONLY a JSON object: {"ops": [...], "notes": "<=2 sentences"}

Allowed ops (any other op, or any op touching identity/education/experience/preferences, is rejected):
  {"op": "bump_last_seen", "section": "skills|interests|projects", "name": "<entry name/topic>"}
  {"op": "add_evidence", "section": "skills|projects", "name": "...", "evidence": "<short cited fact, include obs URL/repo>", "initiative": "<initiative or none>"}
  {"op": "add_skill", "name": "...", "evidence": ["..."], "initiative": "..."}   # starts as level=emerging
  {"op": "add_interest", "topic": "...", "weight": 0.5}
  {"op": "add_project", "name": "...", "role": "...", "highlights": ["..."], "evidence": ["..."], "initiative": "..."}
  {"op": "update_highlight", "section": "projects", "name": "...", "highlight": "<concrete, resume-worthy achievement>"}
  {"op": "merge_projects", "into": "<entry that owns the work>", "from": "<fragment entry>"}
  {"op": "move_evidence", "from": "<entry>", "to": "<entry>", "match": "<substring identifying the misfiled evidence>"}
  {"op": "mark_dormant", "section": "skills|interests", "name": "..."}     # never used on same-day evidence

Rules:
- Every claim must be grounded in the observations shown — never invent.
- INITIATIVES ARE THE JOIN KEY: when an observation matches an initiative's signals, attach the
  evidence to that initiative's profile entry (add_evidence/bump on it) — never create a new
  project entry for a repo that belongs to an existing initiative. add_* ops must name their
  "initiative" (or "none" for genuinely unrelated work).
- If you notice the SAME line of work split across entries, or evidence filed under the wrong
  entry, fix it with merge_projects / move_evidence instead of adding more.
- WRITE GATE: transient one-offs (a single page visit, a lone drive-by comment, routine bot
  activity) get NO op at all — they stay in the raw event log. Evidence must advance a skill,
  project, or initiative.
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
    aliases = load_aliases(store.dir)
    week_ops = recent_ops(store.dir, days=7)
    ops_lines = [
        f"- {o.get('date')}: {o.get('op')} {o.get('section', 'projects')}"
        f"/{o.get('name') or o.get('into') or o.get('topic', '')}"
        for o in week_ops
    ]
    prompt = (
        f"Today is {today}.\n\n## Current profile.yaml\n```yaml\n"
        f"{yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)}\n```\n\n"
        f"## Known initiatives (join keys — attach matching work here)\n"
        f"{render_initiatives(aliases)}\n\n"
        f"## Ops applied in the last 7 days (the arc your update continues)\n"
        + ("\n".join(ops_lines) or "(none)")
        + f"\n\n## Today's observations ({len(observations)} total"
        + (f", showing first {_MAX_OBSERVATIONS}" if dropped else "")
        + ")\n"
        + ("\n".join(obs_lines) or "(no activity observed)")
    )

    result = llm.complete_json(prompt, system=_SYSTEM, max_tokens=8000)
    ops = result.get("ops", []) if isinstance(result, dict) else []

    profile, applied, rejected = store.apply_ops(profile, ops, today=today)
    diff = store.save(profile, f"daily update {today} ({len(applied)} ops)") if applied else ""
    append_ops_log(store.dir, applied, today)

    return {
        "profile_diff": diff,
        "profile_ops": applied,
        "rejected_ops": rejected,
        "notes": result.get("notes", "") if isinstance(result, dict) else "",
    }
