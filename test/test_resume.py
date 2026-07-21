import json
import subprocess

import pytest

from assistant.agent.tasks.resume import apply_edits, find_main_tex, sync_resume

TEX = r"""\documentclass{article}
\begin{document}
\section{Projects}
\item vllm-omni rebase automation
\end{document}
"""


class StubLLM:
    """Deterministic stand-in: relevance yes, then one edit."""

    def __init__(self, edits):
        self.responses = [
            {"relevant": True, "reason": "new milestone"},
            {"edits": edits, "summary": "added highlight"},
        ]

    def complete_json(self, *a, **k):
        return self.responses.pop(0)


def test_apply_edits_exact_and_ambiguous():
    text = "aaa X bbb X ccc Y"
    new, applied, rejected = apply_edits(
        text,
        [
            {"search": "Y", "replace": "Z"},        # unique → applied
            {"search": "X", "replace": "W"},        # appears twice → rejected
            {"search": "missing", "replace": "M"},  # absent → rejected
        ],
    )
    assert new == "aaa X bbb X ccc Z"
    assert len(applied) == 1 and len(rejected) == 2


def test_find_main_tex(tmp_path):
    (tmp_path / "notes.tex").write_text("no documentclass here")
    (tmp_path / "main.tex").write_text(TEX)
    assert find_main_tex(tmp_path).name == "main.tex"
    (tmp_path / "main.tex").unlink()
    (tmp_path / "notes.tex").unlink()
    assert find_main_tex(tmp_path) is None


def test_sync_not_configured(settings):
    result = sync_resume(StubLLM([]), settings, {}, "some diff")
    assert result["status"] == "not_configured"


@pytest.fixture
def resume_repo(settings):
    repo = settings.resume_dir
    repo.mkdir(parents=True)
    (repo / "main.tex").write_text(TEX)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return repo


def test_sync_skips_when_profile_unchanged(settings, resume_repo):
    result = sync_resume(StubLLM([]), settings, {}, "")
    assert result["status"] == "no_change"


def test_sync_commits_locally_and_marks_pending(settings, resume_repo, monkeypatch):
    # no LaTeX toolchain in CI → force the "skipped" path deterministically
    monkeypatch.setattr("assistant.agent.tasks.resume.compile_check", lambda r, t: (None, "skipped"))
    llm = StubLLM([{"search": r"\item vllm-omni rebase automation",
                    "replace": "\\item vllm-omni rebase automation (merged PR \\#4709)"}])
    result = sync_resume(llm, settings, {"projects": []}, "+ highlight: merged PR #4709")

    assert result["status"] == "pending_approval"
    assert "4709" in (resume_repo / "main.tex").read_text()
    # committed locally…
    log = subprocess.run(["git", "log", "--oneline"], cwd=resume_repo,
                         capture_output=True, text=True).stdout
    assert "agent: resume update" in log
    # …but only marked pending — nothing pushed (no remote exists at all)
    pending = json.loads((settings.data_dir / "resume_pending.json").read_text())
    assert pending["summary"] == "added highlight"


def test_sync_rolls_back_on_compile_failure(settings, resume_repo, monkeypatch):
    monkeypatch.setattr("assistant.agent.tasks.resume.compile_check", lambda r, t: (False, "err"))
    llm = StubLLM([{"search": r"\item vllm-omni rebase automation", "replace": "\\broken{"}])
    result = sync_resume(llm, settings, {}, "diff")
    assert result["status"] == "failed"
    assert (resume_repo / "main.tex").read_text() == TEX  # rolled back
