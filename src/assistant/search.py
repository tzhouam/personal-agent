"""Web search for the agent — no API key required.

Default backend is DuckDuckGo Lite (plain HTML, parsed with stdlib — verified
reachable from this container). If TAVILY_API_KEY is set, Tavily's LLM-oriented
API is used instead (better snippets, less rate-limit risk). ``fetch_page``
pulls a result page and strips it to readable text so an LLM can synthesize.

Consumers: the ``web_search`` chat action (search + LLM answer), the
``plan_task`` planner (real candidates instead of "search for X" steps).
"""

import html as _html
import logging
import re
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

from .config import Settings

log = logging.getLogger("assistant")

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) personal-agent/1.0"}
_TAG = re.compile(r"<[^>]+>")
_DDG_LINK = re.compile(
    r"<a rel=\"nofollow\" href=\"([^\"]*)\" class='result-link'>(.*?)</a>", re.DOTALL)
_DDG_SNIPPET = re.compile(r"<td class='result-snippet'>(.*?)</td>", re.DOTALL)


def _clean(text: str) -> str:
    return _html.unescape(" ".join(_TAG.sub(" ", text).split()))


def _real_url(ddg_href: str) -> str:
    """DDG lite links are //duckduckgo.com/l/?uddg=<urlencoded target>&rut=…"""
    if "uddg=" in ddg_href:
        target = parse_qs(urlparse(ddg_href).query).get("uddg", [""])[0]
        if target:
            return target
    return ddg_href


def _search_ddg(query: str, max_results: int) -> list[dict]:
    resp = httpx.get(f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}",
                     headers=_UA, timeout=20, follow_redirects=True)
    resp.raise_for_status()
    links = _DDG_LINK.findall(resp.text)
    snippets = [_clean(s) for s in _DDG_SNIPPET.findall(resp.text)]
    results = []
    for i, (href, title) in enumerate(links[:max_results]):
        results.append({"title": _clean(title), "url": _real_url(href),
                        "snippet": snippets[i] if i < len(snippets) else ""})
    return results


def _search_tavily(query: str, max_results: int, api_key: str) -> list[dict]:
    resp = httpx.post("https://api.tavily.com/search",
                      json={"api_key": api_key, "query": query,
                            "max_results": max_results},
                      timeout=25)
    resp.raise_for_status()
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": r.get("content", "")[:300]}
            for r in resp.json().get("results", [])]


def web_search(query: str, max_results: int = 8,
               settings: Settings | None = None) -> list[dict]:
    """[{title, url, snippet}] — empty list on any failure (callers degrade)."""
    query = str(query).strip()
    if not query:
        return []
    try:
        api_key = getattr(settings, "tavily_api_key", "") if settings else ""
        if api_key:
            return _search_tavily(query, max_results, api_key)
        return _search_ddg(query, max_results)
    except Exception as exc:
        log.warning("web search failed for %r: %s", query, exc)
        return []


def fetch_page(url: str, max_chars: int = 3000) -> str:
    """Readable text of a page (scripts/styles stripped), or "" on failure."""
    try:
        resp = httpx.get(url, headers=_UA, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        text = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>",
                      " ", resp.text)
        return _clean(text)[:max_chars]
    except Exception as exc:
        log.warning("page fetch failed for %s: %s", url, exc)
        return ""


def format_results(results: list[dict], limit: int = 6) -> str:
    return "\n".join(f"- {r['title']} — {r['snippet'][:200]} ({r['url']})"
                     for r in results[:limit]) or "(no results)"
