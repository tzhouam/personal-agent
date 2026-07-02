import json
import logging
from datetime import datetime, timedelta, timezone

from langgraph.graph import END, START, StateGraph

from .collectors import REGISTRY
from .config import Settings
from .deliver.email import render_html, send_email
from .events_store import EventsStore
from .llm import LLM
from .profile_store import ProfileStore
from .research.pipeline import run_research
from .state import AssistantState, load_state, persist_state
from .tasks.curate import curate
from .tasks.github_digest import build_digest
from .tasks.profile_update import update_profile
from .tasks.resume import sync_resume

log = logging.getLogger("assistant")

_PHASES = ["collect", "profile", "resume", "digest", "research", "deliver", "curate"]


class Deps:
    def __init__(self, settings: Settings, run_id: str):
        self.settings = settings
        self.llm = LLM(settings)
        self.events = EventsStore(settings.events_db)
        self.profile = ProfileStore(settings.profile_dir)
        self.run_dir = settings.runs_dir / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def save_artifact(self, name: str, data) -> None:
        (self.run_dir / name).write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def load_artifact(self, name: str):
        path = self.run_dir / name
        return json.loads(path.read_text()) if path.exists() else None


def build_graph(deps: Deps):
    settings = deps.settings

    def _advance(phase: str) -> None:
        # phase names the node to re-enter on --resume; advanced only on completion
        persist_state(settings.state_file, phase=phase)

    def node_collect(state: AssistantState) -> dict:
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
        try:
            result = update_profile(deps.llm, deps.profile, state.get("observations", []))
            deps.save_artifact("profile_update.json", result)
            _advance("resume")
            return {"profile_diff": result["profile_diff"],
                    "profile_ops": result["profile_ops"], "phase": "resume"}
        except Exception as exc:
            log.exception("profile update failed")
            _advance("resume")
            return {"profile_diff": "", "profile_ops": [], "phase": "resume",
                    "errors": [f"profile: {exc}"]}

    def node_resume(state: AssistantState) -> dict:
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
        _advance("research")
        return {"digest": digest, "phase": "research", "errors": errors}

    def node_research(state: AssistantState) -> dict:
        try:
            research = run_research(deps.llm, deps.profile.load(), deps.events, settings)
            errors = []
        except Exception as exc:
            log.exception("research pipeline failed")
            research = {"papers": [], "industry": [], "chinese": [],
                        "source_health": {}, "seen_ids": []}
            errors = [f"research: {exc}"]
        deps.save_artifact("research.json", research)
        _advance("deliver")
        return {"research": research, "phase": "deliver", "errors": errors}

    def node_deliver(state: AssistantState) -> dict:
        run_date = datetime.now().strftime("%Y-%m-%d")
        digest = state.get("digest", {})
        research = state.get("research", {})
        resume = state.get("resume", {})
        stats = {
            "run": state["run_id"],
            "observations": len(state.get("observations", [])),
            "notifications": digest.get("total", 0),
            "seen-suppressed": digest.get("suppressed_seen", 0),
            "papers": len(research.get("papers", [])),
            "profile ops": len(state.get("profile_ops", [])),
        }
        if state.get("errors"):
            stats["errors"] = "; ".join(str(e) for e in state["errors"])[:300]
        html_body = render_html(run_date, digest, research, resume,
                                state.get("profile_diff", ""),
                                state.get("profile_ops", []), stats)
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
        _advance("done")
        return {"curated": curated, "phase": "done", "errors": errors}

    nodes = {"collect": node_collect, "profile": node_profile, "resume": node_resume,
             "digest": node_digest, "research": node_research, "deliver": node_deliver,
             "curate": node_curate}
    graph = StateGraph(AssistantState)
    for phase in _PHASES:
        graph.add_node(phase, nodes[phase])
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    prev = load_state(settings.state_file)

    if resume and prev and prev.get("phase") not in (None, "done") and prev.get("run_id"):
        run_id, start_phase = prev["run_id"], prev["phase"]
        log.info("resuming run %s at phase %s", run_id, start_phase)
    else:
        run_id = datetime.now().strftime("run-%Y%m%d-%H%M%S")
        start_phase = "collect"

    persist_state(settings.state_file, run_id=run_id, phase=start_phase)
    deps = Deps(settings, run_id)

    initial: AssistantState = {"run_id": run_id, "phase": start_phase,
                               "dry_run": dry_run, "errors": []}
    if start_phase != "collect":  # rehydrate artifacts from the interrupted run
        initial["observations"] = deps.load_artifact("observations.json") or []
        initial["notifications"] = deps.load_artifact("notifications.json") or []
        saved = deps.load_artifact("profile_update.json") or {}
        initial["profile_diff"] = saved.get("profile_diff", "")
        initial["profile_ops"] = saved.get("profile_ops", [])
        if start_phase in ("deliver", "curate"):
            initial["digest"] = deps.load_artifact("digest.json") or {}
            initial["research"] = deps.load_artifact("research.json") or {}
            initial["resume"] = deps.load_artifact("resume.json") or {}

    try:
        final = build_graph(deps).invoke(initial)
    finally:
        deps.events.close()

    for err in final.get("errors", []):
        log.warning("run error: %s", err)
    if dry_run:
        print(f"dry-run complete — digest at {final.get('digest_path')}")
    return 0 if final.get("phase") == "done" else 1
