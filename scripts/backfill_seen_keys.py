#!/usr/bin/env python
"""One-shot: seed the new `gh-notif-<id>-<reason>` seen keys from past digests.

The digest seen key used to embed `updated_at`, so every comment on a thread
minted a new key and nothing was ever suppressed (2026-07-20→27: 4 items
suppressed in a week). `orchestrator.seen_key` now keys on id + reason — but the
old rows can never match it, so the first run after the change would treat every
open notification as brand new and surface ~40 items at once.

This reads the `sections` of recent `runs/*/digest.json` artifacts — they carry
both `id` and `reason` for every item already shown — and inserts the
new-format keys, so the catch-up digest only contains genuinely new threads.

    python scripts/backfill_seen_keys.py <data-dir> [--days 7] [--dry-run]

e.g. `python scripts/backfill_seen_keys.py ~/.personal-agent/users/tzhouam`.
Idempotent: `mark_seen` upserts, so re-running only advances `last_seen`.
Back up events.db first — this writes to it.
"""

import argparse
import json
import pathlib
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from assistant.agent.events_store import EventsStore          # noqa: E402
from assistant.agent.orchestrator import seen_key             # noqa: E402


def digest_items(data_dir: pathlib.Path, days: int):
    """Every triaged item from digests written in the last `days` days, newest
    run last. Unreadable or half-written artifacts are skipped, not fatal."""
    cutoff = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    for run in sorted((data_dir / "runs").glob("run-*")):
        parts = run.name.split("-")
        if len(parts) < 2 or parts[1] < cutoff:
            continue
        artifact = run / "digest.json"
        if not artifact.exists():
            continue
        try:
            digest = json.loads(artifact.read_text())
        except (OSError, json.JSONDecodeError):
            print(f"  skipped unreadable {artifact}")
            continue
        for section in digest.get("sections", {}).values():
            for item in section:
                if item.get("id"):
                    yield run.name, item


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("data_dir", type=pathlib.Path,
                    help="a user's data dir (holding runs/ and events.db)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    keys, runs = {}, set()
    for run_name, item in digest_items(args.data_dir, args.days):
        keys[seen_key(item)] = item
        runs.add(run_name)
    print(f"{len(keys)} distinct keys from {len(runs)} runs in the last {args.days}d")

    events = EventsStore(args.data_dir / "events.db")
    try:
        fresh = events.filter_unseen(list(keys))
        print(f"{len(fresh)} not yet in the seen-store")
        for key in fresh[:10]:
            print(f"  + {key}  {keys[key].get('title', '')[:60]}")
        if args.dry_run:
            print("dry run — nothing written")
            return 0
        if fresh:
            events.mark_seen(fresh, context=f"backfill {datetime.now():%Y-%m-%d}")
        print(f"marked {len(fresh)} seen")
    finally:
        events.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
