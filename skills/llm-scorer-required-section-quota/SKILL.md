---
name: llm-scorer-required-section-quota
description: LLM relevance thresholds silently starve product-required output sections (e.g. the 中文媒体 digest section was empty twice) — score per pool, annotate instead of filter, and give required sections a floor
trigger: a digest/report section that is a product requirement comes out empty even though its sources fetched items successfully
modules: [research, llm]
status: active
created_at: 2026-07-02
last_used_at: 2026-07-02
run_count: 0
---

## Diagnose
- Source health says items were fetched (e.g. "量子位: 10 items") but the
  section renders empty.
- The relevance scorer ranked those items below the global keep-threshold —
  likely because the owner profile skews the scorer toward other topics, or a
  single mixed-pool scoring call disadvantages one language/category.

## Fix
(`src/assistant/research/pipeline.py`)
1. Score each pool separately (papers / en feeds / zh feeds) so categories
   don't compete in one ranking.
2. Make the scorer **annotate** (`item["_score"]`) and sort — never filter
   inside the scorer. Selection policy lives in one place (`_select`).
3. Give required sections a floor: `_select(zh_pool, min_score=4, top=N,
   floor=min(3, len(pool)))` — the section shows its best 3 even if all scores
   are below threshold.
4. On scorer failure, default every item's score to the threshold (keep natural
   order, capped) — a broken scorer must not zero the digest.

## Verification
Run the digest with zh sources fetching: the 中文媒体 section contains ≥1 item
whenever any zh source returned items.

## Anti-patterns
- One global threshold across heterogeneous pools.
- Filtering inside the scoring function — selection rules get duplicated and
  drift.
- Treating "scored low" as "fetch failed" — check source health first; they
  need different fixes.
