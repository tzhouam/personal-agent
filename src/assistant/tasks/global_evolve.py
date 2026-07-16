"""Cross-user self-evolution: distill USER-AGNOSTIC behavior rules for the whole
deployment (doc/DESIGN_MULTI_USER.md §12b, layer 2).

All users of this deployment (a family/close-friends self-host) have mutually
authorized using their traces and chat history to improve the shared agent. This
weekly pass reads every **active** user's recent chat sessions, task records,
and pipeline traces, and asks the LLM for at most 3 new rules that would help
*every* user — never personal preferences (those stay in each user's own
lessons, which take precedence in prompts).

Two privacy layers keep personal content out of the shared store (whose rules
render into every user's prompts): the prompt's hard constraints, and a
deterministic post-filter that rejects any rule mentioning a registered uid,
display name, or email address. Provenance (`why`, which uid(s) evidenced the
rule) is stored for the admin listing but never rendered into prompts.

Runs under the deployment-ROOT Settings via the GLOBAL_UID queue job; admin
reviews with `assistant admin lessons list|retire`.
"""

import logging
import re

from ..config import Settings
from ..lessons_store import shared_store
from ..llm import LLM
from ..registry import UserRegistry
from .evolve import _gather_evidence, _trace_evidence

log = logging.getLogger("assistant")

PER_USER_CAP = 7000     # chars of evidence per user
TOTAL_CAP = 24000       # chars of evidence overall

_GLOBAL_EVOLVE_SYSTEM = """You improve a personal-assistant deployment shared by several users by
studying recent conversations, task runs, and pipeline traces across ALL of them. Propose durable,
USER-AGNOSTIC BEHAVIOR rules ("when X, do Y") that would help EVERY user: preventing failed or
retried actions, misunderstandings, repeated manual steps, wrong defaults, slow or truncated calls.
HARD CONSTRAINTS:
- A rule must read the same with any user substituted in. NEVER include personal facts,
  preferences, names, amounts, dates, places, or anything traceable to one user — every user
  sees these rules in their prompts.
- Personal tastes (currency, language, tone, schedules) belong to per-user lessons — skip them.
- Prefer patterns evidenced by at least 2 users when evidence from multiple users exists; accept
  a single-user pattern only when it is plainly user-independent (e.g. a tool-usage failure mode).
- Never repeat or contradict an existing shared lesson.
Respond with ONLY JSON: {"lessons": [{"rule": "<one imperative sentence>",
"why": "<which uid(s) evidenced it + what was observed, short>"}], "note": "<one line>"}
At most 3 lessons; an empty list is the right answer when nothing user-agnostic recurs."""

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def global_evolve(settings: Settings, llm: LLM | None = None) -> dict:
    """One cross-user evolve pass (`settings` = deployment ROOT). Returns
    `{reviewed, users, proposed, learned, rejected}`; a no-op shape outside
    multi_tenant."""
    if settings.deployment_mode != "multi_tenant":
        return {"reviewed": 0, "users": 0, "proposed": [], "learned": [],
                "rejected": 0}
    registry = UserRegistry(settings.data_dir)
    store = shared_store(settings)
    _ensure_repo(store.repo_dir)

    blocks: list[str] = []
    users = 0
    for uid in registry.active():
        try:
            user = Settings.for_user(uid)
            evidence = _gather_evidence(user)
            traces = _trace_evidence(user)
            if traces:
                evidence += "\n## pipeline traces\n" + traces
            if not evidence.strip():
                continue
            users += 1
            blocks.append(f"## user {uid}\n{evidence[:PER_USER_CAP]}")
        except Exception:  # one broken user never blocks the pass
            log.exception("global evolve: evidence for %s failed", uid)
    evidence_all = "\n\n".join(blocks)[:TOTAL_CAP]
    if not evidence_all.strip():
        return {"reviewed": 0, "users": 0, "proposed": [], "learned": [],
                "rejected": 0}

    llm = llm or LLM(settings)
    existing = "\n".join(f"- {l['rule']}" for l in store.active()) or "(none)"
    result = llm.complete_json(
        f"## Existing shared lessons (do not repeat or contradict)\n{existing}\n\n"
        f"## Recent evidence across users\n{evidence_all}",
        system=_GLOBAL_EVOLVE_SYSTEM, max_tokens=5000, role="evolve")
    proposed = (result.get("lessons") or []) if isinstance(result, dict) else []
    learned, rejected = [], 0
    for item in proposed[:3]:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("rule", ""))
        if _looks_user_specific(rule, registry):
            rejected += 1
            log.warning("global evolve: rejected user-specific rule: %.120s", rule)
            continue
        lesson = store.learn(rule, why=str(item.get("why", "")), source="evolve")
        if lesson:
            learned.append(lesson)
    log.info("global evolve: %d users, %d chars, %d proposed, %d learned, "
             "%d rejected", users, len(evidence_all), len(proposed),
             len(learned), rejected)
    return {"reviewed": len(evidence_all), "users": users, "proposed": proposed,
            "learned": learned, "rejected": rejected}


def _looks_user_specific(rule: str, registry: UserRegistry) -> bool:
    """Deterministic privacy backstop behind the prompt constraints: reject a
    rule that names a registered uid, a display name, or any email address —
    such a rule would leak one user's identity into everyone's prompts. Errs
    toward rejecting; rejected proposals are logged, never stored."""
    low = rule.lower()
    if _EMAIL_RE.search(rule):
        return True
    for u in registry.users():
        if str(u.get("uid", "")).lower() in low:
            return True
        display = str(u.get("display", "")).strip()
        if display and display.lower() in low:
            return True
        for c in u.get("channels", []):
            ident = str(c.get("id", "")).strip()
            if ident and ident.lower() in low:
                return True
    return False


def _ensure_repo(repo_dir) -> None:
    """Best-effort `git init` of the shared lessons dir so mutations gain the
    same audit history the personal store enjoys. Failure is fine — the store
    works on a plain dir (LessonsStore._save skips git without `.git`)."""
    import subprocess

    try:
        repo_dir.mkdir(parents=True, exist_ok=True)
        if not (repo_dir / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=repo_dir,
                           capture_output=True, timeout=15)
    except Exception:
        log.debug("shared lessons git init skipped", exc_info=True)
