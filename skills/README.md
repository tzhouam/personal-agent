# Skills — distilled lessons from building this agent

Runbooks in the same `SKILL.md` format as `vllm-omni-rebase-agent/agent/skills/`
(frontmatter + Diagnose / Fix / Verification / Anti-patterns), distilled from
real failures hit while building the personal-agent (2026-07-02). Read the one
matching your symptom before re-deriving the fix.

| Skill | One-line lesson |
|---|---|
| [llm-json-truncation-reasoning-models](llm-json-truncation-reasoning-models/SKILL.md) | "no JSON found" from reasoning models = max_tokens spent on thinking; raise the budget, log stop_reason |
| [arxiv-query-recall](arxiv-query-recall/SKILL.md) | exact-phrase arXiv queries ≈ empty; use word-AND + seen-store dedup instead of narrow windows |
| [chinese-ai-media-rss](chinese-ai-media-rss/SKILL.md) | WeChat 公众号 outlets have no stable RSS; per-source health tracking, self-hosted RSSHub, never fail silently |
| [gmail-imap-app-password](gmail-imap-app-password/SKILL.md) | SMTP app password also does IMAP readonly — skip OAuth; SINCE is date-granular; headers only |
| [chrome-history-sqlite](chrome-history-sqlite/SKILL.md) | copy the locked History db; 1601-epoch µs timestamps; privacy tiers at read time |
| [git-token-push-and-env-hygiene](git-token-push-and-env-hygiene/SKILL.md) | per-command extraheader auth; `grep -x '.env'` (substring grep false-alarms on .env.template); verify from the remote |
| [llm-scorer-required-section-quota](llm-scorer-required-section-quota/SKILL.md) | relevance thresholds silently empty required sections; per-pool scoring + floors |
| [notification-todo-lifecycle](notification-todo-lifecycle/SKILL.md) | dedup todos by URL not notification id; auto-close via API (merged/closed/reviewed); payload.size for push counts |
| [publishing-agent-output-safely](publishing-agent-output-safely/SKILL.md) | public pages = deterministic render; LLM prose = approval gate; compile checks; pull-rebase never force; Overleaf has no API |
| [headless-container-scheduling](headless-container-scheduling/SKILL.md) | PID1=tini → no systemd/cron; dual scheduling paths; don't fight permission classifiers, hand the owner the one-liner |
