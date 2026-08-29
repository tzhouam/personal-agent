"""Gather a self-improvement briefing from recent traces and history.

Reads run traces (`runs/*/trace.jsonl` — slow LLM calls, errors, truncations),
chat sessions (friction: retries/rejections/corrections), and agentic task
records (failures/aborts), and prints a compact markdown brief to stdout. The
weekly self-improve job feeds this to Opus 4.8.

Deployment-mode aware (doc/DESIGN_MULTI_USER.md §12b): in `single_user` it
reads the root data dir exactly as before; in `multi_tenant` it iterates every
**active** registered user's `users/<uid>/` data (all users have mutually
authorized this) and tags each section per uid, so the improvement loop sees
the whole deployment's friction, not one user's.

Local-only: the brief is written next to the data dir and never committed —
only the resulting code changes reach git (via the PR-only harness).
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 2
CUTOFF = datetime.now().astimezone() - timedelta(days=DAYS)
PER_USER_LINE_CAP = 25   # keep the brief compact even with several users


def _durable_moa(data: Path) -> str:
    """Recent MoA totals from the read-only durable metrics table.

    Unlike trace spans this includes chat/routine calls that have no tracer.
    Missing, old-schema, busy, or corrupt databases simply yield no line: the
    trace aggregate remains the compatibility fallback.
    """
    db = data / "events.db"
    if not db.is_file():
        return ""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.2)
        try:
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute(
                "SELECT run_id, ts, name, value FROM metrics "
                "WHERE step = 'moa' AND ts >= ? ORDER BY ts", (cutoff,)
            ).fetchall()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return ""
    if not rows:
        return ""
    calls: dict[tuple[str, str], dict[str, float]] = {}
    for run_id, ts, name, value in rows:
        try:
            calls.setdefault((str(run_id), str(ts)), {})[str(name)] = float(value)
        except (TypeError, ValueError):
            continue
    records = [row for row in calls.values() if "members_total" in row]
    if not records:
        return ""

    def total(rows_: list[dict], name: str) -> int:
        return int(sum(row.get(name, 0) for row in rows_))

    required = {"members_attempted", "members_skipped", "members_failed",
                "proposals_final", "aggregator_attempted", "aggregator_skipped",
                "aggregator_failed", "degraded"}
    instrumented = [row for row in records if required <= row.keys()]
    legacy = [row for row in records if not required <= row.keys()]
    lines = [
        f"- Durable MoA: calls={len(records)} "
        f"(instrumented={len(instrumented)}, legacy={len(legacy)}); "
        f"aggregators successful={total(records, 'aggregator_ok')}/{len(records)}; "
        f"fallbacks={total(records, 'fallback_used')}; "
        f"abandoned={total(records, 'abandoned')}"
    ]
    if instrumented:
        attempted = total(instrumented, "members_attempted")
        skipped = total(instrumented, "members_skipped")
        lines.append(
            f"- Instrumented MoA: member slots attempted={attempted}/"
            f"{attempted + skipped} skipped={skipped} "
            f"failed={total(instrumented, 'members_failed')}; proposer successes="
            f"{total(instrumented, 'proposals_ok')}; final proposals="
            f"{total(instrumented, 'proposals_final')}; aggregator attempts="
            f"{total(instrumented, 'aggregator_attempted')} "
            f"skipped={total(instrumented, 'aggregator_skipped')} "
            f"failed={total(instrumented, 'aggregator_failed')}; "
            f"degraded={total(instrumented, 'degraded')}/{len(instrumented)}")
    if legacy:
        # Legacy proposals_ok accumulated across layers, while members_total did
        # not, so no mathematically valid proposal denominator can be recovered.
        lines.append(
            f"- Legacy MoA (partial schema): proposer successes="
            f"{total(legacy, 'proposals_ok')}; configured members="
            f"{total(legacy, 'members_total')}; aggregators successful="
            f"{total(legacy, 'aggregator_ok')}/{len(legacy)}")
    return "\n".join(lines)


def _data_roots() -> list[tuple[str, Path]]:
    """`[(label, data_dir)]` to scan: the root dir in single_user (label ''),
    each active user's dir in multi_tenant. Falls back to the plain root if the
    assistant package/registry is unavailable (never crash the harness)."""
    root = Path.home() / ".personal-agent"
    try:
        from assistant.platform.config import Settings

        settings = Settings()
        root = Path(settings.data_dir)
        if settings.deployment_mode == "multi_tenant":
            from assistant.platform.registry import UserRegistry

            return [(uid, root / "users" / uid)
                    for uid in UserRegistry(root).active()]
    except Exception:
        pass
    return [("", root)]


def _traces(data: Path) -> list[str]:
    """Aggregate recent run health without copying owner-authored content.

    Trace span timestamps are authoritative when present: directory ordering is
    not recency (a resumed/copied run may have an old name, while a stale trace
    can sit in a lexically new directory). A run-id timestamp is used only for
    legacy/empty traces. Digest and research artifacts contribute counts only;
    titles, URLs, summaries, prompts, and source names never leave the data dir.
    """
    durable_moa = _durable_moa(data)
    runs_dir = data / "runs"
    if not runs_dir.exists():
        return [durable_moa] if durable_moa else []

    counts = {"runs": 0, "llm": 0, "slow": 0, "trunc": 0, "errors": 0,
              "moa_calls": 0, "moa_members": 0, "moa_proposals": 0,
              "moa_aggregators": 0, "moa_fallbacks": 0, "moa_abandoned": 0,
              "digest_runs": 0, "digest_eligible": 0, "digest_triaged": 0,
              "digest_fallback": 0,
              "research_runs": 0, "research_selected": 0,
              "research_summarized": 0}
    research_explicit = {"runs": 0, "score_requested": 0, "score_completed": 0,
                         "score_fallback": 0, "summary_requested": 0,
                         "summary_completed": 0, "summary_fallback": 0}
    research_legacy = {"runs": 0}
    research_health = {"sources_ok": 0, "sources_total": 0}
    cutoff_ts = CUTOFF.timestamp()

    def number(value) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    def span_ts(span: dict) -> float | None:
        for key in ("end", "start"):
            try:
                stamp = float(span.get(key))
            except (TypeError, ValueError):
                continue
            if stamp > 0:
                return stamp
        return None

    def run_id_ts(run: Path) -> float | None:
        try:
            return datetime.strptime(run.name, "run-%Y%m%d-%H%M%S").timestamp()
        except ValueError:
            return None

    def artifact(run: Path, name: str) -> dict:
        try:
            value = json.loads((run / name).read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def is_phase_span(span: dict, phase: str) -> bool:
        attr = span.get("attr", {})
        return (span.get("name") == "phase" and isinstance(attr, dict)
                and attr.get("phase") == phase)

    def artifact_is_recent(run: Path, name: str, phase: str,
                           spans: list[dict], stamps: list[float]) -> bool:
        """Whether one phase's artifact belongs in this evidence window.

        A resumed run can contain yesterday's digest beside today's later phase,
        so run-wide recency is too broad. Prefer a timestamped span for the
        artifact's own phase; legacy clockless traces may use the run-id clock.
        A recent artifact mtime is the independent fallback for partial runs,
        disabled tracing, and artifacts rewritten outside the full graph.
        """
        for span in spans:
            if not is_phase_span(span, phase):
                continue
            stamp = span_ts(span)
            if stamp is not None and stamp >= cutoff_ts:
                return True
            if stamp is None and not stamps and (run_id_ts(run) or 0) >= cutoff_ts:
                return True
        try:
            return (run / name).stat().st_mtime >= cutoff_ts
        except OSError:
            return False

    for run in sorted(runs_dir.glob("run-*")):
        tf = run / "trace.jsonl"
        spans: list[dict] = []
        try:
            if tf.exists():
                for line in tf.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        spans.append(value)
        except OSError:
            spans = []

        stamps = [stamp for stamp in (span_ts(s) for s in spans)
                  if stamp is not None]
        # Recorded span time wins over a suggestive directory name. Fall back to
        # the timestamp encoded in the run id only when the trace has no clock.
        recent_trace = ((max(stamps) >= cutoff_ts) if stamps else
                        ((run_id_ts(run) or 0) >= cutoff_ts))
        digest = artifact(run, "digest.json")
        research = artifact(run, "research.json")
        digest_recent = bool(digest) and artifact_is_recent(
            run, "digest.json", "digest", spans, stamps)
        research_recent = bool(research) and artifact_is_recent(
            run, "research.json", "research", spans, stamps)
        if not (recent_trace or digest_recent or research_recent):
            continue
        counts["runs"] += 1
        recent_spans = (([s for s in spans if (span_ts(s) or 0) >= cutoff_ts]
                         if stamps else spans) if recent_trace else [])

        for s in recent_spans:
            a = s.get("attr", {})
            if not isinstance(a, dict):
                a = {}
            status = str(a.get("status", "")).lower()
            if (a.get("ok") is False or a.get("error") or a.get("error_type")
                    or status in {"error", "failed"}):
                counts["errors"] += 1
            if s.get("name") == "mixture":
                counts["moa_calls"] += 1
                counts["moa_members"] += number(a.get("members_total"))
                counts["moa_proposals"] += number(a.get("proposals_ok"))
                counts["moa_aggregators"] += number(a.get("aggregator_ok"))
                counts["moa_fallbacks"] += number(a.get("fallback_used"))
                counts["moa_abandoned"] += number(a.get("abandoned"))
            if s.get("name") != "llm":
                continue
            counts["llm"] += 1
            dur = number(s.get("dur_ms")) / 1000
            if dur > 45:
                counts["slow"] += 1
            if a.get("stop_reason") == "max_tokens":
                counts["trunc"] += 1

        if digest_recent:
            total = max(0, number(digest.get("total")))
            overflow = max(0, number(digest.get("overflow")))
            requested = (max(0, number(digest.get("llm_requested")))
                         if "llm_requested" in digest else max(0, total - overflow))
            triaged = max(0, number(digest.get("llm_triaged")))
            fallback = (max(0, number(digest.get("fallback_count")))
                        if "fallback_count" in digest else max(0, requested - triaged))
            counts["digest_runs"] += 1
            counts["digest_eligible"] += requested
            counts["digest_triaged"] += triaged
            counts["digest_fallback"] += fallback

        if research_recent:
            papers = research.get("papers") if isinstance(research.get("papers"), list) else []
            feeds_ = []
            for key in ("industry", "chinese"):
                if isinstance(research.get(key), list):
                    feeds_.extend(research[key])
            counts["research_runs"] += 1
            explicit_keys = {
                "score_requested", "score_completed", "score_fallback",
                "summary_requested", "summary_completed", "summary_fallback",
            }
            if explicit_keys <= research.keys():
                research_explicit["runs"] += 1
                for key in explicit_keys:
                    research_explicit[key] += max(0, number(research.get(key)))
            else:
                # Compatibility only: old artifacts cannot distinguish a model
                # summary from deterministic fallback text, so label this legacy.
                research_legacy["runs"] += 1
                counts["research_selected"] += len(papers) + len(feeds_)
                counts["research_summarized"] += sum(
                    1 for p in papers if isinstance(p, dict) and p.get("summary"))
                counts["research_summarized"] += sum(
                    1 for item in feeds_ if isinstance(item, dict) and item.get("takeaway"))
            health = research.get("source_health", {})
            values = health.values() if isinstance(health, dict) else []
            for value in values:
                text = str(value)
                if text.startswith("ok:"):
                    research_health["sources_ok"] += 1
                    research_health["sources_total"] += 1
                elif text.startswith("FAILED"):
                    research_health["sources_total"] += 1

    if not counts["runs"]:
        return [durable_moa] if durable_moa else []
    out = [
        (f"- runs={counts['runs']}; LLM calls={counts['llm']}; "
         f"slow (>45s)={counts['slow']}; truncations={counts['trunc']}; "
         f"explicit span errors={counts['errors']}")
    ]
    if durable_moa:
        out.append(durable_moa)
    elif counts["moa_calls"]:
        # Legacy trace spans expose cumulative proposer successes but only a
        # per-call configured-member count. With multiple refinement layers,
        # dividing those values would produce impossible ratios such as 4/2.
        out.append(
            f"- MoA health: calls={counts['moa_calls']}; "
            f"proposer successes={counts['moa_proposals']}; "
            f"configured members={counts['moa_members']}; "
            f"aggregators={counts['moa_aggregators']}/{counts['moa_calls']}; "
            f"fallbacks={counts['moa_fallbacks']}; abandoned={counts['moa_abandoned']}")
    if counts["digest_runs"]:
        out.append(
            f"- Digest coverage: LLM-triaged={counts['digest_triaged']}/"
            f"{counts['digest_eligible']}; deterministic fallbacks="
            f"{counts['digest_fallback']}; "
            f"runs={counts['digest_runs']}")
    if research_explicit["runs"]:
        out.append(
            f"- Research coverage: scored={research_explicit['score_completed']}/"
            f"{research_explicit['score_requested']} "
            f"(fallbacks={research_explicit['score_fallback']}); summarized="
            f"{research_explicit['summary_completed']}/"
            f"{research_explicit['summary_requested']} "
            f"(fallbacks={research_explicit['summary_fallback']}); "
            f"runs={research_explicit['runs']}")
    if research_legacy["runs"]:
        out.append(
            f"- Research legacy coverage (fallback indistinguishable): summarized="
            f"{counts['research_summarized']}/"
            f"{counts['research_selected']}; "
            f"runs={research_legacy['runs']}")
    if research_health["sources_total"]:
        out.append(
            f"- Research source health: healthy={research_health['sources_ok']}/"
            f"{research_health['sources_total']}; runs={counts['research_runs']}")
    return out


def _sessions(data: Path) -> list[str]:
    """Chat exchanges showing friction (retries/rejections/errors) or owner corrections."""
    out: list[str] = []
    sessions_dir = data / "sessions"
    if not sessions_dir.exists():
        return out
    # sessions are day-sharded (sessions/<hash>/<date>.json); read every shard,
    # tolerating a legacy flat sessions/<hash>.json (pre-migration). semantics
    # unchanged: filter all turns by CUTOFF, cap total output at the end
    for p in sorted(list(sessions_dir.glob("*/*.json")) + list(sessions_dir.glob("*.json"))):
        try:
            turns = json.loads(p.read_text()).get("turns", [])
        except ValueError:
            continue
        for t in turns:
            ts = t.get("ts", "")
            if ts and not _timestamp_is_recent(ts):
                continue
            owner, reply = str(t.get("owner", "")), str(t.get("assistant", ""))
            label = t.get("outcome")
            if label:  # structured per-turn label (chat/agent.py Stage 1+2)
                friction = (label == "fail" or bool(t.get("repaired"))
                            or t.get("owner_verdict") == "dissatisfied")
            else:  # pre-label turns: legacy keyword heuristic
                friction = any(s in reply for s in
                               ("(retry)", "NOT logged", "rejected", "couldn't",
                                "failed", "assistant error", "无法", "抱歉"))
            # standalone copy of chat/agent.py CORRECTION_MARKERS (this script
            # must run without the package importable) — update both together
            corr = any(s in owner for s in
                       ("不对", "不是", "错", "改成", "别再", "以后", "应该", "重新", "取消"))
            if friction or corr:
                tag = ("friction" if friction else "") + ("+correction" if corr else "")
                out.append(f"- [{tag}] owner: {owner[:140]!r}\n    agent: {reply[:200]!r}")
    return out[-PER_USER_LINE_CAP:]


def _timestamp_is_recent(value) -> bool:
    """Parse a session timestamp and compare instants, not ISO strings.

    Session history contains both legacy local-naive values and current aware
    ISO timestamps (including ``Z``). Naive values mean local wall time, matching
    how ``CUTOFF`` is constructed. Invalid non-empty values cannot be placed in
    the requested window and are therefore ignored; missing legacy timestamps
    retain the caller's existing include behavior.
    """
    try:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        stamp = datetime.fromisoformat(text)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            stamp = stamp.replace(tzinfo=CUTOFF.tzinfo)
        return (stamp.astimezone(timezone.utc)
                >= CUTOFF.astimezone(timezone.utc))
    except (TypeError, ValueError, OverflowError):
        return False


def _tasks(data: Path) -> list[str]:
    """Agentic task runs that aborted or hit failing steps."""
    out: list[str] = []
    tasks_dir = data / "tasks"
    if not tasks_dir.exists():
        return out
    for p in sorted(tasks_dir.glob("task-*.json")):
        if p.stem[5:13] < CUTOFF.strftime("%Y%m%d"):
            continue
        try:
            r = json.loads(p.read_text())
        except ValueError:
            continue
        fails = [s for s in r.get("steps", [])
                 if s.get("outcome") and "fail" in str(s["outcome"]).lower()]
        if r.get("status") != "done" or fails:
            out.append(f"- {r.get('id')}: status={r.get('status')} "
                       f"req={r.get('request','')[:90]!r} "
                       f"failed_steps={len(fails)}"
                       + (f" first={fails[0]['outcome'][:120]!r}" if fails else ""))
    return out


def main() -> None:
    blocks: list[str] = []
    for label, data in _data_roots():
        traces, sessions, tasks = _traces(data), _sessions(data), _tasks(data)
        if not (traces or sessions or tasks):
            continue
        part: list[str] = []
        if label:
            part.append(f"## user {label}\n")
        if traces:
            part.append("### Performance signals (traces)\n" + "\n".join(traces) + "\n")
        if sessions:
            part.append("### Chat friction & owner corrections\n" + "\n".join(sessions) + "\n")
        if tasks:
            part.append("### Task-run failures\n" + "\n".join(tasks) + "\n")
        blocks.append("\n".join(part))
    if not blocks:
        return  # empty output → the job skips the Opus call
    print(f"# Self-improvement evidence — last {DAYS} days ({datetime.now():%Y-%m-%d})\n")
    print("\n".join(blocks))


if __name__ == "__main__":
    main()
