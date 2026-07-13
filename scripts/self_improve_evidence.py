"""Gather a self-improvement briefing from the last day's traces and history.

Reads run traces (`runs/*/trace.jsonl` — slow LLM calls, errors, truncations,
phase timings), chat sessions (friction: retries/rejections/corrections), and
agentic task records (failures/aborts), and prints a compact markdown brief to
stdout. The daily self-improve job feeds this to Opus 4.8.

Local-only: the brief is written next to the data dir and never committed —
only the resulting code changes reach git.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

DATA = Path.home() / ".personal-agent"
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 2
CUTOFF = datetime.now() - timedelta(days=DAYS)


def _traces() -> list[str]:
    """Slow LLM calls, truncations, errors, and per-phase timings from recent runs."""
    out: list[str] = []
    runs = sorted((DATA / "runs").glob("run-*"))
    for run in runs[-DAYS - 1:]:
        tf = run / "trace.jsonl"
        if not tf.exists():
            continue
        try:
            spans = [json.loads(l) for l in tf.read_text().splitlines() if l.strip()]
        except ValueError:
            continue
        slow, trunc = [], []
        for s in spans:
            if s.get("name") != "llm":
                continue
            a = s.get("attr", {})
            dur = (s.get("dur_ms") or 0) / 1000
            if dur > 45:
                slow.append(f"{a.get('model','?')} {dur:.0f}s "
                            f"in={a.get('prompt_tokens','?')} out={a.get('completion_tokens','?')}")
            if a.get("stop_reason") == "max_tokens":
                trunc.append(f"{a.get('model','?')} truncated at max_tokens")
        if slow or trunc:
            out.append(f"- **{run.name}**: "
                       + "; ".join(slow[:4] + trunc[:3]))
    return out


def _sessions() -> list[str]:
    """Chat exchanges showing friction (retries/rejections/errors) or owner corrections."""
    out: list[str] = []
    for p in sorted((DATA / "sessions").glob("*.json")):
        try:
            turns = json.loads(p.read_text()).get("turns", [])
        except ValueError:
            continue
        for t in turns:
            ts = t.get("ts", "")
            if ts and ts < CUTOFF.isoformat():
                continue
            owner, reply = str(t.get("owner", "")), str(t.get("assistant", ""))
            friction = any(s in reply for s in
                           ("(retry)", "NOT logged", "rejected", "couldn't",
                            "failed", "assistant error", "无法", "抱歉"))
            corr = any(s in owner for s in
                       ("不对", "不是", "错", "改成", "别再", "以后", "应该", "重新", "取消"))
            if friction or corr:
                tag = ("friction" if friction else "") + ("+correction" if corr else "")
                out.append(f"- [{tag}] owner: {owner[:140]!r}\n    agent: {reply[:200]!r}")
    return out[-25:]


def _tasks() -> list[str]:
    """Agentic task runs that aborted or hit failing steps."""
    out: list[str] = []
    for p in sorted((DATA / "tasks").glob("task-*.json")):
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
    traces, sessions, tasks = _traces(), _sessions(), _tasks()
    if not (traces or sessions or tasks):
        return  # empty output → the job skips the Opus call
    print(f"# Self-improvement evidence — last {DAYS} days ({datetime.now():%Y-%m-%d})\n")
    if traces:
        print("## Performance signals (traces)\n" + "\n".join(traces) + "\n")
    if sessions:
        print("## Chat friction & owner corrections\n" + "\n".join(sessions) + "\n")
    if tasks:
        print("## Task-run failures\n" + "\n".join(tasks) + "\n")


if __name__ == "__main__":
    main()
