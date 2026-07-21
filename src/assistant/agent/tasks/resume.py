"""M4 — resume sync with approval gate.

The resume lives in its own git repo (``settings.resume_dir``), typically a
clone of an Overleaf project via the git bridge (``settings.resume_remote_url``).
The agent edits and commits **locally**; nothing reaches Overleaf until the
owner runs ``assistant approve-resume`` — the resume is outward-facing, so it
is never auto-pushed.
"""

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import yaml

from assistant.platform.config import Settings
from assistant.platform.llm import LLM
from assistant.agent.writing import RESUME_VOICE_RULES

_RELEVANCE_SYSTEM = """Decide whether today's profile changes are resume-worthy (new project
milestone, publication, promotion-level skill evidence — not routine activity bumps).
Respond with ONLY JSON: {"relevant": true|false, "reason": "<one sentence>"}"""

_EDIT_SYSTEM = """You update your owner's LaTeX resume from their evidence-backed profile.

Respond with ONLY JSON:
{"edits": [{"search": "<exact substring of the tex file>", "replace": "<replacement>"}],
 "summary": "<one sentence describing the update>"}

Rules:
- Only state facts present in the profile (every profile entry carries evidence). Never
  fabricate, inflate, or reword existing claims to sound better.
- Preserve the document's LaTeX style, formatting, and voice.
- Prefer few, small edits. Each "search" string must occur exactly once in the file.
- An empty edits list is the right answer if nothing genuinely belongs on the resume.
- Any NEW bullet you write follows the resume-voice rules below.

""" + RESUME_VOICE_RULES


def find_main_tex(repo: Path) -> Path | None:
    """Locate the resume's main .tex in `repo`: the largest file containing
    `\\documentclass` (the entry point among any includes), or None if there is
    no such file."""
    candidates = [
        p for p in sorted(repo.rglob("*.tex"))
        if "\\documentclass" in p.read_text(errors="ignore")
    ]
    return max(candidates, key=lambda p: p.stat().st_size, default=None)


def apply_edits(text: str, edits: list[dict]) -> tuple[str, list[dict], list[dict]]:
    """Exact-match search/replace; an edit whose search is absent or ambiguous is
    rejected rather than fuzzily applied — a resume is no place for guesses."""
    applied, rejected = [], []
    for edit in edits:
        search, replace = edit.get("search", ""), edit.get("replace", "")
        if not search or text.count(search) != 1:
            rejected.append({**edit, "reason": "search not found exactly once"})
            continue
        text = text.replace(search, replace, 1)
        applied.append(edit)
    return text, applied, rejected


def compile_check(repo: Path, tex: Path) -> tuple[bool | None, str]:
    """latexmk/pdflatex gate; returns (None, …) when no LaTeX toolchain exists."""
    for cmd in (["latexmk", "-pdf", "-interaction=nonstopmode"],
                ["pdflatex", "-interaction=nonstopmode"]):
        if shutil.which(cmd[0]):
            proc = subprocess.run(cmd + [tex.name], cwd=tex.parent,
                                  capture_output=True, text=True, timeout=300)
            return proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return None, "no LaTeX toolchain installed — compile gate skipped"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in `repo`, capturing output; `check` toggles raising on
    non-zero exit."""
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=check)


def _pending_file(settings: Settings) -> Path:
    """Path to the JSON record of a resume update awaiting owner approval."""
    return settings.data_dir / "resume_pending.json"


def sync_resume(llm: LLM, settings: Settings, profile: dict, profile_diff: str) -> dict:
    """Propose and locally commit a resume edit from today's profile diff,
    returning a status dict (never raising into the pipeline).

    The gate chain, most-declining first: no repo / no main .tex →
    not_configured; empty diff or not resume-worthy per the relevance LLM →
    no_change; edits that don't apply exactly-once → no_change with the rejected
    list; edits that break `compile_check` → rolled back, failed. A successful
    edit is committed locally and recorded as pending_approval — the resume is
    outward-facing, so it is never auto-pushed (that is `approve_resume`)."""
    repo = settings.resume_dir
    if not (repo / ".git").exists():
        return {"status": "not_configured",
                "note": "no resume repo — run `assistant resume-init` (see README)"}
    tex = find_main_tex(repo)
    if tex is None:
        return {"status": "not_configured", "note": f"no \\documentclass .tex found in {repo}"}
    if not profile_diff.strip():
        return {"status": "no_change", "note": "profile unchanged today"}

    relevance = llm.complete_json(
        f"## Today's profile diff\n{profile_diff[:4000]}",
        system=_RELEVANCE_SYSTEM, max_tokens=1000,
    )
    if not (isinstance(relevance, dict) and relevance.get("relevant")):
        return {"status": "no_change",
                "note": f"not resume-worthy: {relevance.get('reason', '?') if isinstance(relevance, dict) else '?'}"}

    text = tex.read_text()
    result = llm.complete_json(
        f"## Profile (source of truth)\n```yaml\n"
        f"{yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)[:8000]}\n```\n\n"
        f"## Today's profile diff\n{profile_diff[:4000]}\n\n"
        f"## Current resume ({tex.name})\n```latex\n{text}\n```",
        system=_EDIT_SYSTEM, max_tokens=8000, role="pipeline",
    )
    edits = result.get("edits", []) if isinstance(result, dict) else []
    new_text, applied, rejected = apply_edits(text, edits)
    if not applied:
        return {"status": "no_change", "note": "no applicable edits", "rejected": rejected}

    tex.write_text(new_text)
    ok, compile_log = compile_check(repo, tex)
    if ok is False:  # a resume that doesn't compile is a failed edit — roll back
        tex.write_text(text)
        return {"status": "failed", "note": "edit broke LaTeX compile — rolled back",
                "compile_log": compile_log[-1000:]}

    today = date.today().isoformat()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"agent: resume update {today}")
    diff = _git(repo, "show", "--stat", "-p", "HEAD", check=False).stdout

    pending = {
        "date": today,
        "summary": result.get("summary", ""),
        "commit": _git(repo, "rev-parse", "HEAD").stdout.strip(),
        "compile": "ok" if ok else "skipped (no toolchain)",
        "diff": diff[:6000],
    }
    _pending_file(settings).write_text(json.dumps(pending, indent=2, ensure_ascii=False))
    return {"status": "pending_approval", **pending, "rejected": rejected}


def approve_resume(settings: Settings) -> int:
    """Owner-invoked: push the locally committed resume update to the remote
    (Overleaf git bridge or GitHub), pulling remote edits first."""
    pending_file = _pending_file(settings)
    if not pending_file.exists():
        print("nothing pending approval")
        return 1
    repo = settings.resume_dir
    if settings.resume_remote_url:
        remotes = _git(repo, "remote", check=False).stdout.split()
        if "origin" not in remotes:
            _git(repo, "remote", "add", "origin", settings.resume_remote_url)
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    pull = _git(repo, "pull", "--rebase", "origin", branch, check=False)
    if pull.returncode != 0:
        _git(repo, "rebase", "--abort", check=False)
        print(f"remote has conflicting edits — resolve manually in {repo}:\n{pull.stderr[-800:]}")
        return 1
    push = _git(repo, "push", "origin", branch, check=False)
    if push.returncode != 0:
        print(f"push failed:\n{push.stderr[-800:]}")
        return 1
    pending = json.loads(pending_file.read_text())
    pending_file.unlink()
    print(f"resume update pushed ({pending.get('summary', '')})")
    return 0
