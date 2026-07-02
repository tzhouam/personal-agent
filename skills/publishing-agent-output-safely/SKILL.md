---
name: publishing-agent-output-safely
description: Agent-published outward-facing artifacts (resume, personal website) — deterministic renders for public pages, approval gates for prose, compile checks as gates, pull-rebase-never-force, and know that Overleaf has no public API
trigger: designing any agent flow that writes to a public site, resume, or other outward-facing destination
modules: [publishing, resume, website]
status: active
created_at: 2026-07-02
last_used_at: 2026-07-02
run_count: 0
---

## Diagnose
Outward-facing surfaces are where LLM hallucination becomes reputational
damage, and where the agent can clobber the owner's manual edits. Each surface
needs an explicit safety posture, chosen deliberately.

## Fix
Postures used in this repo, strongest first — pick per surface:
1. **Deterministic render, no LLM** (website, `src/assistant/website.py`):
   the page is a pure template over evidence-gated profile.yaml — nothing
   fabricated can appear, which is what made owner-approved direct pushes
   acceptable. Any LLM-written surface should NOT get direct-push.
2. **Approval gate** (resume, `tasks/resume.py`): LLM edits commit locally
   only; the diff goes in the daily email; `assistant approve-resume` pushes.
3. **Machine verification as a hard gate**: a resume edit that fails
   `latexmk` is rolled back, not shipped ("compiles" is the minimum bar for
   generated LaTeX).
4. **Respect concurrent human edits**: always `git pull --rebase` the remote
   before pushing; on conflict, abort and surface in the email. NEVER
   force-push an owner-owned repo.
5. **Constrain the LLM's write surface**: exact search/replace edits that must
   match exactly once (`apply_edits`) — ambiguous or unmatched edits are
   rejected, not fuzzily applied.
6. Overleaf specifically: there is **no public API**. Options are the git
   bridge (premium), GitHub Sync (premium), or brittle browser automation —
   design for the git bridge and gate on the owner having premium.

## Verification
`test_resume.py` covers: local-commit-only + pending marker, compile-failure
rollback, ambiguous-edit rejection. For the website: profile change → run →
site diff contains exactly the profile facts.

## Anti-patterns
- LLM free-writing a public page "because it's reviewed in the email" — emails
  get skimmed; determinism is the only real guarantee.
- Force-pushing to recover from a conflict with the owner's edits.
- Auto-pushing a resume because "the profile is evidence-gated" — wording and
  emphasis are judgment calls the owner must see before the world does.
