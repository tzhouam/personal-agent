---
name: arxiv-query-recall
description: arXiv API exact-phrase queries return almost nothing in a short date window; use word-AND queries plus a seen-store instead of a narrow window
trigger: research/arxiv pipeline yields 0-2 candidate papers from several queries while the same topics clearly have recent papers
modules: [research]
status: active
created_at: 2026-07-02
last_used_at: 2026-07-02
run_count: 0
---

## Diagnose
- `fetch_recent` health note says e.g. "1 candidates from 6 queries".
- Test one query without the date filter: `search('all:"LLM inference serving"')`
  returns results, but the newest is weeks old — exact phrases (`all:"..."`)
  match so few papers that a 3-day window is usually empty.

## Fix
1. Build queries as an AND of words, not a quoted phrase:
   `" AND ".join(f"all:{w}" for w in query.split())`
   (`src/assistant/research/arxiv.py:fetch_recent`). Recall goes up ~20×; the
   downstream LLM relevance scorer supplies the precision.
2. Widen the window (7 days) and rely on the **seen-store** for freshness: a
   paper is surfaced only the first day it appears, so a wide window never
   causes repeats (`events_store.filter_unseen`, config comment on
   `arxiv_lookback_days`).
3. Keep a deterministic fallback for query generation (profile interest topics)
   so an LLM failure doesn't zero the section.

## Verification
`arxiv.fetch_recent(['LLM inference serving', ...], 7, 30)` returns 15+
candidates; daily digest shows ~10 papers with relevance rationales.

## Anti-patterns
- Narrowing the date window to control volume — control it with scoring + seen-
  store dedup instead; a narrow window starves sparse topics.
- Sorting by relevance on the arXiv side (`sortBy=relevance`) — you lose
  recency, which is the whole point of a daily digest.
