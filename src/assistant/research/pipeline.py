import hashlib
import json
import logging

from ..config import Settings
from ..events_store import EventsStore
from ..llm import LLM
from ..profile_store import render_summary
from . import arxiv, feeds

log = logging.getLogger("assistant")

_QUERY_SYSTEM = """Given a profile of your owner, produce arXiv search phrases capturing what they
would want in a daily research digest. Respond immediately with ONLY this JSON, no other text:
{"queries": ["<3-6 short search phrases, each 2-4 words, e.g. 'LLM inference serving'>"]}"""

_SCORE_SYSTEM = """Score each item's relevance to the owner profile from 0 (irrelevant) to 10
(must-read). Judge by topical overlap with their skills, interests, and active projects.
Respond with ONLY a JSON array: [{"idx": <int>, "score": <int>}] covering every idx given."""

_SUMMARY_SYSTEM = """Write the research section of the owner's daily digest.

For each paper: a 2-3 sentence summary plus one "why" sentence explicitly tied to the owner's
profile (their projects/interests). For each feed item: a one-sentence takeaway.
Items marked lang=zh must be summarized in Chinese; everything else in English.

Respond with ONLY JSON:
{"papers": [{"id": "...", "summary": "...", "why": "..."}],
 "items": [{"id": "...", "takeaway": "..."}]}"""

_MIN_SCORE = 6


def run_research(llm: LLM, profile: dict, events: EventsStore, settings: Settings) -> dict:
    profile_summary = render_summary(profile)
    health: dict[str, str] = {}

    # ── 1. gather candidates ─────────────────────────────────────────
    papers = _gather_papers(llm, profile, profile_summary, settings, health)
    feed_items = _gather_feed_items(settings, health)

    # dedupe against everything ever surfaced
    papers = [p for p in papers if p["seen_id"] in set(events.filter_unseen([p["seen_id"] for p in papers]))]
    feed_items = [i for i in feed_items if i["seen_id"] in set(events.filter_unseen([i["seen_id"] for i in feed_items]))]

    # ── 2. cheap-model relevance scoring, one batch per pool ─────────
    render_feed = lambda i: f"[{i['source']}] {i['title']} — {i['summary'][:200]}"  # noqa: E731
    papers = _select(
        _score(llm, profile_summary, papers, lambda p: f"{p['title']} — {p['abstract'][:300]}"),
        min_score=_MIN_SCORE, top=settings.research_top_papers,
    )
    en_pool = _score(llm, profile_summary, [i for i in feed_items if i.get("lang") != "zh"], render_feed)
    zh_pool = _score(llm, profile_summary, [i for i in feed_items if i.get("lang") == "zh"], render_feed)
    industry = _select(en_pool, min_score=_MIN_SCORE, top=settings.research_top_feed_items)
    # the 中文媒体 section is a product requirement — lower bar plus a floor of 3
    chinese = _select(zh_pool, min_score=4, top=settings.research_top_feed_items,
                      floor=min(3, len(zh_pool)))

    # ── 3. one full-model call writes all summaries ──────────────────
    if papers or industry or chinese:
        _summarize(llm, profile_summary, papers, industry + chinese)

    return {
        "papers": papers,
        "industry": industry,
        "chinese": chinese,
        "source_health": health,
        "seen_ids": [x["seen_id"] for x in papers + industry + chinese],
    }


def _gather_papers(llm: LLM, profile: dict, profile_summary: str,
                   settings: Settings, health: dict) -> list[dict]:
    try:
        result = llm.complete_json(
            f"## Owner profile\n{profile_summary}", system=_QUERY_SYSTEM, max_tokens=1500
        )
        queries = [q for q in result.get("queries", []) if isinstance(q, str)][:6]
    except Exception as exc:
        log.warning("arxiv query generation failed: %s", exc)
        queries = []
    if not queries:
        # deterministic fallback: active interest topics straight from the profile
        queries = [
            str(i.get("topic")) for i in profile.get("interests", [])
            if i.get("topic") and i.get("status", "active") == "active"
        ][:5]
        if queries:
            health["arxiv"] = "queries from profile interests (LLM fallback)"
    if not queries:
        health["arxiv"] = "no queries generated"
        return []

    papers = arxiv.fetch_recent(queries, settings.arxiv_lookback_days, settings.arxiv_max_per_query)
    health["arxiv"] = f"{len(papers)} candidates from {len(queries)} queries"
    for p in papers:
        p["seen_id"] = f"arxiv-{p['id'].split('v')[0]}"
    return papers


def _gather_feed_items(settings: Settings, health: dict) -> list[dict]:
    items = []
    for source in feeds.load_sources(settings.sources_file):
        name = source.get("name", source.get("url", "?"))
        try:
            fetched = feeds.fetch_feed(source["url"])[:15]
            for item in fetched:
                if not item.get("title") or not item.get("url"):
                    continue
                item["source"] = name
                item["lang"] = source.get("lang", "en")
                item["seen_id"] = "feed-" + hashlib.sha1(item["url"].encode()).hexdigest()[:16]
                items.append(item)
            health[name] = f"{len(fetched)} items"
        except Exception as exc:
            health[name] = f"FAILED: {type(exc).__name__}"  # surfaced in the email footer
    return items


def _score(llm: LLM, profile_summary: str, pool: list[dict], render) -> list[dict]:
    """Annotate every item with _score and return the pool sorted by it (desc).
    On scorer failure every item gets _MIN_SCORE so downstream selection keeps
    natural order instead of dropping the whole pool."""
    if not pool:
        return []
    lines = "\n".join(f"[{i}] {render(x)}" for i, x in enumerate(pool))
    try:
        scored = llm.complete_json(
            f"## Owner profile\n{profile_summary}\n\n## Items\n{lines}",
            system=_SCORE_SYSTEM, model=llm.cheap_model, max_tokens=4000,
        )
        scores = {int(s["idx"]): int(s["score"]) for s in scored
                  if isinstance(s, dict) and "idx" in s and "score" in s}
    except Exception as exc:
        log.warning("relevance scoring failed, keeping natural order: %s", exc)
        scores = {}
    for i, item in enumerate(pool):
        item["_score"] = scores.get(i, _MIN_SCORE if not scores else 0)
    return sorted(pool, key=lambda x: -x["_score"])


def _select(ranked: list[dict], min_score: int, top: int, floor: int = 0) -> list[dict]:
    picked = [x for x in ranked if x["_score"] >= min_score][:top]
    if len(picked) < floor:  # quota: never let a required section go empty
        picked = ranked[:floor]
    return picked


def _summarize(llm: LLM, profile_summary: str, papers: list[dict], feed_items: list[dict]) -> None:
    paper_lines = "\n".join(
        f"id={p['seen_id']} :: {p['title']} :: {p['abstract'][:800]}" for p in papers
    )
    item_lines = "\n".join(
        f"id={i['seen_id']} lang={i.get('lang', 'en')} :: [{i['source']}] {i['title']} :: {i['summary'][:300]}"
        for i in feed_items
    )
    try:
        result = llm.complete_json(
            f"## Owner profile\n{profile_summary}\n\n## Papers\n{paper_lines or '(none)'}\n\n"
            f"## Feed items\n{item_lines or '(none)'}",
            system=_SUMMARY_SYSTEM, max_tokens=8000,
        )
    except Exception as exc:
        log.warning("summary generation failed: %s", exc)
        return
    summaries = {p.get("id"): p for p in result.get("papers", []) if isinstance(p, dict)}
    takeaways = {i.get("id"): i for i in result.get("items", []) if isinstance(i, dict)}
    for p in papers:
        entry = summaries.get(p["seen_id"], {})
        p["summary"] = entry.get("summary", p["abstract"][:300])
        p["why"] = entry.get("why", "")
    for i in feed_items:
        i["takeaway"] = takeaways.get(i["seen_id"], {}).get("takeaway", i["summary"][:200])
