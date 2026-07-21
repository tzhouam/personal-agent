"""Chrome collector — turns local browsing history into visit Observations.

Registers as `@register("chrome")`. Reads the on-disk Chrome History SQLite and
applies privacy tiers so untrusted domains only ever leave as aggregate counts,
never full URLs.
"""

import shutil
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from assistant.platform.config import Settings
from assistant.agent.collectors import register

# Chrome stores visit_time as microseconds since 1601-01-01 UTC.
_CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _to_chrome_time(dt: datetime) -> int:
    """Convert a datetime to Chrome's visit_time (microseconds since 1601-01-01 UTC)."""
    return int((dt - _CHROME_EPOCH).total_seconds() * 1_000_000)


def _from_chrome_time(value: int) -> datetime:
    """Convert a Chrome visit_time (microseconds since 1601-01-01 UTC) to a datetime."""
    return _CHROME_EPOCH + timedelta(microseconds=value)


@register("chrome")
class ChromeCollector:
    """Reads the local Chrome History SQLite.

    Privacy tiers (denylist > allowlist > domain-count-only): denylisted visits
    are dropped before anything is stored; allowlisted domains contribute full
    title+URL observations; everything else is aggregated to domain visit counts.
    """

    name = "chrome"

    def __init__(self, settings: Settings):
        """Cache the History DB path and the allow/deny domain lists that drive
        the privacy tiers."""
        self.history_path = settings.chrome_history_path
        self.allowlist = settings.chrome_allowlist
        self.denylist = settings.chrome_denylist

    def collect(self, since: datetime) -> list[dict]:
        """Return visit Observations from history since `since`, privacy-tiered.

        Copies the History DB to a temp file first because Chrome holds a write
        lock on the live file, then reads the most recent 2000 visits. Denylisted
        visits are dropped; allowlisted domains yield full `visit` observations
        (title + URL, deduped by URL); every other domain is collapsed into one
        `domain_visits` count observation (top 25). A missing History file makes
        the collector a no-op — it degrades, never crashes.
        """
        if not self.history_path.exists():
            return []  # no Chrome on this machine — collector is a no-op

        # The DB is locked while Chrome runs — always query a copy.
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
            shutil.copyfile(self.history_path, tmp.name)
            conn = sqlite3.connect(tmp.name)
            try:
                rows = conn.execute(
                    "SELECT u.url, u.title, v.visit_time FROM visits v"
                    " JOIN urls u ON u.id = v.url WHERE v.visit_time > ?"
                    " ORDER BY v.visit_time DESC LIMIT 2000",
                    (_to_chrome_time(since),),
                ).fetchall()
            finally:
                conn.close()

        observations: list[dict] = []
        domain_counts: Counter[str] = Counter()
        seen_urls: set[str] = set()

        for url, title, visit_time in rows:
            domain = (urlparse(url).hostname or "").removeprefix("www.")
            if not domain or self._denied(domain, url):
                continue
            if self._allowed(domain):
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                observations.append(
                    {
                        "source": "chrome",
                        "ts": _from_chrome_time(visit_time).isoformat(),
                        "kind": "visit",
                        "title": f"Visited: {title or url}",
                        "url": url,
                        "entities": [domain],
                        "raw": {},
                    }
                )
            else:
                domain_counts[domain] += 1

        now = datetime.now(timezone.utc).isoformat()
        for domain, count in domain_counts.most_common(25):
            observations.append(
                {
                    "source": "chrome",
                    "ts": now,
                    "kind": "domain_visits",
                    "title": f"Browsed {domain} ({count} visits)",
                    "url": None,
                    "entities": [domain],
                    "raw": {},
                }
            )
        return observations

    def _denied(self, domain: str, url: str) -> bool:
        """True if any denylist term is a substring of the domain or full URL —
        a broad match so a single term can suppress a whole site or path."""
        return any(term in domain or term in url for term in self.denylist)

    def _allowed(self, domain: str) -> bool:
        """True if `domain` equals or is a subdomain of an allowlist entry —
        stricter than the denylist so allowlisting can't leak sibling domains."""
        return any(domain == a or domain.endswith("." + a) for a in self.allowlist)
