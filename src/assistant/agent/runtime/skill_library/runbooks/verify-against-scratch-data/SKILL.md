---
name: verify-against-scratch-data
description: Never verify features against the live DATA_DIR — fabricated test inputs pollute the owner's real ledgers
trigger: about to live-test chat actions, stores, or the daemon with made-up data
modules: [finance_store, health_store, lessons_store, todo_store, serve]
status: active
created_at: 2026-07-12
last_used_at: 2026-07-17
run_count: 1
---

# Verify against scratch data, never the live stores

## Diagnose
You are about to prove a feature works by sending fabricated inputs ("记账：
发了季度奖金8000", fake meals, fake feedback rules) through the live daemon
(`127.0.0.1:8377`) or `assistant ask/task`. Those paths write to the OWNER'S
real `DATA_DIR` (`~/.personal-agent`): the finance ledger, health log,
lessons, todos. Voiding afterwards still leaves noise the owner will see —
this happened on 2026-07-12 (f50-f53, h1-h3, an invented body profile, a
test-created lesson) and the owner had to ask what a "季度奖金" was.

## Fix
Point everything at a scratch data dir; every store and the whole daemon
honor `DATA_DIR`:

    export DATA_DIR=$(mktemp -d)          # or Settings(data_dir=...)
    SERVE_PORT=8399 assistant serve &     # scratch daemon, separate port
    # …or skip HTTP: run_action(name, params, Settings(data_dir=scratch))

For end-to-end LLM verification, `handle_message(text, Settings(data_dir=
scratch), image_paths=…)` exercises everything except the loopback HTTP
layer. Only read-only calls (`/healthz`, list/summary actions, analyses)
may target the live daemon.

## Verification
After the test run: `git -C ~/.personal-agent/profile status --short` and
`git log --oneline -3` show NO commits from your test window; the scratch
dir holds all test writes.

## Anti-patterns
- "I'll void the records afterwards" — voided rows still show in the file.
- Testing dedup/learning "for real" on the live daemon: dedup and lessons
  behave identically under a scratch `DATA_DIR`.
- Deleting owner rows to clean up your mess — only ever remove rows you
  yourself fabricated, with content verification, and say so.
- **pytest helpers that build `Settings(...)` by hand without pinning
  `data_dir`** — the default is the LIVE `~/.personal-agent`, and any code
  path that writes through settings (e.g. the MoA `moa` metrics sink in
  `llm.py`, 2026-07-17: 138 stray rows in the live root events.db from
  `test_llm_roles._settings`) lands there. Every such helper needs a
  `data_dir=tmp_path/...` kwarg or an autouse `monkeypatch.setenv("DATA_DIR",
  str(tmp_path/"data"))` fixture; after adding a new settings-driven write
  path, rerun the suite and check the live dir gained nothing.
- **`Settings.for_user(uid)` re-reads the repo `.env`** — it does `cls()`
  internally, so its `data_dir` resolves to the REAL deployment root, NOT any
  `base_settings.data_dir` you pass around. Code that provisions/writes for a
  user must derive paths from the passed `base_settings` (e.g. `base.data_dir /
  "users" / uid / "profile"`), never from `Settings.for_user(uid).profile_dir`,
  or a scratch test writes into the live `~/.personal-agent/users/`
  (2026-07-18: `onboarding.provision_user` seeded a stray `users/<hex>/` into
  the live dir this way). Verify: after any user-provisioning test, `ls
  ~/.personal-agent/users/` shows only the real uids.
