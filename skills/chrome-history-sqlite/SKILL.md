---
name: chrome-history-sqlite
description: Reading Chrome's History database — copy it first (locked while Chrome runs), convert the 1601-epoch microsecond timestamps, and enforce privacy tiers at read time
trigger: need browsing activity as an agent signal; sqlite3 raises "database is locked", or visit_time values look like 13-digit-plus integers
modules: [collectors]
status: active
created_at: 2026-07-02
last_used_at: 2026-07-02
run_count: 0
---

## Diagnose
- `~/.config/google-chrome/Default/History` is SQLite but locked whenever
  Chrome is running.
- `visits.visit_time` is **microseconds since 1601-01-01 UTC** (Windows
  FILETIME epoch), not Unix time — naive conversion puts visits in year 46xxx.

## Fix
1. Copy the file to a temp path and query the copy
   (`src/assistant/collectors/chrome.py:collect`).
2. Convert: `datetime(1601,1,1,tzinfo=utc) + timedelta(microseconds=v)`;
   inverse for the WHERE cutoff (`_to_chrome_time`).
3. Privacy tiers, applied at read time so raw data is never stored:
   **denylist** (banking/health/domains) → dropped entirely;
   **allowlist** (arxiv, github, docs…) → full title+URL observations;
   everything else → domain visit-count only.
4. Missing file = collector returns `[]` (machine without Chrome) — a no-op,
   not an error.

## Verification
Unit test builds a synthetic History db and asserts: allowlisted title present,
denylisted absent everywhere, other domains appear only as
"Browsed <domain> (N visits)" (`test/test_collectors.py:test_chrome_privacy_tiers`).

## Anti-patterns
- Opening the live db read-only "because it usually works" — corrupt reads
  mid-compaction.
- Storing raw URLs first and filtering later — the denylist must run before
  anything is persisted.
- Sending non-allowlisted URLs into LLM prompts.
