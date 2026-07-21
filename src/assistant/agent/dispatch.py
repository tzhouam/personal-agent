"""Agent-side job handlers — the per-owner work the platform worker pool runs.

`build_dispatch()` returns the kind → handler map the platform `WorkerPool`
consumes (`platform.dispatch.Dispatch`). The handlers lazy-import the agent
subsystems they drive (pipeline, task runner, weekly passes), so importing this
module stays cheap and the *platform* never has to import any of them — the
composition root (`serve`, `cli`) is the only place the two layers meet.

Long kinds take `cancel_check=token.check` so cooperative cancellation (§6)
lands at their own checkpoints — phase boundaries in the pipeline, per turn in
the task loop — not just at job start.
"""

import os

from assistant.platform.dispatch import Dispatch


def _dispatch_run(settings, args, token):
    from assistant.agent.orchestrator import run
    token.check()
    # Default resume=True: a REQUEUED job (daemon restart mid-run) then continues
    # from its state.json checkpoint instead of restarting from collect. Safe for
    # fresh runs too — run() only resumes when the previous run is incomplete.
    run(settings, resume=bool(args.get("resume", True)), cancel_check=token.check)


def _dispatch_run_phase(settings, args, token):
    from assistant.cli.commands import cmd_run_phase
    token.check()
    cmd_run_phase(settings, str(args.get("phase", "")))


def _dispatch_task(settings, args, token):
    from assistant.agent.task_runner import run_task
    token.check()
    # force_resume: a queue retry legitimately resumes a record left `running`
    # by a dead worker (the queue marks failure only after the worker raised,
    # and the active-scoped dedupe key keeps at most one job per record)
    run_task(str(args.get("request", "")), settings, cancel_check=token.check,
             approved_task_id=args.get("approved_task_id"),
             force_resume=bool(args.get("approved_task_id")))


def _dispatch_evolve(settings, args, token):
    """Weekly per-user personal-lessons pass (§12b layer 1)."""
    from assistant.agent.tasks.evolve import evolve
    token.check()
    evolve(settings)


def _dispatch_global_evolve(settings, args, token):
    """Weekly cross-user shared-lessons pass (§12b layer 2). `settings` is the
    deployment ROOT (GLOBAL_UID job)."""
    from assistant.agent.tasks.global_evolve import global_evolve
    token.check()
    global_evolve(settings)


def _dispatch_self_improve(settings, args, token):
    """Weekly code/workflow self-improvement (§12b layer 3): runs the existing
    PR-only harness (`scripts/self-improve.sh` — worktree off origin/main,
    pytest gate, sensitive-path guard, never merges). An operator-level GLOBAL
    job — a subprocess here carries no forgeable user identity (the no-Popen
    rule is about *tenant* jobs). Nonzero exit raises so the queue retries/fails
    visibly."""
    import subprocess
    from pathlib import Path

    token.check()
    # this file: src/assistant/agent/dispatch.py — repo root is parents[3]
    # (agent → assistant → src → repo).
    script = Path(__file__).resolve().parents[3] / "scripts" / "self-improve.sh"
    env = dict(os.environ, SELF_IMPROVE_DAYS=str(args.get("days", 7)))
    proc = subprocess.run(["bash", str(script), "live"], env=env,
                          capture_output=True, text=True, timeout=3900)
    if proc.returncode != 0:
        raise RuntimeError(f"self-improve exited {proc.returncode}: "
                           f"{(proc.stdout or '')[-300:]}{(proc.stderr or '')[-300:]}")


def build_dispatch() -> Dispatch:
    """The kind → in-process handler map the platform `WorkerPool` runs."""
    return {
        "run": _dispatch_run,
        "run_phase": _dispatch_run_phase,
        "task": _dispatch_task,
        "evolve": _dispatch_evolve,
        "global_evolve": _dispatch_global_evolve,
        "self_improve": _dispatch_self_improve,
    }
