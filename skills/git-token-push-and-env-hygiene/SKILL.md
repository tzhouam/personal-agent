---
name: git-token-push-and-env-hygiene
description: Push to GitHub with a PAT without persisting it (per-command extraheader), and verify .env never leaks — including the grep pitfall where ".env" matches ".env.template"
trigger: agent needs to push to GitHub non-interactively; or verifying a repo contains no secrets before/after push
modules: [publishing]
status: active
created_at: 2026-07-02
last_used_at: 2026-07-02
run_count: 0
---

## Diagnose
Embedding the token in the remote URL (`https://user:token@github.com/...`)
persists it in `.git/config`; credential helpers persist it on disk. Both leak
if the workdir is ever copied or committed.

## Fix
1. Pass auth per command, never stored:
   `git -c http.https://github.com/.extraheader="Authorization: Basic $(printf 'user:%s' "$TOKEN" | base64 -w0)" push ...`
   (see `src/assistant/agent/website/:_auth_flag` for the programmatic version;
   works for clone/fetch/push, private repos included).
2. Create repos via API (`POST /user/repos {"private": true}`) and confirm
   `"private": true` in the response.
3. `.gitignore` must contain `.env`; verify with **exact-match** tooling:
   `git diff --cached --name-only | grep -x '.env'`
   — a plain `grep '\.env'` matches `.env.template` and produces a false
   "leak" alarm (this happened here).
4. After the first push, verify from the remote's perspective:
   list `GET /repos/<r>/contents/` and assert `.env` is absent.

## Verification
`git ls-files --error-unmatch .env` fails ("did you forget to add") and the
remote contents listing shows only `.env.template`.

## Anti-patterns
- Token in the remote URL or in a committed script.
- Substring-grepping staged files for `.env` — exact-match (`grep -x`) or
  `git check-ignore .env`.
- Assuming `.gitignore` is enough — a file force-added earlier stays tracked;
  check `git ls-files`.
