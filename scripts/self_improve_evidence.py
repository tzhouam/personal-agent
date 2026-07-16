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
import sys
from datetime import datetime, timedelta
from pathlib import Path

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 2
CUTOFF = datetime.now() - timedelta(days=DAYS)
PER_USER_LINE_CAP = 25   # keep the brief compact even with several users


def _data_roots() -> list[tuple[str, Path]]:
    """`[(label, data_dir)]` to scan: the root dir in single_user (label ''),
    each active user's dir in multi_tenant. Falls back to the plain root if the
    assistant package/registry is unavailable (never crash the harness)."""
    root = Path.home() / ".personal-agent"
    try:
        from assistant.config import Settings

        settings = Settings()
        root = Path(settings.data_dir)
        if settings.deployment_mode == "multi_tenant":
            from assistant.registry import UserRegistry

            return [(uid, root / "users" / uid)
                    for uid in UserRegistry(root).active()]
    except Exception:
        pass
    return [("", root)]


def _traces(data: Path) -> list[str]:
    """Slow LLM calls, truncations, and errors from recent runs."""
    out: list[str] = []
    runs_dir = data / "runs"
    if not runs_dir.exists():
        return out
    for run in sorted(runs_dir.glob("run-*"))[-DAYS - 1:]:
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
            out.append(f"- **{run.name}**: " + "; ".join(slow[:4] + trunc[:3]))
    return out


def _sessions(data: Path) -> list[str]:
    """Chat exchanges showing friction (retries/rejections/errors) or owner corrections."""
    out: list[str] = []
    sessions_dir = data / "sessions"
    if not sessions_dir.exists():
        return out
    for p in sorted(sessions_dir.glob("*.json")):
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
    return out[-PER_USER_LINE_CAP:]


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
