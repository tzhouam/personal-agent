"""LangGraph pipeline that runs the daily assistant.

Wires the nine phases (collect → profile → resume → digest → todos → research →
website → deliver → curate) into a `StateGraph`, threading `AssistantState`
through each node. Exports `Deps` (shared resource bundle), `build_graph`
(assembles the compiled graph), and `run` (the single-run entry point with the
resume-checkpoint and single-instance lock logic). Contract: every node
degrades on failure — it logs, appends to `errors`, and advances rather than
crashing the run."""

import fcntl
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from langgraph.graph import END, START, StateGraph

from .collectors import REGISTRY
from .config import Settings
from .deliver.announce import announce_digest
from .deliver.email import render_html, send_email
from .events_store import EventsStore
from .llm import LLM
from . import tracing
from .metrics import EXTRACTORS, build_health, render_health_html
from .profile_store import ProfileStore
from .research.pipeline import run_research
from .state import AssistantState, load_state, persist_state
from .tasks.curate import curate
from .tasks.github_digest import build_digest
from .tasks.profile_update import update_profile
from .tasks.resume import sync_resume
from .tasks.todos import update_todos
from .todo_store import ReadingList, TodoStore
from .website import sync_website

log = logging.getLogger("assistant")

_PHASES = ["collect", "profile", "resume", "digest", "todos", "research",
           "website", "deliver", "curate"]


class Deps:
    """Bundle of long-lived resources shared by every node: the `Settings`, the
    LLM client, and the events/profile/todo/reading stores, plus this run's
    artifact directory. Constructed once per run and closed over by the node
    functions in `build_graph`."""

    def __init__(self, settings: Settings, run_id: str):
        """Open the shared stores and create `runs/<run_id>/` for artifacts.
        `run_id` scopes both the artifact directory and events recorded this
        run."""
        self.settings = settings
        self.llm = LLM(settings)
        self.events = EventsStore(settings.events_db)
        self.profile = ProfileStore(settings.profile_dir)
        self.todos = TodoStore(settings.profile_dir)
        self.reading = ReadingList(settings.profile_dir)
        self.run_dir = settings.runs_dir / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def save_artifact(self, name: str, data) -> None:
        """Write `data` as pretty JSON to `run_dir/name`, the durable record a
        later `--resume` rehydrates from."""
        (self.run_dir / name).write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def load_artifact(self, name: str):
        """Return the parsed JSON artifact `name` from this run's directory, or
        `None` if it was never written (e.g. a phase that didn't run)."""
        path = self.run_dir / name
        return json.loads(path.read_text()) if path.exists() else None


def build_graph(deps: Deps):
    """Assemble and compile the phase `StateGraph`, closing every node over
    `deps`. Nodes are wrapped by `_instrumented` for per-phase metrics; the
    conditional edge from START jumps to the `phase` recorded in state (for
    `--resume`) or to `collect` for a fresh run, then phases run in the fixed
    `_PHASES` order to END. Returns the compiled graph ready to `.invoke`."""
    settings = deps.settings

    def _advance(phase: str) -> None:
        """Checkpoint the next phase to re-enter on `--resume`. Called only
        after a phase completes, so an interrupted run resumes at the phase it
        was in, not the next one."""
        # phase names the node to re-enter on --resume; advanced only on completion
        persist_state(settings.state_file, phase=phase)

    def node_collect(state: AssistantState) -> dict:
        """Phase 1: run every registered collector for the lookback window and
        gather raw observations plus GitHub notifications.

        Each collector runs in a try/except so one bad source can't sink the
        run — failures are logged and appended to `errors`. Observations are
        persisted to events.db and to artifacts, then the profile phase is
        queued. Returns the observations, notifications, next phase, and any
        collector errors."""
        since = datetime.now(timezone.utc) - timedelta(hours=settings.lookback_hours)
        observations, notifications, errors = [], [], []
        for name, factory in REGISTRY.items():
            try:
                collector = factory(settings)
                collected = collector.collect(since)
                observations.extend(collected)
                log.info("collector %s: %d observations", name, len(collected))
                if hasattr(collector, "fetch_notifications"):
                    notifications.extend(collector.fetch_notifications(since))
            except Exception as exc:
                log.exception("collector %s failed", name)
                errors.append(f"collect/{name}: {exc}")
        deps.events.add_observations(state["run_id"], observations)
        deps.save_artifact("observations.json", observations)
        deps.save_artifact("notifications.json", notifications)
        _advance("profile")
        return {"observations": observations, "notifications": notifications,
                "phase": "profile", "errors": errors}

    def node_profile(state: AssistantState) -> dict:
        """Phase 2: fold new observations into the evidence-gated profile store.

        Delegates to `update_profile`, records how many patch ops were rejected
        as a metric, and returns the human-readable `profile_diff` plus the
        applied `profile_ops`. On any failure it degrades to an empty diff/ops
        and still advances to `resume`, keeping the run alive."""
        try:
            result = update_profile(deps.llm, deps.profile, state.get("observations", []))
            deps.save_artifact("profile_update.json", result)
            deps.events.record_metrics(state["run_id"], "profile",
                                       {"ops_rejected": len(result.get("rejected_ops", []))})
            _advance("resume")
            return {"profile_diff": result["profile_diff"],
                    "profile_ops": result["profile_ops"], "phase": "resume"}
        except Exception as exc:
            log.exception("profile update failed")
            _advance("resume")
            return {"profile_diff": "", "profile_ops": [], "phase": "resume",
                    "errors": [f"profile: {exc}"]}

    def node_resume(state: AssistantState) -> dict:
        """Phase 3: sync the CV/resume to the git remote when the profile changed.

        Passes the current profile and this run's `profile_diff` to
        `sync_resume`; a `status == "failed"` result (or a raised exception) is
        turned into an `errors` entry rather than aborting. Returns the resume
        result and advances to `digest`."""
        try:
            result = sync_resume(deps.llm, settings, deps.profile.load(),
                                 state.get("profile_diff", ""))
            errors = []
            if result.get("status") == "failed":
                errors = [f"resume: {result.get('note')}"]
            log.info("resume sync: %s", result.get("status"))
        except Exception as exc:
            log.exception("resume sync failed")
            result, errors = {"status": "failed", "note": str(exc)}, [f"resume: {exc}"]
        deps.save_artifact("resume.json", result)
        _advance("digest")
        return {"resume": result, "phase": "digest", "errors": errors}

    def node_digest(state: AssistantState) -> dict:
        """Phase 4: build the ranked GitHub-notification digest from unseen items.

        Notifications are keyed by `gh-notif-<id>-<updated_at>` and filtered
        against the seen-store so only fresh ones reach the LLM ranker
        (`build_digest`, sorting into red/yellow/white). Records the count of
        already-seen items suppressed. On failure it degrades to an empty
        digest. Returns the digest and advances to `todos`."""
        notifications = state.get("notifications", [])
        unseen_ids = set(deps.events.filter_unseen(
            [f"gh-notif-{n['id']}-{n.get('updated_at', '')}" for n in notifications]
        ))
        fresh = [n for n in notifications
                 if f"gh-notif-{n['id']}-{n.get('updated_at', '')}" in unseen_ids]
        try:
            digest = build_digest(deps.llm, deps.profile.load(), fresh,
                                  state.get("observations", []))
            errors = []
        except Exception as exc:
            log.exception("digest failed")
            digest = {"sections": {"red": [], "yellow": [], "white": []}, "total": 0}
            errors = [f"digest: {exc}"]
        digest["suppressed_seen"] = len(notifications) - len(fresh)
        deps.save_artifact("digest.json", digest)
        _advance("todos")
        return {"digest": digest, "phase": "todos", "errors": errors}

    def node_todos(state: AssistantState) -> dict:
        """Phase 5: reconcile the todo list from digest, resume, and website marks.

        First applies done/unrelated clicks queued on the private website (so
        those marks feed this run's counts), then calls `update_todos` to add,
        auto-close, and re-rank todos, using GitHub for auto-close checks when a
        token is configured. Both steps are independently guarded so either can
        fail without stopping the run. Returns the todo summary and advances to
        `research`."""
        try:  # website clicks first, so done-marks feed the monitor + quota
            from .marks import collect_marks

            marks = collect_marks(settings, deps.events)
            if marks["applied"]:
                deps.events.record_metrics(state["run_id"], "todos",
                                           {"website_marks": marks["applied"]})
        except Exception:
            log.exception("website marks collection failed")
        try:
            from .collectors.github import GitHubCollector

            github = GitHubCollector(settings) if settings.github_token else None
            todos = update_todos(deps.todos, state.get("digest", {}),
                                 state.get("resume", {}), github=github, llm=deps.llm)
            log.info("todos: %d open (%d added, %d auto-closed)", todos["open_count"],
                     len(todos["added"]), len(todos.get("closed", [])))
            errors = []
        except Exception as exc:
            log.exception("todo update failed")
            todos, errors = ({"added": [], "closed": [], "open": [], "open_count": 0},
                             [f"todos: {exc}"])
        deps.save_artifact("todos.json", todos)
        _advance("research")
        return {"todos": todos, "phase": "research", "errors": errors}

    def node_research(state: AssistantState) -> dict:
        """Phase 6: run the research pipeline and grow the reading list.

        `run_research` returns ranked papers plus industry/Chinese feed items;
        each paper is upserted into the persistent reading list (deduped by
        `seen_id`) so the backlog carries across runs. On failure it degrades to
        empty results. Returns the research payload and the current open reading
        items, and advances to `website`."""
        try:
            research = run_research(deps.llm, deps.profile.load(), deps.events, settings)
            errors = []
        except Exception as exc:
            log.exception("research pipeline failed")
            research = {"papers": [], "industry": [], "chinese": [],
                        "source_health": {}, "seen_ids": []}
            errors = [f"research: {exc}"]
        for paper in research.get("papers", []):  # papers accumulate as the reading list
            deps.reading.upsert(paper["seen_id"], title=paper["title"], url=paper["url"],
                                source="arxiv", why=paper.get("why", ""))
        reading = deps.reading.open_items()
        deps.save_artifact("research.json", research)
        deps.save_artifact("reading.json", reading)
        _advance("website")
        return {"research": research, "reading": reading, "phase": "website", "errors": errors}

    def node_website(state: AssistantState) -> dict:
        """Phase 7: deterministically render and publish the personal website.

        Feeds the profile, open todos, and open reading items to `sync_website`
        (no LLM — the render is deterministic). On failure it degrades to a
        `status: failed` record. Returns the website result and advances to
        `deliver`."""
        try:
            website = sync_website(settings, deps.profile.load(),
                                   (state.get("todos") or {}).get("open", []),
                                   reading=state.get("reading") or deps.reading.open_items())
            log.info("website: %s %s", website.get("status"), website.get("pr_url", ""))
            errors = []
        except Exception as exc:
            log.exception("website sync failed")
            website, errors = {"status": "failed", "note": str(exc)}, [f"website: {exc}"]
        deps.save_artifact("website.json", website)
        _advance("deliver")
        return {"website": website, "phase": "deliver", "errors": errors}

    def node_deliver(state: AssistantState) -> dict:
        """Phase 8: assemble the HTML digest, email it, and mark items seen.

        Collects headline stats (and any accumulated `errors`) plus a best-effort
        health section, renders the full digest HTML, and always writes it to
        `digest.html`. In `dry_run` mode it stops there. Otherwise it sends the
        email (Resend then SMTP), optionally announces via WeChat, and only
        then marks the delivered notifications and research ids seen — so a
        failed send stays on `deliver` for `--resume` to retry and nothing is
        marked seen prematurely. Returns `email_sent`, the digest path, and the
        next phase."""
        run_date = datetime.now().strftime("%Y-%m-%d")
        digest = state.get("digest", {})
        research = state.get("research", {})
        resume = state.get("resume", {})
        todos = state.get("todos", {})
        reading = state.get("reading", [])
        website = state.get("website", {})
        stats = {
            "run": state["run_id"],
            "observations": len(state.get("observations", [])),
            "notifications": digest.get("total", 0),
            "seen-suppressed": digest.get("suppressed_seen", 0),
            "todos open": todos.get("open_count", 0),
            "reading backlog": len(reading),
            "profile ops": len(state.get("profile_ops", [])),
            "website": website.get("status", "?"),
        }
        if state.get("errors"):
            stats["errors"] = "; ".join(str(e) for e in state["errors"])[:300]
        try:
            health_html = render_health_html(
                build_health(deps.events, settings.profile_dir))
        except Exception:  # health is a nicety — never block the digest
            log.exception("health section failed")
            health_html = ""
        html_body = render_html(run_date, digest, research, resume, todos, reading,
                                website, state.get("profile_diff", ""),
                                state.get("profile_ops", []), stats,
                                health_html=health_html)
        digest_path = deps.run_dir / "digest.html"
        digest_path.write_text(html_body)

        if state.get("dry_run"):
            log.info("dry-run: digest written to %s, email not sent", digest_path)
            _advance("curate")
            return {"email_sent": False, "digest_path": str(digest_path), "phase": "curate"}
        try:
            transport = send_email(deps.settings,
                                   f"[assistant] Daily digest — {run_date}", html_body)
            log.info("digest emailed via %s", transport)
            note = announce_digest(settings, (
                f"Daily digest {run_date} delivered — "
                f"{digest.get('total', 0)} notifications, "
                f"{todos.get('open_count', 0)} todos open. "
                f"Full digest in your email."))
            if note != "disabled":
                log.info("wechat announce: %s", note)
            # only mark items seen once actually delivered
            deps.events.mark_seen(
                [f"gh-notif-{i['id']}-{i.get('updated_at', '')}"
                 for section in digest.get("sections", {}).values() for i in section]
                + research.get("seen_ids", []),
                context=f"digest {run_date}",
            )
            _advance("curate")
            return {"email_sent": True, "digest_path": str(digest_path), "phase": "curate"}
        except Exception as exc:
            log.exception("email delivery failed")
            # stay on deliver so --resume retries the send
            return {"email_sent": False, "digest_path": str(digest_path),
                    "phase": "deliver", "errors": [f"deliver: {exc}"]}

    def node_curate(state: AssistantState) -> dict:
        """Phase 9: post-delivery housekeeping — decay stale profile entries and
        prune old chat history.

        Skipped (returns empty) when delivery is still stuck on `deliver`, so a
        failed run isn't curated past. Runs the profile `curate` decay pass and,
        separately guarded, prunes chat-session turns older than
        `chat_history_max_age_hours` for context-window hygiene. Advances the
        checkpoint to `done`."""
        if state.get("phase") == "deliver":
            return {}  # delivery failed — don't curate past a stuck run
        try:
            curated = curate(deps.profile)
            if curated["decayed"]:
                log.info("curator: %d entries decayed to dormant", len(curated["decayed"]))
            errors = []
        except Exception as exc:
            log.exception("curator failed")
            curated, errors = {"decayed": []}, [f"curate: {exc}"]
        try:  # daily retention hygiene: chat turns kept ~30d, then pruned
            from .serve import SessionStore

            pruned = SessionStore(
                settings.data_dir,
                context_hours=settings.chat_history_max_age_hours,
                retention_days=settings.chat_history_retention_days).prune()
            if pruned["turns"] or pruned["files"]:
                log.info("chat sessions pruned: %d turns, %d files (>%dd old)",
                         pruned["turns"], pruned["files"],
                         settings.chat_history_retention_days)
            deps.events.record_metrics(state["run_id"], "curate",
                                       {"chat_turns_pruned": pruned["turns"]})
        except Exception:
            log.exception("session pruning failed")
        try:  # staged chat images (email/wechat/base64) expire with chat history
            cutoff = time.time() - settings.chat_history_max_age_hours * 3600
            media_dir = settings.data_dir / "media"
            stale = [p for p in media_dir.glob("*")
                     if p.is_file() and p.stat().st_mtime < cutoff] if media_dir.exists() else []
            for path in stale:
                path.unlink(missing_ok=True)
            if stale:
                log.info("media pruned: %d staged image(s) older than %dh",
                         len(stale), settings.chat_history_max_age_hours)
        except Exception:
            log.exception("media pruning failed")
        _advance("done")
        return {"curated": curated, "phase": "done", "errors": errors}

    def _instrumented(name, fn):
        """Record duration, error count, and the phase's headline numbers
        (metrics.EXTRACTORS) into events.db — doc/PIPELINE_METRICS.md."""
        def wrapped(state: AssistantState) -> dict:
            """Run `fn` inside a phase span, then record its duration/error count
            and headline numbers to events.db (metrics never break the run)."""
            start = time.monotonic()
            with tracing.span("phase", phase=name):
                out = fn(state)
            try:
                values = {"duration_s": round(time.monotonic() - start, 2),
                          "errors": len(out.get("errors") or [])}
                values.update(EXTRACTORS.get(name, lambda o: {})(out))
                deps.events.record_metrics(state["run_id"], name, values)
            except Exception:  # metrics must never break the pipeline
                log.exception("metrics recording failed for %s", name)
            return out
        return wrapped

    nodes = {"collect": node_collect, "profile": node_profile, "resume": node_resume,
             "digest": node_digest, "todos": node_todos, "research": node_research,
             "website": node_website, "deliver": node_deliver, "curate": node_curate}
    graph = StateGraph(AssistantState)
    for phase in _PHASES:
        graph.add_node(phase, _instrumented(phase, nodes[phase]))
    graph.add_conditional_edges(
        START,
        lambda s: s.get("phase") if s.get("phase") in _PHASES else "collect",
        {p: p for p in _PHASES},
    )
    for a, b in zip(_PHASES, _PHASES[1:]):
        graph.add_edge(a, b)
    graph.add_edge(_PHASES[-1], END)
    return graph.compile()


def run(settings: Settings, dry_run: bool = False, resume: bool = False) -> int:
    """Single entry point for one pipeline run: cron, chat `trigger_run`, and
    manual CLI all funnel through here.

    Takes an exclusive non-blocking `run.lock` first so two runs can't interleave
    state/artifacts (returns 3 if another holds it). With `resume`, continues the
    last unfinished run's `run_id` at its checkpointed phase and rehydrates the
    artifacts each downstream phase needs; otherwise mints a fresh timestamped
    `run_id` starting at `collect`. `dry_run` writes the digest but skips email.
    Builds and invokes the graph, records the run's duration/error metrics, and
    always closes stores and releases the lock. Returns 0 when the run reached
    `done`, else 1 (or 3 for the lock conflict)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Canonical single-run guard: cron, chat trigger_run, and manual CLI runs
    # all pass through here; holding the lock for the whole run prevents two
    # pipelines from interleaving state.json / run artifacts.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = (settings.data_dir / "run.lock").open("w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("another run already holds %s — refusing to start", lock_fd.name)
        lock_fd.close()
        return 3

    prev = load_state(settings.state_file)

    if resume and prev and prev.get("phase") not in (None, "done") and prev.get("run_id"):
        run_id, start_phase = prev["run_id"], prev["phase"]
        log.info("resuming run %s at phase %s", run_id, start_phase)
    else:
        run_id = datetime.now().strftime("run-%Y%m%d-%H%M%S")
        start_phase = "collect"

    persist_state(settings.state_file, run_id=run_id, phase=start_phase)
    deps = Deps(settings, run_id)
    # Trace recorder: timed spans (phases + every LLM call) → runs/<id>/trace.jsonl
    tracing.init(run_id, deps.run_dir / "trace.jsonl")

    initial: AssistantState = {"run_id": run_id, "phase": start_phase,
                               "dry_run": dry_run, "errors": []}
    if start_phase != "collect":  # rehydrate artifacts from the interrupted run
        initial["observations"] = deps.load_artifact("observations.json") or []
        initial["notifications"] = deps.load_artifact("notifications.json") or []
        saved = deps.load_artifact("profile_update.json") or {}
        initial["profile_diff"] = saved.get("profile_diff", "")
        initial["profile_ops"] = saved.get("profile_ops", [])
        if start_phase in ("todos", "research", "website", "deliver", "curate"):
            initial["digest"] = deps.load_artifact("digest.json") or {}
            initial["resume"] = deps.load_artifact("resume.json") or {}
        if start_phase in ("research", "website", "deliver", "curate"):
            initial["todos"] = deps.load_artifact("todos.json") or {}
        if start_phase in ("website", "deliver", "curate"):
            initial["research"] = deps.load_artifact("research.json") or {}
            initial["reading"] = deps.load_artifact("reading.json") or []
        if start_phase in ("deliver", "curate"):
            initial["website"] = deps.load_artifact("website.json") or {}

    run_start = time.monotonic()
    try:
        final = build_graph(deps).invoke(initial)
        deps.events.record_metrics(run_id, "run", {
            "duration_s": round(time.monotonic() - run_start, 2),
            "errors": len(final.get("errors") or [])})
    finally:
        deps.events.close()
        lock_fd.close()  # releases the flock

    for err in final.get("errors", []):
        log.warning("run error: %s", err)
    if dry_run:
        print(f"dry-run complete — digest at {final.get('digest_path')}")
    return 0 if final.get("phase") == "done" else 1
