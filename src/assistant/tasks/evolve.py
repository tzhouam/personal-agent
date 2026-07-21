"""Self-evolution pass: distill behavior lessons from recent interactions.

Reads what actually happened lately — chat session turns (the 48h window the
session store retains), agentic task traces (`DATA_DIR/tasks/`), with special
attention to failure signals ("(retry)", "NOT logged", rejections, aborted
tasks) — and asks the LLM for at most 3 NEW durable behavior rules that would
have prevented the friction, gated against the lessons already learned.
Proposals land in the lessons store with `source: evolve` (git-audited,
retire-able); run weekly next to profile consolidation, or on demand via
`assistant evolve`.
"""

import json
import logging
from datetime import datetime, timedelta

from ..config import Settings
from ..lessons_store import LessonsStore
from ..llm import LLM

log = logging.getLogger("assistant")

_EVOLVE_SYSTEM = """You improve a personal assistant by studying its recent conversations and
task runs. Propose durable BEHAVIOR rules ("when X, do Y") that would have prevented observed
friction: failed/retried actions, misunderstandings the owner had to correct, repeated manual
steps, wrong defaults. Rules must be about HOW the assistant behaves — never facts about the
world, never one-off reminders, never anything contradicting an existing lesson.
Respond with ONLY JSON: {"lessons": [{"rule": "<one imperative sentence>",
"why": "<the observed evidence, quoted short>"}], "note": "<one line on what you reviewed>"}
At most 3 lessons; an empty list is the right answer when nothing recurring stands out."""


def evolve(settings: Settings, llm: LLM | None = None) -> dict:
    """Run one self-evolution pass; returns `{reviewed, proposed, learned}`
    where `learned` are the lessons actually stored (dedup/caps applied)."""
    llm = llm or LLM(settings)
    store = LessonsStore(settings.profile_dir)
    evidence = _gather_evidence(settings)
    if not evidence.strip():
        return {"reviewed": 0, "proposed": [], "learned": []}

    existing = "\n".join(f"- {l['rule']}" for l in store.active()) or "(none)"
    result = llm.complete_json(
        f"## Existing lessons (do not repeat or contradict)\n{existing}\n\n"
        f"## Recent interactions\n{evidence[:16000]}",
        system=_EVOLVE_SYSTEM, max_tokens=5000, role="evolve")
    proposed = (result.get("lessons") or []) if isinstance(result, dict) else []
    learned = []
    for item in proposed[:3]:
        if not isinstance(item, dict):
            continue
        lesson = store.learn(item.get("rule", ""), why=item.get("why", ""),
                             source="evolve")
        if lesson:
            learned.append(lesson)
    log.info("evolve: reviewed %d chars, %d proposed, %d learned",
             len(evidence), len(proposed), len(learned))
    return {"reviewed": len(evidence), "proposed": proposed, "learned": learned}


def _gather_evidence(settings: Settings) -> str:
    """Recent chat turns + task traces, failure signals annotated."""
    parts: list[str] = []
    sessions_dir = settings.data_dir / "sessions"
    if sessions_dir.exists():
        for path in sorted(sessions_dir.glob("*.json")):
            try:
                turns = json.loads(path.read_text()).get("turns", [])
            except ValueError:
                continue
            for t in turns[-10:]:
                owner = str(t.get("owner", ""))[:300]
                reply = str(t.get("assistant", ""))[:400]
                label = t.get("outcome")
                if label:  # structured per-turn label (chat/agent.py Stage 1+2)
                    friction = (label == "fail" or t.get("repaired")
                                or t.get("owner_verdict") == "dissatisfied")
                else:  # pre-label turns: legacy keyword heuristic
                    friction = any(
                        s in reply for s in ("(retry)", "NOT logged", "rejected",
                                             "couldn't", "failed"))
                marker = " [FRICTION]" if friction else ""
                parts.append(f"owner: {owner}\nassistant{marker}: {reply}")
    tasks_dir = settings.data_dir / "tasks"
    if tasks_dir.exists():
        cutoff = (datetime.now() - timedelta(days=7)).strftime("task-%Y%m%d")
        for path in sorted(tasks_dir.glob("task-*.json")):
            if path.stem < cutoff:
                continue
            try:
                record = json.loads(path.read_text())
            except ValueError:
                continue
            failed = [s for s in record.get("steps", [])
                      if s.get("outcome") and "fail" in str(s["outcome"]).lower()]
            parts.append(f"task {record.get('id')}: status={record.get('status')} "
                         f"request={record.get('request', '')[:120]} "
                         f"steps={len(record.get('steps', []))} "
                         f"failed_steps={len(failed)}"
                         + (f" first_failure={failed[0]['outcome'][:150]}" if failed else ""))
    return "\n---\n".join(parts)


def _trace_evidence(settings: Settings, days: int = 7) -> str:
    """Pipeline-trace signals for the evolve passes: one compact line per recent
    run — total wall, the slowest phases, LLM volume, and suspicious spans
    (>45s LLM calls, `max_tokens` truncations). Only full daily runs produce
    `trace.jsonl` (chat/task turns record to events.db/tasks instead), so this
    is the operations view of the agent, per user."""
    from .. import tracing

    runs_dir = settings.runs_dir
    if not runs_dir.exists():
        return ""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("run-%Y%m%d")
    lines: list[str] = []
    for run_dir in sorted(p for p in runs_dir.glob("run-*") if p.is_dir()):
        if run_dir.name[:len(cutoff)] < cutoff:
            continue
        spans = tracing.load_spans(run_dir / "trace.jsonl")
        if not spans:
            continue
        wall_s = (max(s.get("end", 0) for s in spans)
                  - min(s.get("start", 0) for s in spans))
        phases = sorted((s for s in spans if s.get("name") == "phase"),
                        key=lambda s: -s.get("dur_ms", 0))[:3]
        phase_txt = ", ".join(
            f"{s.get('attr', {}).get('phase', '?')}={s.get('dur_ms', 0) / 1000:.0f}s"
            for s in phases)
        llm_spans = [s for s in spans if s.get("name") == "llm"]
        tok_in = sum(s.get("attr", {}).get("prompt_tokens") or 0 for s in llm_spans)
        tok_out = sum(s.get("attr", {}).get("completion_tokens") or 0 for s in llm_spans)
        suspicious = []
        for s in llm_spans:
            attr = s.get("attr", {})
            if s.get("dur_ms", 0) > 45_000:
                suspicious.append(f"slow llm {s['dur_ms'] / 1000:.0f}s "
                                  f"({attr.get('model', '?')})")
            if attr.get("stop_reason") == "max_tokens":
                suspicious.append(f"truncated at max_tokens ({attr.get('model', '?')})")
        lines.append(f"run {run_dir.name}: wall={wall_s:.0f}s slowest[{phase_txt}] "
                     f"llm_calls={len(llm_spans)} tokens={tok_in}->{tok_out}"
                     + (f" SUSPECT[{'; '.join(suspicious[:4])}]" if suspicious else ""))
    return "\n".join(lines)
