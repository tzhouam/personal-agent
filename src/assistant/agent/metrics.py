"""Pipeline metrics: per-phase instrumentation + the digest's Health section.

Measurement plan and research citations: doc/PIPELINE_METRICS.md. Everything
here is computed from artifacts the pipeline already writes plus the owner's
implicit actions (todo done, reading done) — no explicit ratings.
"""

import html
import statistics
from collections import Counter
from datetime import date, datetime, timedelta

from assistant.agent.todo_store import ReadingList, TodoStore
from assistant.agent.urgency import going_stale


# what each phase's returned state-update contributes to the metrics table
# (duration and error count are recorded generically by the orchestrator wrapper)
def _collect(out: dict) -> dict:
    """Metrics for the collect phase: total observations, notifications, and a
    per-source ``obs_<source>`` breakdown."""
    values = {"observations": len(out.get("observations", [])),
              "notifications": len(out.get("notifications", []))}
    for source, n in Counter(o.get("source", "?")
                             for o in out.get("observations", [])).items():
        values[f"obs_{source}"] = n
    return values


def _digest(out: dict) -> dict:
    """Metrics for the digest phase: red/yellow/white section counts plus how
    many already-seen items were suppressed."""
    sections = out.get("digest", {}).get("sections", {})
    return {"red": len(sections.get("red", [])),
            "yellow": len(sections.get("yellow", [])),
            "white": len(sections.get("white", [])),
            "suppressed": out.get("digest", {}).get("suppressed_seen", 0)}


EXTRACTORS = {
    "collect": _collect,
    "profile": lambda out: {"ops_applied": len(out.get("profile_ops", []))},
    "digest": _digest,
    "todos": lambda out: {"wip": out.get("todos", {}).get("open_count", 0),
                          "added": len(out.get("todos", {}).get("added", [])),
                          "auto_closed": len(out.get("todos", {}).get("closed", []))},
    "research": lambda out: {
        "papers": len(out.get("research", {}).get("papers", [])),
        "paper_quota": out.get("research", {}).get("paper_quota", 0),
        "industry": len(out.get("research", {}).get("industry", [])),
        "sources_ok": sum(1 for v in out.get("research", {}).get("source_health", {}).values()
                          if str(v).startswith("ok")),
        "sources_total": len(out.get("research", {}).get("source_health", {}))},
    "website": lambda out: {
        "pushed": 1 if out.get("website", {}).get("status") in ("pushed", "no_change") else 0},
    "deliver": lambda out: {"email_sent": 1 if out.get("email_sent") else 0},
    "curate": lambda out: {"decayed": len(out.get("curated", {}).get("decayed", []))},
}


# ── the 7-day health summary (rendered into the digest email) ────────

def _day(value) -> date | None:
    """Parse the leading ``YYYY-MM-DD`` of ``value`` to a date, or None if
    empty/unparseable — dates in the stores are best-effort."""
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date() if value else None
    except ValueError:
        return None


def _series(rows: list[dict], step: str, name: str) -> list[float]:
    """The values of one ``(step, name)`` metric across ``rows``, in row order."""
    return [r["value"] for r in rows if r["step"] == step and r["name"] == name]


def _rate(num: int, den: int) -> str:
    """Format ``num/den`` with a percentage, omitting the percent on den 0
    (avoiding division by zero)."""
    return f"{num}/{den}" + (f" ({100 * num // den}%)" if den else "")


def build_health(events, profile_dir, days: int = 7) -> list[tuple[str, str]]:
    """[(label, value), ...] — the 7-day view of doc/PIPELINE_METRICS.md's
    headline metrics. Every line degrades to '—' rather than raising."""
    rows = events.metrics_window(days)
    today = date.today()
    lines: list[tuple[str, str]] = []

    # step health: runs seen and runs with errors, worst offenders first
    durations = _series(rows, "run", "duration_s")
    run_ids = {r["run_id"] for r in rows}
    step_errors = Counter()
    for r in rows:
        if r["name"] == "errors" and r["value"] > 0:
            step_errors[r["step"]] += 1
    lines.append((f"runs ({days}d)", str(len(run_ids)) + (
        f" · median {statistics.median(durations) / 60:.1f} min" if durations else "")))
    lines.append(("steps with errors", ", ".join(
        f"{s}×{n}" for s, n in step_errors.most_common(3)) or "none"))

    obs = _series(rows, "collect", "observations")
    if obs:
        lines.append(("observations/run", f"median {statistics.median(obs):.0f}"
                      + (" · ⚠ latest 0" if obs[-1] == 0 else "")))

    applied = sum(_series(rows, "profile", "ops_applied"))
    rejected = sum(_series(rows, "profile", "ops_rejected"))
    if applied or rejected:
        lines.append(("profile ops acceptance", _rate(int(applied), int(applied + rejected))))

    red = sum(_series(rows, "digest", "red"))
    suppressed = sum(_series(rows, "digest", "suppressed"))
    lines.append(("digest reds / suppressed", f"{int(red)} / {int(suppressed)}"))

    # red action rate — SRE alerting precision proxy: of github-sourced todos
    # created 7..30 days ago, how many did the owner (or the monitor, on the
    # owner's action) actually close?
    todo_items = TodoStore(profile_dir).load()["items"]
    window = [t for t in todo_items
              if t.get("source") == "github" and t.get("priority") == "red"
              and (_day(t.get("created")) or today) < today - timedelta(days=7)
              and (_day(t.get("created")) or date.min) > today - timedelta(days=30)]
    if window:
        acted = sum(1 for t in window
                    if t.get("status") == "done" and t.get("closed") != "outdated")
        lines.append(("red action rate (7-30d)", _rate(acted, len(window))))

    open_todos = [t for t in todo_items if t["status"] == "open"]
    done_week = [t for t in todo_items if t.get("status") == "done"
                 and (_day(t.get("done_at")) or date.min) >= today - timedelta(days=days)]
    ages = sorted((today - (_day(t.get("created")) or today)).days for t in open_todos)
    stale = sum(1 for t in open_todos if going_stale(t, today))
    lines.append(("todos WIP / done 7d / going-stale",
                  f"{len(open_todos)} / {len(done_week)} / {stale}"))
    if ages:
        lines.append(("todo age median·max (d)", f"{ages[len(ages) // 2]} · {ages[-1]}"))

    reading = ReadingList(profile_dir).load()["items"]
    surfaced_week = [r for r in reading
                     if (_day(r.get("created")) or date.min) >= today - timedelta(days=days)]
    read_week = [r for r in reading if r.get("status") == "done"
                 and (_day(r.get("done_at")) or date.min) >= today - timedelta(days=days)]
    lines.append(("reading surfaced / read (7d)", f"{len(surfaced_week)} / {len(read_week)}"))

    # chat outcome labels (chat/agent.py) — success means the owner was
    # satisfied, so the dissatisfied count rides next to the success rate
    cs = int(sum(_series(rows, "chat_turn", "success")))
    cf = int(sum(_series(rows, "chat_turn", "fail")))
    cn = int(sum(_series(rows, "chat_turn", "neutral")))
    cr = int(sum(_series(rows, "chat_turn", "repaired")))
    cd = int(sum(_series(rows, "chat_turn", "prev_dissatisfied")))
    if cs or cf or cn:  # omitted entirely for pre-label data
        lines.append((f"chat turns ({days}d)",
                      f"{_rate(cs, cs + cf)} success · {cn} neutral"
                      + (f" · {cd} dissatisfied" if cd else "")
                      + (f" · {cr} repaired" if cr else "")))

    checked = _series(rows, "consolidate", "claims_checked")
    if checked:  # weekly judge audit (faithfulness/staleness/contradiction)
        last = {name: int(_series(rows, "consolidate", name)[-1])
                for name in ("contradictions", "stale_claims", "unsupported_claims")}
        lines.append(("profile audit (weekly)",
                      f"{last['contradictions']} contradictions · {last['stale_claims']} stale"
                      f" · {last['unsupported_claims']} unsupported of {int(checked[-1])} claims"))

    pushed = _series(rows, "website", "pushed")
    sent = _series(rows, "deliver", "email_sent")
    lines.append(("website publishes / emails",
                  f"{_rate(int(sum(pushed)), len(pushed))} / {_rate(int(sum(sent)), len(sent))}"))
    return lines


def render_health_html(lines: list[tuple[str, str]]) -> str:
    """Render build_health()'s (label, value) pairs as the digest email's
    Health table. Empty input yields "" (section omitted); all cells are
    HTML-escaped."""
    if not lines:
        return ""
    cells = "".join(
        f"<tr><td style='color:#6b7280;padding:2px 12px 2px 0'>{html.escape(k)}</td>"
        f"<td>{html.escape(v)}</td></tr>" for k, v in lines)
    return ("<h3 style='margin-bottom:4px'>📈 Health (7 days)</h3>"
            f"<table style='font-size:13px;border-collapse:collapse'>{cells}</table>")
