"""arXiv Atom API client for the research digest: query, parse, and window
candidate papers. Exports `search`, `parse_feed`, and `fetch_recent`, and
enforces the API's 3-second request spacing so a run doesn't get rate-limited
to zero results."""

import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

API = "https://export.arxiv.org/api/query"
_NS = {"atom": "http://www.w3.org/2005/Atom"}
# arXiv API etiquette: one request every 3 seconds; it rate-limits whole runs
# with 429s otherwise (observed 2026-07-03: an entire run got 0 papers).
_QUERY_SPACING_SECONDS = 3.0


def _retryable(exc: BaseException) -> bool:
    """True for transient failures worth a retry — 429/5xx responses and
    transport errors; a 4xx other than 429 is a bad query, not worth retrying."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503)
    return isinstance(exc, httpx.TransportError)


@retry(
    retry=retry_if_exception(_retryable),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=5, max=60),
    reraise=True,
)
def search(query: str, max_results: int = 30, timeout: int = 30) -> list[dict]:
    """One arXiv API query for `query`, newest submissions first, parsed into
    paper dicts. Retries transient failures (see `_retryable`) with exponential
    backoff before giving up."""
    resp = httpx.get(
        API,
        params={
            "search_query": query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": max_results,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return parse_feed(resp.text)


def parse_feed(xml_text: str) -> list[dict]:
    """Parse an arXiv Atom response into a list of paper dicts (id, title,
    abstract, published, up to 8 authors, categories, abstract url). Whitespace
    is collapsed in title/abstract; the version suffix is stripped from the url."""
    papers = []
    for entry in ET.fromstring(xml_text).findall("atom:entry", _NS):
        arxiv_id = (entry.findtext("atom:id", "", _NS)).rsplit("/", 1)[-1]
        papers.append(
            {
                "id": arxiv_id,
                "title": " ".join((entry.findtext("atom:title", "", _NS)).split()),
                "abstract": " ".join((entry.findtext("atom:summary", "", _NS)).split()),
                "published": entry.findtext("atom:published", "", _NS),
                "authors": [
                    a.findtext("atom:name", "", _NS)
                    for a in entry.findall("atom:author", _NS)
                ][:8],
                "categories": [
                    c.get("term", "") for c in entry.findall("atom:category", _NS)
                ],
                "url": f"https://arxiv.org/abs/{arxiv_id.split('v')[0]}",
            }
        )
    return papers


def fetch_recent(queries: list[str], lookback_days: int, max_per_query: int) -> list[dict]:
    """Run all queries, dedupe by id, keep only papers submitted in the window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    by_id: dict[str, dict] = {}
    for i, query in enumerate(queries):
        if i:
            time.sleep(_QUERY_SPACING_SECONDS)
        # AND of words, not exact phrase — phrase queries return almost nothing
        # in a one-week window; the relevance scorer downstream does the precision
        terms = " AND ".join(f"all:{w}" for w in query.split())
        try:
            for paper in search(terms, max_results=max_per_query):
                published = paper["published"]
                if not published:
                    continue
                if datetime.fromisoformat(published.replace("Z", "+00:00")) < cutoff:
                    continue
                by_id.setdefault(paper["id"].split("v")[0], paper)
        except (httpx.HTTPError, ET.ParseError):
            continue  # one bad query must not kill the sweep
    return list(by_id.values())
