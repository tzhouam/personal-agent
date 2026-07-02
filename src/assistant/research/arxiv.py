import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

API = "https://export.arxiv.org/api/query"
_NS = {"atom": "http://www.w3.org/2005/Atom"}


def search(query: str, max_results: int = 30, timeout: int = 30) -> list[dict]:
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
    for query in queries:
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
