---
name: llm-json-truncation-reasoning-models
description: JSON-mode calls to reasoning models (DeepSeek via Anthropic-compatible API) fail with "no JSON found" because the response truncates at max_tokens before the JSON is emitted
trigger: complete_json / structured-output call raises "no JSON object or array found in response", especially with deepseek-* or other reasoning models behind an Anthropic-compatible endpoint
modules: [llm]
status: active
created_at: 2026-07-02
last_used_at: 2026-07-09
run_count: 1
---

## Diagnose
- The parse error appears even after a "respond with ONLY JSON" retry.
- Check `resp.stop_reason` — it is `max_tokens`. Reasoning models spend a large,
  invisible token budget thinking before emitting the answer; a small
  `max_tokens` (e.g. 500–3000) is consumed entirely by reasoning, so the text
  contains no JSON at all.
- Seen twice in this repo: arXiv query generation (max_tokens=500) and the
  profile updater (max_tokens=3000, large prompt).

## Fix
1. Give JSON calls a generous budget: 1500+ for tiny outputs, 8000 for anything
   substantial, 16000 for op emission over large prompts
   (`src/assistant/llm.py`, `tasks/profile_update.py`; 2026-07-09: enrich
   backfill batches truncated at 8000 — the first batch silently yielded 0 ops).
2. Log truncation explicitly so the failure is diagnosable, not silent:
   `if resp.stop_reason == "max_tokens": log.warning(...)` (`llm.py:complete`).
3. Keep the one-shot JSON retry with the parse error fed back — it fixes format
   drift, but it can NOT fix truncation; only a bigger budget does.
4. Prompt with "Respond immediately with ONLY this JSON" to shorten preamble.

## Verification
Re-run the failing call; it returns parseable JSON and no truncation warning:
`assistant enrich-profile` completes with "N ops applied" per batch.

## Anti-patterns
- Retrying the identical call harder (same max_tokens) — truncation is
  deterministic, the retry just doubles cost.
- "Fixing" the parser to accept partial JSON — you'll act on a half-emitted op
  list.
- Assuming the provider is broken: the request succeeded (HTTP 200); the budget
  was simply spent on reasoning.
