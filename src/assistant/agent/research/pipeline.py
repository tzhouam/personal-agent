"""The daily research pipeline: gather arXiv papers and RSS/Atom feed items,
score them against the owner profile with the cheap model, select the top of
each pool, then write all summaries in one full-model call. `run_research` is the
entry point; the module also owns the engagement-driven adaptive paper quota."""

import hashlib
import logging

from assistant.platform.config import Settings
from assistant.agent.events_store import EventsStore
from assistant.platform.llm import LLM
from assistant.agent.profile_store import render_summary
from assistant.agent.research import arxiv, feeds

log = logging.getLogger("assistant")

_QUERY_SYSTEM = """Given a profile of your owner, produce arXiv search phrases capturing what they
would want in a daily research digest. Respond immediately with ONLY this JSON, no other text:
{"queries": ["<3-6 short search phrases, each 2-4 words, e.g. 'LLM inference serving'>"]}"""

_SCORE_SYSTEM = """Score each item's relevance to the owner profile from 0 (irrelevant) to 10
(must-read). Judge by topical overlap with their skills, interests, and active projects.
If a "Rejected as unrelated" list is present, those are items the owner explicitly rejected —
score anything topically similar to them 0-2.
Respond with ONLY a JSON array: [{"idx": <int>, "score": <int>}] covering every idx given."""

_SUMMARY_SYSTEM = """Write the research section of the owner's daily digest.

For each paper: a 2-3 sentence summary plus one "why" sentence explicitly tied to the owner's
profile (their projects/interests). For each feed item: a one-sentence takeaway.
Items marked lang=zh must be summarized in Chinese; everything else in English.

Respond with ONLY JSON:
{"papers": [{"id": "...", "summary": "...", "why": "..."}],
 "items": [{"id": "...", "takeaway": "..."}]}"""

_MIN_SCORE = 6
_QUOTA_WINDOW_DAYS = 14
_QUOTA_MIN_HISTORY = 20  # surfaced items needed before the controller kicks in
_QUOTA_FLOOR = 2


def adaptive_paper_quota(settings: Settings, reading_items: list[dict],
                         today=None) -> tuple[int, str]:
    """Tune how many papers to surface against the owner's actual engagement
    (doc/PIPELINE_METRICS.md §6 — done-rate is the implicit relevance label).
    Sustainable surfacing ≈ 1.5× the rate the owner acts (done OR unrelated)
    on items, floored at 2/day so discovery never fully stops. Cold start:
    keep the configured quota until enough history exists."""
    import math
    from datetime import date, timedelta

    today = today or date.today()
    cutoff = (today - timedelta(days=_QUOTA_WINDOW_DAYS)).isoformat()
    surfaced = [r for r in reading_items if str(r.get("created", "")) >= cutoff]
    if len(surfaced) < _QUOTA_MIN_HISTORY:
        return settings.research_top_papers, ""
    acted = [r for r in reading_items
             if str(r.get("done_at", "")) >= cutoff
             or str(r.get("unrelated_at", "")) >= cutoff]
    per_day = len(acted) / _QUOTA_WINDOW_DAYS
    quota = max(_QUOTA_FLOOR,
                min(settings.research_top_papers, math.ceil(per_day * 1.5) + 1))
    note = ""
    if quota < settings.research_top_papers:
        note = (f"paper quota {settings.research_top_papers}→{quota}: you acted on "
                f"{len(acted)} of {len(surfaced)} items surfaced in the last "
                f"{_QUOTA_WINDOW_DAYS}d — marking items done/unrelated raises it")
    return quota, note


def run_research(llm: LLM, profile: dict, events: EventsStore, settings: Settings) -> dict:
    """Run the full research digest and return its sections (papers, industry,
    chinese) plus source_health and the seen_ids to record.

    Threads the owner's reading-list feedback into scoring: items marked
    unrelated become negative examples in the prompt, and the done/unrelated
    rate tunes the adaptive paper quota. Candidates are deduped against
    everything ever surfaced (`events.filter_unseen`), scored one batch per pool
    (English feeds and Chinese feeds separately, since 中文媒体 is a required
    section with a lower bar and a floor), and only then summarized. Returned
    requested/completed/fallback counters make the existing deterministic
    score/summary fallbacks visible without changing their behavior."""
    profile_summary = render_summary(profile)
    health: dict[str, str] = {}
    accounting = {
        "score_requested": 0,
        "score_completed": 0,
        "score_fallback": 0,
        "summary_requested": 0,
        "summary_completed": 0,
        "summary_fallback": 0,
    }

    # negative feedback: readings the owner marked unrelated bias the scorer
    from assistant.agent.todo_store import ReadingList

    reading_items = ReadingList(settings.profile_dir).load()["items"]
    paper_quota, quota_note = adaptive_paper_quota(settings, reading_items)
    if quota_note:
        health["paper quota"] = quota_note

    negatives = ReadingList(settings.profile_dir).unrelated_titles()
    if negatives:
        profile_summary += ("\n\n## Rejected as unrelated by the owner recently\n"
                            + "\n".join(f"- {t}" for t in negatives))

    # ── 1. gather candidates ─────────────────────────────────────────
    papers = _gather_papers(llm, profile, profile_summary, settings, health)
    feed_items = _gather_feed_items(settings, health)

    # dedupe against everything ever surfaced
    papers = [p for p in papers if p["seen_id"] in set(events.filter_unseen([p["seen_id"] for p in papers]))]
    feed_items = [i for i in feed_items if i["seen_id"] in set(events.filter_unseen([i["seen_id"] for i in feed_items]))]

    # ── 2. cheap-model relevance scoring, one batch per pool ─────────
    render_feed = lambda i: f"[{i['source']}] {i['title']} — {i['summary'][:200]}"  # noqa: E731
    papers = _select(
        _score(llm, profile_summary, papers,
               lambda p: f"{p['title']} — {p['abstract'][:300]}", accounting),
        min_score=_MIN_SCORE, top=paper_quota,
    )
    en_pool = _score(llm, profile_summary,
                     [i for i in feed_items if i.get("lang") != "zh"],
                     render_feed, accounting)
    zh_pool = _score(llm, profile_summary,
                     [i for i in feed_items if i.get("lang") == "zh"],
                     render_feed, accounting)
    industry = _select(en_pool, min_score=_MIN_SCORE, top=settings.research_top_feed_items)
    # the 中文媒体 section is a product requirement — lower bar plus a floor of 3
    chinese = _select(zh_pool, min_score=4, top=settings.research_top_feed_items,
                      floor=min(3, len(zh_pool)))

    # ── 3. one full-model call writes all summaries ──────────────────
    if papers or industry or chinese:
        _summarize(llm, profile_summary, papers, industry + chinese, accounting)

    return {
        "paper_quota": paper_quota,
        "papers": papers,
        "industry": industry,
        "chinese": chinese,
        "source_health": health,
        "seen_ids": [x["seen_id"] for x in papers + industry + chinese],
        **accounting,
        "degraded": bool(accounting["score_fallback"]
                         or accounting["summary_fallback"]),
    }


def _gather_papers(llm: LLM, profile: dict, profile_summary: str,
                   settings: Settings, health: dict) -> list[dict]:
    """Generate arXiv queries from the profile and fetch recent candidates,
    tagging each with a `seen_id`. The LLM proposes ≤6 search phrases; on failure
    it falls back to the profile's active interest topics, and to no papers if
    even that is empty. `health` is annotated with what happened for the footer."""
    try:
        result = llm.complete_json(
            f"## Owner profile\n{profile_summary}", system=_QUERY_SYSTEM,
            max_tokens=1500, role="pipeline",
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
    """Fetch every configured feed source (≤15 items each), tagging items with
    their source name, language, and a url-hashed `seen_id`. A failing source is
    recorded as FAILED in `health` (surfaced in the email footer) and skipped, so
    one broken scraper never kills the sweep.

    A *missing* sources file is recorded as a FAILED row of its own rather than
    passing silently: `load_sources` degrades to `[]` for an absent file, so a
    misresolved `sources_file` looked exactly like "no news today" — three days
    of empty industry/中文 sections (2026-07-22→24) raised nothing anywhere.
    Health values are prefixed `ok:`/`FAILED` so the run can count them."""
    items = []
    if not settings.sources_file.exists():
        health["sources"] = f"FAILED: sources file missing ({settings.sources_file})"
        return items
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
            health[name] = f"ok: {len(fetched)} items"
        except Exception as exc:
            health[name] = f"FAILED: {type(exc).__name__}"  # surfaced in the email footer
    return items


def _score(llm: LLM, profile_summary: str, pool: list[dict], render,
           accounting: dict | None = None) -> list[dict]:
    """Annotate every item with _score and return the pool sorted by it (desc).
    On scorer failure every item gets _MIN_SCORE so downstream selection keeps
    natural order instead of dropping the whole pool."""
    if not pool:
        return []
    if accounting is not None:
        accounting["score_requested"] += len(pool)
    lines = "\n".join(f"[{i}] {render(x)}" for i, x in enumerate(pool))
    try:
        scored = llm.complete_json(
            f"## Owner profile\n{profile_summary}\n\n## Items\n{lines}",
            system=_SCORE_SYSTEM, role="research", max_tokens=4000,
        )
        scores = {int(s["idx"]): int(s["score"]) for s in scored
                  if isinstance(s, dict) and "idx" in s and "score" in s}
    except Exception as exc:
        log.warning("relevance scoring failed, keeping natural order: %s", exc)
        scores = {}
    completed = sum(1 for i in range(len(pool)) if i in scores)
    if accounting is not None:
        accounting["score_completed"] += completed
        accounting["score_fallback"] += len(pool) - completed
    for i, item in enumerate(pool):
        item["_score"] = scores.get(i, _MIN_SCORE if not scores else 0)
    return sorted(pool, key=lambda x: -x["_score"])


def _select(ranked: list[dict], min_score: int, top: int, floor: int = 0) -> list[dict]:
    """Pick from a `_score`-ranked pool: the top `top` items scoring at least
    `min_score`. `floor` is a quota — if the threshold yields fewer than `floor`
    items, take the top `floor` regardless, so a required section never goes empty."""
    picked = [x for x in ranked if x["_score"] >= min_score][:top]
    if len(picked) < floor:  # quota: never let a required section go empty
        picked = ranked[:floor]
    return picked


def _summarize(llm: LLM, profile_summary: str, papers: list[dict],
               feed_items: list[dict], accounting: dict | None = None) -> None:
    """One full-model call writes every summary, mutating `papers` and
    `feed_items` in place: each paper gets `summary` + profile-tied `why`, each
    feed item a `takeaway` (zh items in Chinese). On failure the items fall back
    to a truncated abstract/summary, so the digest still renders."""
    paper_lines = "\n".join(
        f"id={p['seen_id']} :: {p['title']} :: {p['abstract'][:800]}" for p in papers
    )
    item_lines = "\n".join(
        f"id={i['seen_id']} lang={i.get('lang', 'en')} :: [{i['source']}] {i['title']} :: {i['summary'][:300]}"
        for i in feed_items
    )
    requested = len(papers) + len(feed_items)
    if accounting is not None:
        accounting["summary_requested"] += requested
    try:
        result = llm.complete_json(
            f"## Owner profile\n{profile_summary}\n\n## Papers\n{paper_lines or '(none)'}\n\n"
            f"## Feed items\n{item_lines or '(none)'}",
            system=_SUMMARY_SYSTEM, max_tokens=8000, role="pipeline",
        )
        if not isinstance(result, dict):
            raise ValueError("summary response must be an object")
        raw_papers = result.get("papers", [])
        raw_items = result.get("items", [])
        if not isinstance(raw_papers, list) or not isinstance(raw_items, list):
            raise ValueError("summary response lists are malformed")
    except Exception as exc:
        log.warning("summary generation failed: %s", exc)
        if accounting is not None:
            accounting["summary_fallback"] += requested
        _apply_summary_fallbacks(papers, feed_items)
        return
    summaries = {p["id"]: p for p in raw_papers
                 if isinstance(p, dict) and isinstance(p.get("id"), str)
                 and p.get("id")}
    takeaways = {i["id"]: i for i in raw_items
                 if isinstance(i, dict) and isinstance(i.get("id"), str)
                 and i.get("id")}

    def nonempty(value) -> bool:
        return isinstance(value, str) and bool(value.strip())

    completed = sum(
        1 for paper in papers
        if paper["seen_id"] in summaries
        and nonempty(summaries[paper["seen_id"]].get("summary"))
        and nonempty(summaries[paper["seen_id"]].get("why"))
    ) + sum(
        1 for item in feed_items
        if item["seen_id"] in takeaways
        and nonempty(takeaways[item["seen_id"]].get("takeaway"))
    )
    if accounting is not None:
        accounting["summary_completed"] += completed
        accounting["summary_fallback"] += requested - completed
    for p in papers:
        entry = summaries.get(p["seen_id"], {})
        p["summary"] = (entry.get("summary") if nonempty(entry.get("summary"))
                        else p["abstract"][:300])
        p["why"] = entry.get("why") if nonempty(entry.get("why")) else ""
    for i in feed_items:
        takeaway = takeaways.get(i["seen_id"], {}).get("takeaway")
        i["takeaway"] = takeaway if nonempty(takeaway) else i["summary"][:200]


def _apply_summary_fallbacks(papers: list[dict], feed_items: list[dict]) -> None:
    """Populate the deterministic values promised by `_summarize`'s contract."""
    for paper in papers:
        summary = paper.get("summary")
        why = paper.get("why")
        paper["summary"] = (summary if isinstance(summary, str) and summary.strip()
                            else str(paper.get("abstract", ""))[:300])
        paper["why"] = why if isinstance(why, str) else ""
    for item in feed_items:
        takeaway = item.get("takeaway")
        item["takeaway"] = (
            takeaway if isinstance(takeaway, str) and takeaway.strip()
            else str(item.get("summary", ""))[:200])
