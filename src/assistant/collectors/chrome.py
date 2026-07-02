import shutil
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from ..config import Settings
from . import register

# Chrome stores visit_time as microseconds since 1601-01-01 UTC.
_CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _to_chrome_time(dt: datetime) -> int:
    return int((dt - _CHROME_EPOCH).total_seconds() * 1_000_000)


def _from_chrome_time(value: int) -> datetime:
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
        self.history_path = settings.chrome_history_path
        self.allowlist = settings.chrome_allowlist
        self.denylist = settings.chrome_denylist

    def collect(self, since: datetime) -> list[dict]:
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
        return any(term in domain or term in url for term in self.denylist)

    def _allowed(self, domain: str) -> bool:
        return any(domain == a or domain.endswith("." + a) for a in self.allowlist)
