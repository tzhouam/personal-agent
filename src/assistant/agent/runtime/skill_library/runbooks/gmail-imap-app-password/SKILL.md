---
name: gmail-imap-app-password
description: Read Gmail headlessly without an OAuth dance — a Gmail app password already used for SMTP also works for IMAP readonly; enforce real time cutoffs and headers-only privacy
trigger: need to collect email on a headless box and Gmail API OAuth (browser consent, client secrets) is unavailable or overkill
modules: [collectors]
status: active
created_at: 2026-07-02
last_used_at: 2026-07-02
run_count: 0
---

## Diagnose
Gmail API requires an OAuth client + interactive browser consent — impossible in
a headless container. But if delivery already works via SMTP with an app
password, that same credential authenticates `imaplib.IMAP4_SSL` too.

## Fix
1. `IMAP4_SSL("imap.gmail.com", 993)` + `login(smtp_user, app_password)` +
   `select("INBOX", readonly=True)` (`src/assistant/agent/collectors/gmail.py`).
2. IMAP `SINCE` is **date-granular** — it over-returns; re-filter by parsing the
   Date header against the real cutoff (`_headers_to_observation`).
3. Privacy: fetch `BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE LIST-UNSUBSCRIBE)]`
   only — bodies never enter the pipeline; classify newsletter vs personal from
   List-Unsubscribe; skip `notifications@github.com` (covered natively by the
   GitHub collector — otherwise every notification appears twice).
4. Decode headers with `email.header.decode_header`/`make_header` (RFC 2047
   encoded-words are common in Chinese subjects).

## Verification
`assistant run --dry-run` logs `collector gmail: N observations` with sensible
titles; no message bodies appear in `runs/<id>/observations.json`.

## Anti-patterns
- Building the full OAuth flow first — start with IMAP; add OAuth only when you
  need labels/threads or send-as scopes.
- Fetching full RFC822 bodies "for context" — privacy leak into LLM prompts and
  the events store.
- Trusting `SINCE` for a 26-hour window — you'll double-ingest yesterday.
