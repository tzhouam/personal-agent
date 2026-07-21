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

from assistant.platform.config import Settings

log = logging.getLogger("assistant")

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) personal-agent/1.0"}
_TAG = re.compile(r"<[^>]+>")
_DDG_LINK = re.compile(
    r"<a rel=\"nofollow\" href=\"([^\"]*)\" class='result-link'>(.*?)</a>", re.DOTALL)
_DDG_SNIPPET = re.compile(r"<td class='result-snippet'>(.*?)</td>", re.DOTALL)


def _clean(text: str) -> str:
    """Strip HTML tags, collapse whitespace, and unescape entities to plain text."""
    return _html.unescape(" ".join(_TAG.sub(" ", text).split()))


def _real_url(ddg_href: str) -> str:
    """DDG lite links are //duckduckgo.com/l/?uddg=<urlencoded target>&rut=…"""
    if "uddg=" in ddg_href:
        target = parse_qs(urlparse(ddg_href).query).get("uddg", [""])[0]
        if target:
            return target
    return ddg_href


def _search_ddg(query: str, max_results: int) -> list[dict]:
    """The keyless default backend: scrape DuckDuckGo Lite's HTML into up to
    ``max_results`` ``{title, url, snippet}`` dicts, resolving DDG redirect
    links to their real targets."""
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


def _search_gemini(query: str, api_key: str, model: str) -> dict:
    """Gemini API 'grounding with Google Search' — Google's post-CSE way to
    search the whole web with one AI Studio key. Returns the grounded ANSWER
    plus its cited sources in a single call (free tier: ~1500/day on
    2.5-class models)."""
    from assistant.platform.timeutil import temporal_anchor

    # anchored so date-relative queries ("today's news", "本周") ground on the
    # right day — this call bypasses LLM._call, which anchors everything else
    resp = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": api_key},
        json={"contents": [{"parts": [{"text": query + "\n\n" + temporal_anchor()}]}],
              "tools": [{"google_search": {}}]},
        timeout=45)
    resp.raise_for_status()
    candidate = (resp.json().get("candidates") or [{}])[0]
    answer = " ".join(p.get("text", "")
                      for p in candidate.get("content", {}).get("parts", [])).strip()
    results = []
    for chunk in candidate.get("groundingMetadata", {}).get("groundingChunks", []):
        web = chunk.get("web") or {}
        if web.get("uri"):
            results.append({"title": web.get("title", ""), "url": web["uri"],
                            "snippet": ""})
    return {"answer": answer, "results": results}


def _search_google(query: str, max_results: int, api_key: str, cse_id: str) -> list[dict]:
    """Google Programmable Search (Custom Search JSON API) — free tier is
    100 queries/day; needs an API key AND a search-engine id (cx)."""
    resp = httpx.get("https://www.googleapis.com/customsearch/v1",
                     params={"key": api_key, "cx": cse_id, "q": query,
                             "num": min(max_results, 10)},
                     timeout=20)
    resp.raise_for_status()
    return [{"title": item.get("title", ""), "url": item.get("link", ""),
             "snippet": item.get("snippet", "")}
            for item in resp.json().get("items", [])]


def _search_brave(query: str, max_results: int, api_key: str) -> list[dict]:
    """Brave Search API — independent index, ~1-2k free queries/month, no card."""
    resp = httpx.get("https://api.search.brave.com/res/v1/web/search",
                     params={"q": query, "count": min(max_results, 20)},
                     headers={"X-Subscription-Token": api_key,
                              "Accept": "application/json"},
                     timeout=20)
    resp.raise_for_status()
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": _clean(r.get("description", ""))[:300]}
            for r in resp.json().get("web", {}).get("results", [])]


def _search_tavily(query: str, max_results: int, api_key: str) -> list[dict]:
    """Tavily's LLM-oriented search API into ``{title, url, snippet}`` dicts
    (snippet is the returned content, capped at 300 chars)."""
    resp = httpx.post("https://api.tavily.com/search",
                      json={"api_key": api_key, "query": query,
                            "max_results": max_results},
                      timeout=25)
    resp.raise_for_status()
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": r.get("content", "")[:300]}
            for r in resp.json().get("results", [])]


def web_search_answer(query: str, max_results: int = 8,
                      settings: Settings | None = None) -> dict:
    """{"answer": str, "results": [{title, url, snippet}]} — answer is
    non-empty only when a grounded backend (Gemini) produced one; plain
    backends return results for the caller to synthesize. Both empty on
    total failure (callers degrade)."""
    query = str(query).strip()
    if not query:
        return {"answer": "", "results": []}
    get = (lambda name: getattr(settings, name, "") if settings else "")
    backends = []
    if get("gemini_api_key"):
        backends.append(("gemini", lambda: _search_gemini(
            query, get("gemini_api_key"),
            get("gemini_search_model") or "gemini-2.5-flash")))
    if get("google_api_key") and get("google_cse_id"):
        backends.append(("google", lambda: {"answer": "", "results": _search_google(
            query, max_results, get("google_api_key"), get("google_cse_id"))}))
    if get("tavily_api_key"):
        backends.append(("tavily", lambda: {"answer": "", "results": _search_tavily(
            query, max_results, get("tavily_api_key"))}))
    if get("brave_api_key"):
        backends.append(("brave", lambda: {"answer": "", "results": _search_brave(
            query, max_results, get("brave_api_key"))}))
    backends.append(("ddg", lambda: {"answer": "", "results": _search_ddg(query, max_results)}))
    for name, backend in backends:  # keyed backends first, DDG as the safety net
        try:
            out = backend()
            if out.get("answer") or out.get("results"):
                return out
        except Exception as exc:
            log.warning("web search via %s failed for %r: %s", name, query, exc)
    return {"answer": "", "results": []}


def web_search(query: str, max_results: int = 8,
               settings: Settings | None = None) -> list[dict]:
    """[{title, url, snippet}] — empty list on any failure (callers degrade)."""
    return web_search_answer(query, max_results, settings)["results"]


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
    """Render the first ``limit`` results as a bulleted, LLM-readable list, or
    "(no results)" when empty."""
    return "\n".join(f"- {r['title']} — {r['snippet'][:200]} ({r['url']})"
                     for r in results[:limit]) or "(no results)"
