"""Weekly profile consolidation — the promotion machinery between the
evidence layer and the curated profile (doc/RESEARCH_AGENT_MEMORY_2026.md §4 P2).

Unlike the daily updater (one day of observations, additive bias), this pass
sees a whole section at once — every entry with its full accumulated
evidence — and is allowed to REWRITE: dedupe bullets, merge fragmented
entries, move misfiled evidence, and promote clusters of evidence into
contribution-level highlights written in resume voice.

Safety, per the 2026 literature (arXiv:2605.12978 — continuous rewriting
corrupts):
- runs on a deliberate weekly cadence, never per-interaction;
- raw evidence stays in events.db; superseded highlights land in each
  entry's `history` (audit rows), and stable entries reject rewrites that
  cite fewer sources (see profile_store.rewrite_entry);
- every run is one git commit in the profile repo (the rollback), and the
  diff is emailed to the owner (the audit gate).
"""

import logging
from datetime import date

import yaml

from ..config import Settings
from ..deliver.email import send_email
from ..llm import LLM
from ..profile_store import (CONSOLIDATE_OPS, ProfileStore, append_ops_log,
                             load_aliases, render_initiatives)
from ..writing import RESUME_VOICE_RULES

log = logging.getLogger("assistant")

_SECTIONS = ("projects", "skills", "interests")

_SYSTEM = """You are the weekly profile consolidator for your owner (a software/ML engineer).
You see one profile section IN FULL. Your job is editorial, not additive: make the section read
like the owner's strongest honest resume, grounded in the evidence already present.

Respond with ONLY a JSON object: {"ops": [...], "notes": "<=3 sentences"}

Allowed ops:
  {"op": "rewrite_entry", "section": "projects|skills|interests", "name": "...",
   "highlights": ["<resume-voice contribution statement, cites PR/RFC/URL where possible>", ...],
   "evidence": ["<optional deduped evidence list — must not lose any cited URL>"],
   "part_of": "<optional parent initiative>", "level": "<skills only: emerging|working|expert>"}
  {"op": "merge_projects", "into": "<entry that owns the work>", "from": "<fragment entry>"}
  {"op": "move_evidence", "from": "<entry>", "to": "<entry>", "match": "<substring of the misfiled evidence>"}
  {"op": "add_skill", "name": "...", "evidence": ["..."]}       # skills re-based on real capabilities
  {"op": "mark_dormant", "section": "skills|interests", "name": "..."}

Editorial rules:
1. PROMOTE, don't list: a cluster of related evidence (several PRs, an RFC + implementation, a
   multi-day arc) becomes ONE highlight stating the contribution and its scope — "Designed…",
   "Led…", "Built…". Follow the style of the STYLE REFERENCE below. Keep the strongest
   evidence URLs inside the highlight text.
2. MERGE fragments: entries that are the same line of work (see initiatives) are merged into the
   owning entry; misfiled evidence is moved to where it belongs BEFORE rewriting.
3. DEDUPE: near-duplicate highlights/evidence collapse into the better-written one. Resolve
   contradictions (e.g. inconsistent RFC/PR numbers) in favor of the version supported by more
   evidence URLs; note unresolved ones in "notes".
4. PROMOTION GATE: only evidence recurring across multiple days, or terminal events (merged PR,
   accepted RFC, shipped release), earns a highlight. Single-day crumbs stay as evidence only.
5. NEVER INVENT: every highlight must be supported by evidence visible in the section. If the
   section is already clean, return few or no ops.
6. Skills describe real capabilities ("LLM inference systems", "multi-agent orchestration"),
   not GitHub language stats. Use rewrite_entry/add_skill/mark_dormant to converge on that;
   evidence for a skill should reference the initiatives/projects that prove it.
7. Do not touch entries with status "merged". At most 20 ops.

""" + RESUME_VOICE_RULES


def consolidate_profile(llm: LLM, store: ProfileStore, settings: Settings,
                        section: str | None = None, dry_run: bool = False) -> dict:
    profile = store.load()
    today = date.today().isoformat()
    aliases = load_aliases(store.dir)
    style_reference = yaml.safe_dump(
        profile.get("experience", []), sort_keys=False, allow_unicode=True)

    sections = [section] if section else list(_SECTIONS)
    all_applied, all_rejected, notes = [], [], []
    for name in sections:
        entries = profile.get(name, [])
        if not entries:
            continue
        context = {"projects": profile.get("projects", [])} if name == "skills" else {}
        prompt = (
            f"Today is {today}.\n\n"
            f"## Known initiatives\n{render_initiatives(aliases)}\n\n"
            f"## STYLE REFERENCE (the owner's hand-written experience section — match this voice)\n"
            f"```yaml\n{style_reference}```\n\n"
            + (f"## Projects section (context for judging skills)\n```yaml\n"
               f"{yaml.safe_dump(context['projects'], sort_keys=False, allow_unicode=True)}```\n\n"
               if context else "")
            + f"## Section to consolidate: {name}\n```yaml\n"
              f"{yaml.safe_dump(entries, sort_keys=False, allow_unicode=True)}```"
        )
        try:
            result = llm.complete_json(prompt, system=_SYSTEM, max_tokens=8000)
        except Exception as exc:
            log.exception("consolidation LLM call failed for %s", name)
            notes.append(f"{name}: failed ({exc})")
            continue
        ops = result.get("ops", []) if isinstance(result, dict) else []
        for op in ops:  # section ops default to the section they came from
            op.setdefault("section", name)
        profile, applied, rejected = store.apply_ops(
            profile, ops, today=today, allowed=CONSOLIDATE_OPS)
        all_applied += applied
        all_rejected += rejected
        if isinstance(result, dict) and result.get("notes"):
            notes.append(f"{name}: {result['notes']}")
        log.info("consolidate %s: %d applied, %d rejected", name, len(applied), len(rejected))

    if dry_run or not all_applied:
        return {"applied": all_applied, "rejected": all_rejected,
                "notes": " | ".join(notes), "diff": "", "emailed": False}

    diff = store.save(profile, f"weekly consolidation {today} ({len(all_applied)} ops)")
    append_ops_log(store.dir, all_applied, today)

    emailed = False
    try:
        import html as _html
        body = (
            f"<p>Weekly profile consolidation applied {len(all_applied)} ops "
            f"({len(all_rejected)} rejected).</p>"
            + (f"<p>{_html.escape(' | '.join(notes))}</p>" if notes else "")
            + f"<pre style='font-size:12px'>{_html.escape(diff[:12000])}</pre>"
            + f"<p>Rollback: <code>git -C ~/.personal-agent/profile revert HEAD</code></p>"
        )
        send_email(settings, f"[assistant] Profile consolidation — {today}", body)
        emailed = True
    except Exception:
        log.exception("consolidation email failed (profile change is committed)")

    return {"applied": all_applied, "rejected": all_rejected,
            "notes": " | ".join(notes), "diff": diff, "emailed": emailed}
