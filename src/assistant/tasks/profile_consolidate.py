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
   owning entry; misfiled evidence is moved to where it belongs BEFORE rewriting. An initiative's
   own `entry` is canonical — merge fragments INTO it, never merge it into something else.
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


_JUDGE_SYSTEM = """You audit the owner's auto-maintained profile for quality. Check ONLY the
material given — never outside knowledge. Be conservative: report only clear cases.

Respond with ONLY JSON:
{"contradictions": [{"where": "<entry>", "detail": "<claim A vs claim B>"}],
 "stale": [{"where": "<entry>", "detail": "<active claim superseded by which newer evidence>"}],
 "unsupported": [{"where": "<entry>", "detail": "<highlight with no supporting evidence line>"}],
 "claims_checked": <int>}

- contradiction: two profile statements that cannot both be true (conflicting numbers, dates,
  RFC/PR ids, roles).
- stale: an active claim that the newer evidence shown clearly supersedes.
- unsupported: a highlight asserting something no evidence line in its own entry backs."""


def judge_profile(llm: LLM, store: ProfileStore, settings: Settings) -> dict:
    """Weekly LLM-judge audit (doc/PIPELINE_METRICS.md §2: faithfulness /
    staleness / contradiction). Read-only — findings are surfaced, never
    auto-fixed; the consolidator or owner acts on them next cycle."""
    from ..events_store import EventsStore

    profile = store.load()
    auditable = {s: [e for e in profile.get(s, []) if e.get("status") == "active"]
                 for s in ("skills", "projects")}
    events = EventsStore(settings.events_db)
    try:
        recent = events.conn.execute(
            "SELECT ts, title FROM observations WHERE ts >= date('now', '-30 day')"
            " ORDER BY ts DESC LIMIT 80").fetchall()
    finally:
        events.close()
    recent_lines = "\n".join(f"- {ts[:10]} {title[:160]}" for ts, title in recent)

    result = llm.complete_json(
        f"## Profile (active entries)\n```yaml\n"
        f"{yaml.safe_dump(auditable, sort_keys=False, allow_unicode=True)}```\n\n"
        f"## Recent evidence (last 30 days of observations)\n{recent_lines or '(none)'}",
        system=_JUDGE_SYSTEM, max_tokens=16000)
    if not isinstance(result, dict):
        raise ValueError("judge returned non-dict")
    return {
        "contradictions": result.get("contradictions", []) or [],
        "stale": result.get("stale", []) or [],
        "unsupported": result.get("unsupported", []) or [],
        "claims_checked": int(result.get("claims_checked", 0) or 0),
    }


def consolidate_profile(llm: LLM, store: ProfileStore, settings: Settings,
                        section: str | None = None, dry_run: bool = False) -> dict:
    """Run the weekly editorial pass over one `section` (or all of projects/
    skills/interests) and return {applied, rejected, judge, notes, diff, emailed}.

    Each section is sent in full so the model may rewrite/merge/dedupe under
    `CONSOLIDATE_OPS`, using the owner's hand-written experience as the style
    reference. A read-only LLM-judge audit then records quality metrics; applied
    ops are committed as one git commit and the diff plus findings are emailed
    (the audit gate). `dry_run` computes ops and findings but writes/emails
    nothing."""
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
            result = llm.complete_json(prompt, system=_SYSTEM, max_tokens=8000, role="pipeline")
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

    # weekly quality audit — runs even on a 0-op week; findings are metrics
    # + email content, never auto-fixes (doc/PIPELINE_METRICS.md §2)
    judge = {"contradictions": [], "stale": [], "unsupported": [], "claims_checked": 0}
    if not dry_run:
        try:
            judge = judge_profile(llm, store, settings)
            from ..events_store import EventsStore

            events = EventsStore(settings.events_db)
            events.record_metrics(f"consolidate-{today}", "consolidate", {
                "contradictions": len(judge["contradictions"]),
                "stale_claims": len(judge["stale"]),
                "unsupported_claims": len(judge["unsupported"]),
                "claims_checked": judge["claims_checked"]})
            events.close()
            log.info("profile judge: %d contradictions, %d stale, %d unsupported "
                     "(%d claims checked)", len(judge["contradictions"]),
                     len(judge["stale"]), len(judge["unsupported"]),
                     judge["claims_checked"])
        except Exception:
            log.exception("profile judge failed (consolidation unaffected)")

    findings = [f"{kind}: {f.get('where', '?')} — {f.get('detail', '')}"
                for kind, items in (("contradiction", judge["contradictions"]),
                                    ("stale", judge["stale"]),
                                    ("unsupported", judge["unsupported"]))
                for f in items]

    if dry_run or not (all_applied or findings):
        return {"applied": all_applied, "rejected": all_rejected, "judge": judge,
                "notes": " | ".join(notes), "diff": "", "emailed": False}

    diff = ""
    if all_applied:
        diff = store.save(profile, f"weekly consolidation {today} ({len(all_applied)} ops)")
        append_ops_log(store.dir, all_applied, today)

    emailed = False
    try:
        import html as _html
        body = (
            f"<p>Weekly profile consolidation applied {len(all_applied)} ops "
            f"({len(all_rejected)} rejected).</p>"
            + (f"<p>{_html.escape(' | '.join(notes))}</p>" if notes else "")
            + (("<h4>⚖️ Quality audit findings</h4><ul>"
                + "".join(f"<li>{_html.escape(line)}</li>" for line in findings[:12])
                + f"</ul><p style='font-size:12px'>{judge['claims_checked']} claims checked; "
                  "findings are surfaced, not auto-fixed.</p>") if findings else
               f"<p>⚖️ Quality audit: clean ({judge['claims_checked']} claims checked).</p>")
            + (f"<pre style='font-size:12px'>{_html.escape(diff[:12000])}</pre>" if diff else "")
            + f"<p>Rollback: <code>git -C ~/.personal-agent/profile revert HEAD</code></p>"
        )
        send_email(settings, f"[assistant] Profile consolidation — {today}", body)
        emailed = True
    except Exception:
        log.exception("consolidation email failed (profile change is committed)")

    return {"applied": all_applied, "rejected": all_rejected, "judge": judge,
            "notes": " | ".join(notes), "diff": diff, "emailed": emailed}
