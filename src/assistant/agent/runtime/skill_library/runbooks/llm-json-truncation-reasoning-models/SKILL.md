---
name: llm-json-truncation-reasoning-models
description: JSON-mode calls to reasoning models (DeepSeek via Anthropic-compatible API) fail with "no JSON found" because the response truncates at max_tokens before the JSON is emitted
trigger: complete_json / structured-output call raises "no JSON object or array found in response", especially with deepseek-* or other reasoning models behind an Anthropic-compatible endpoint
modules: [llm]
status: active
created_at: 2026-07-02
last_used_at: 2026-08-30
run_count: 2
---

## Diagnose
- The parse error appears even after a "respond with ONLY JSON" retry.
- Check `resp.stop_reason` — it is `max_tokens`. Reasoning models spend a large,
  invisible token budget thinking before emitting the answer; a small
  `max_tokens` (e.g. 500–3000) is consumed entirely by reasoning, so the text
  contains no JSON at all.
- Seen again in August production traces: 55/1,064 daily LLM calls truncated
  (53 on MiMo). A 60-notification digest exhausted 6,000 tokens twice and then
  silently rendered a full deterministic fallback.

## Fix
1. Preserve `stop_reason` on the string-compatible completion. A normal
   `end_turn` parse failure gets exactly one same-budget repair with bounded
   parse feedback; `max_tokens` gets exactly one retry at 2× the budget, capped
   at 16,000. A truncation already at the cap fails explicitly.
2. Bound the requested output before raising its budget: GitHub triage uses
   15-item/8k batches (60-item cap); research scoring uses 20-item/8k batches
   with at most two calls in flight. Merge by stable IDs/indices and apply
   deterministic fallback only to missing/failed batches.
3. Keep tiny structured calls single-model: arXiv query generation is 4k with
   `mixture=False`. MoA adds cost but no value when the required JSON is small.
4. Record requested/completed/fallback/degraded counters in phase artifacts so
   a parse failure cannot masquerade as a successful empty result.
5. Prompt with "Respond immediately with ONLY this JSON" to shorten preamble.

## Verification
- `pytest -q test/test_llm_reliability.py test/test_tasks.py test/test_research.py`
  covers stop-aware retry, the 16k ceiling, stable batching, partial fallback,
  and no-MoA calls.
- Inspect the next run artifacts: digest/research coverage should be ≥95%, with
  no identical-budget truncation retry in `trace.jsonl`.

## Anti-patterns
- Retrying the identical call at the same `max_tokens` — truncation is
  deterministic, so this only doubles cost and latency.
- "Fixing" the parser to accept partial JSON — you'll act on a half-emitted op
  list.
- Assuming the provider is broken: the request succeeded (HTTP 200); the budget
  was simply spent on reasoning.
- Raising one monolithic call to an unbounded budget when stable ID-keyed
  batches can isolate one bad response.
