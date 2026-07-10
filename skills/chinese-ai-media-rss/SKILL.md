---
name: chinese-ai-media-rss
description: 新智元/机器之心 publish on WeChat 公众号 with no stable first-party RSS — anyfeeder's public WeChat mirror works (verified 2026-07-10); RSSHub is a dead end; keep per-source health tracking, not one-off scrapers
trigger: feed fetch for a Chinese AI media source fails with ParseError (HTML instead of XML) or HTTP 403, or the 中文 digest section is silently empty
modules: [research, sources]
status: active
created_at: 2026-07-02
last_used_at: 2026-07-10
run_count: 1
---

## Diagnose
- `https://www.jiqizhixin.com/rss` → 302 → `/data-service` returning
  `<!DOCTYPE html>` (SPA), so the XML parser raises ParseError.
- `https://rsshub.app/...` → 403 "Just a moment..." (Cloudflare) — the public
  RSSHub instance is not usable from datacenter IPs.
- WeChat-only outlets (新智元) have no first-party web feed at all.

## Fix
1. Working direct feeds: 量子位 `https://www.qbitai.com/feed` (WordPress).
2. For WeChat-only outlets, **anyfeeder's public mirror** works (verified
   2026-07-10, fresh same-day items): 新智元
   `https://plink.anyfeeder.com/weixin/AI_era`, 机器之心
   `https://plink.anyfeeder.com/weixin/almosthuman2014` (the 公众号 id, not
   the site name). Fallbacks if it dies: feedx.net (404 for these two as of
   2026-07-10), wechat2rss (index now JS-rendered/partly paid).
3. **RSSHub is a dead end here** (2026-07-10): rsshub.app is
   Cloudflare-blocked from datacenter IPs, and the npm `rsshub` library build
   (1.0.0-master) ships NO jiqizhixin route at all — don't reinstall it
   hoping otherwise. jiqizhixin.com's /api/* endpoints all return the SPA
   shell too.
4. Make failures visible, never silent: record per-source health
   (`"FAILED: ParseError"`) and render missing sources in the digest footer
   (`research/pipeline.py:_gather_feed_items`, email footer).
5. Guarantee the product requirement independently of flaky sources: the 中文
   section has a score floor/quota so whatever zh sources DO work still surface
   (`pipeline.py:_select(floor=...)`).

## Verification
Daily digest footer lists any failed sources by name; 中文媒体 section is
non-empty whenever at least one zh source fetched.

## Anti-patterns
- Silently dropping a broken source — the reader assumes coverage that no
  longer exists.
- Scraping the SPA HTML with regex — breaks on the next frontend deploy.
- Relying on rsshub.app in production.
