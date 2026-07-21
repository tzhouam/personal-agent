#!/usr/bin/env python
"""Local plan reviewer — the development-process review gate, with NO outside
tools: the critique runs through the personal-agent's own LLM stack against
the owner's configured endpoints.

Reviewer resolution (strongest available, printed before the run):
1. ``LLM_REVIEW`` in `.env` — the dedicated slot the owner points at a more
   powerful model (`{"model": ..., "base_url"?, "api_key"?}`).
2. The MoA aggregator from ``LLM_MIXTURE`` (the strongest model already
   configured).
3. The default ``ANTHROPIC_MODEL``.

Usage:
    /rebase/.venv/bin/python scripts/review_plan.py PLAN.md [--context "..."]

Writes ``PLAN.review.md`` beside the plan. Exit codes: 0 = APPROVE,
1 = APPROVE-WITH-CHANGES (incorporate the must-fixes, THEN proceed — distinct
from 0 so an automated gate can never auto-pass on required changes),
2 = REVISE (revise and re-review before executing), 3 = reviewer unavailable
or no verdict (do NOT execute unreviewed — fix the reviewer first).

This is a development-process tool: nothing in the assistant runtime imports
or invokes it.
"""

import argparse
import re
import sys
from pathlib import Path

_PROMPT = """You are a rigorous staff-level engineer reviewing an implementation PLAN written by
another AI agent for the personal-agent repo (a local-first daily assistant: typed-action
registry as the only mutation surface, git-versioned YAML stores under per-user write locks,
a tiered background-task runner with a risky-action approval gate, an OpenClaw WeChat bridge,
optional multi-tenant mode). Do NOT rewrite the plan. Critique it — be specific, concise, and
skeptical:

1. Correctness & feasibility — wrong assumptions, missing steps, broken ordering.
2. Risks & blast radius — irreversible / data-loss / security-sensitive steps; tenant
   isolation; approval-gate or never-delete violations.
3. Gaps — unhandled edge cases, missing verification or rollback, unstated dependencies.
4. Scope — over- or under-engineered; is there a materially simpler path?

Don't pad sections that are fine. End with EXACTLY one verdict line:
APPROVE / APPROVE-WITH-CHANGES / REVISE — plus the top must-fix items if not APPROVE.
{context}
=== PLAN UNDER REVIEW ===
{plan}"""

# Tolerant of the phrasings models actually produce ("APPROVE WITH CHANGES",
# "Verdict: REVISE", bold markers, trailing colons/text on the same line).
_VERDICT_RE = re.compile(
    r"\b(APPROVE[-\s_]WITH[-\s_]CHANGES|APPROVED?|REVISE)\b", re.IGNORECASE)


def build_prompt(plan: str, context: str = "") -> str:
    """The full critique prompt for `plan` (optional extra `context` note)."""
    ctx = f"\nExtra context from the author: {context}\n" if context.strip() else ""
    return _PROMPT.format(context=ctx, plan=plan)


def parse_verdict(critique: str) -> str | None:
    """The LAST verdict token in the critique (models often restate earlier
    options while reasoning; the final line is the contract), normalized to
    APPROVE | APPROVE-WITH-CHANGES | REVISE — or None when absent (treated as
    reviewer-unavailable by the caller, never as approval)."""
    hits = _VERDICT_RE.findall(critique or "")
    if not hits:
        return None
    last = hits[-1].upper()
    if last.startswith("APPROVE") and "WITH" in last:
        return "APPROVE-WITH-CHANGES"
    return "APPROVE" if last.startswith("APPROVE") else "REVISE"


def resolve_reviewer(settings) -> dict:
    """The reviewer model spec, strongest-first: LLM_REVIEW → the mixture
    aggregator → the default model."""
    review = settings.llm_review or {}
    if review.get("model"):
        return dict(review)
    agg = (settings.llm_mixture or {}).get("aggregator") or {}
    if agg.get("model"):
        return dict(agg)
    return {"model": settings.anthropic_model}


def review_file(plan_path: Path, context: str = "") -> int:
    """Review `plan_path`, write `<plan>.review.md` beside it, print the
    critique, and return the exit code (0/2/3)."""
    from assistant.platform.config import Settings
    from assistant.platform.llm import LLM

    plan = plan_path.read_text()
    settings = Settings()
    spec = resolve_reviewer(settings)
    llm = LLM(settings)
    llm.roles["review"] = spec
    print(f"reviewer: {spec['model']} (local stack — no outside tools)")
    try:
        # 8k budget: a truncated critique that loses its verdict line must be
        # rare, and a lost verdict is treated as unavailable, never approval
        critique = llm.complete(build_prompt(plan, context), role="review",
                                mixture=False, max_tokens=8192)
    except Exception as exc:
        print(f"reviewer unavailable: {exc}", file=sys.stderr)
        return 3
    out = plan_path.with_suffix(plan_path.suffix + ".review.md")
    out.write_text(f"# Review of {plan_path.name} — {spec['model']}\n\n{critique}\n")
    print(critique)
    print(f"\n(review saved: {out})")
    verdict = parse_verdict(critique)
    if verdict is None:
        print("no verdict line found — treating as unavailable", file=sys.stderr)
        return 3
    return {"APPROVE": 0, "APPROVE-WITH-CHANGES": 1}.get(verdict, 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plan", help="path to the plan file to review")
    parser.add_argument("--context", default="",
                        help="one-line extra context for the reviewer")
    args = parser.parse_args()
    plan_path = Path(args.plan)
    if not plan_path.is_file():
        print(f"plan file not found: {plan_path}", file=sys.stderr)
        return 3
    return review_file(plan_path, args.context)


if __name__ == "__main__":
    sys.exit(main())
