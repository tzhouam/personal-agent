You are improving the **personal-agent** codebase (you are in its repo root) based on
evidence gathered from the last day's run traces, chat history, and task runs. The evidence
is appended below under `--- EVIDENCE ---`.

Your goal: make the agent measurably better at the specific frictions the evidence shows —
nothing speculative.

## Rules (hard constraints)

1. **Small and focused.** Make at MOST 1–2 tightly-scoped changes that each address a
   concrete signal in the evidence (a failed/retried action, a wrong default the owner
   corrected, a slow or truncated LLM call, an aborted task). No broad refactors, no
   renames, no dependency changes, no reformatting of untouched code.
2. **Test-backed.** Every behavior change must come with a new or adjusted test in `test/`
   that would have caught the problem. Run `/rebase/.venv/bin/python -m pytest test/ -q`
   yourself and make sure it passes before you finish.
3. **Never touch:** `.env`, `.env.*`, anything under `.git/`, credentials, tokens, the
   `docs/media/` binaries, or the git history. Do not run git, push, or network commands —
   the wrapper handles commit/push.
4. **Match the codebase.** Follow the existing style, the typed-action-registry pattern,
   the store idioms (git-versioned YAML, stated/auto time identity, never-delete), and the
   docstring conventions. Read the relevant files before editing.
5. **Respect the owner's standing rules** already encoded in `lessons.yaml` and the chat
   system prompt — reinforce them in code, don't contradict them.
6. **If nothing in the evidence clearly warrants a code change, change nothing.** Print a
   one-paragraph explanation of what you reviewed and why no change was justified. A no-op
   day is a correct outcome, not a failure.

## What good improvements look like

- A recurring action rejection → tighter validation, a clearer error message, or a prompt
  hint so the model gets it right the first time.
- An owner correction that keeps recurring → encode the corrected behavior in the relevant
  handler/store, with a test.
- A consistently slow or truncated call → a tighter prompt, a smaller context slice, or a
  raised token budget (whichever the evidence supports).
- A task that aborted on a fixable failure → make that failure recoverable.

## Deliverable

End your run with a short summary: the one or two changes you made and the exact evidence
line each addresses, or your no-change explanation. Keep the working tree with only your
intended edits (plus their tests).
