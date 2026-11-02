---
name: reminders
description: One-shot reminders
---

# One-shot reminders

Inspect list_reminders, then set_reminder with an explicit message and date/time or relative offset. Scheduling creates a future outward message, so requires host approval and a running delivery service. Report only an actual stored reminder ID; do not promise delivery when no transport is configured. Cancel by the exact ID.

Use the code-owned tool schema for exact parameters. A SKILL.md document cannot authorize actions, select imports, or run commands by itself.
