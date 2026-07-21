from datetime import date, timedelta

from assistant.platform.llm import _parse_json
from assistant.agent.profile_store import ProfileStore
from assistant.agent.tasks.curate import curate
from assistant.agent.tasks.github_digest import build_digest


class BrokenLLM:
    """Simulates total LLM failure — the digest must fall back deterministically."""

    def complete_json(self, *a, **k):
        raise RuntimeError("llm down")


def test_digest_deterministic_fallback():
    notifications = [
        {"id": "1", "repo": "o/r", "reason": "review_requested", "type": "PullRequest",
         "title": "Please review", "updated_at": "t", "url": "u"},
        {"id": "2", "repo": "o/r", "reason": "subscribed", "type": "Issue",
         "title": "Some issue", "updated_at": "t", "url": "u"},
    ]
    digest = build_digest(BrokenLLM(), {}, notifications, [])
    assert [i["id"] for i in digest["sections"]["red"]] == ["1"]
    assert [i["id"] for i in digest["sections"]["white"]] == ["2"]
    assert digest["total"] == 2 and digest["llm_triaged"] == 0


def test_digest_empty():
    digest = build_digest(BrokenLLM(), {}, [], [])
    assert digest["total"] == 0


def test_curator_decay(tmp_path):
    store = ProfileStore(tmp_path / "profile")
    old = (date.today() - timedelta(days=45)).isoformat()
    recent = date.today().isoformat()
    store.save(
        {
            "identity": {"name": "T", "github": "t", "emails": []},
            "skills": [
                {"name": "Old", "last_seen": old, "status": "active"},
                {"name": "Fresh", "last_seen": recent, "status": "active"},
            ],
            "projects": [{"name": "P", "last_seen": old, "status": "active"}],  # 60d window
        },
        "seed",
    )
    result = curate(store)
    assert result["decayed"] == ["skills: Old"]
    profile = store.load()
    assert profile["skills"][0]["status"] == "dormant"
    assert profile["skills"][1]["status"] == "active"
    assert profile["projects"][0]["status"] == "active"  # 45d < 60d project window


def test_ref_label():
    from assistant.agent.utils import ref_label

    assert ref_label("https://github.com/o/r/pull/4803") == "PR #4803"
    assert ref_label("https://github.com/o/r/issues/7") == "Issue #7"
    assert ref_label("https://github.com/o/r/issues/7", title="[RFC]: audio pipeline") == "RFC #7"
    assert ref_label("https://arxiv.org/abs/2501.1") == "Paper"
    assert ref_label("https://github.com/o/r/releases") == "Release"
    assert ref_label("https://example.com/x") is None
    assert ref_label(None) is None


def test_parse_json_variants():
    assert _parse_json('{"a": 1}') == {"a": 1}
    assert _parse_json('Here you go:\n```json\n[1, 2]\n```') == [1, 2]
    assert _parse_json('prefix {"a": {"b": 2}} suffix') == {"a": {"b": 2}}


def test_run_refuses_when_lock_held(settings):
    import fcntl

    from assistant.agent import orchestrator

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    holder = (settings.data_dir / "run.lock").open("w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert orchestrator.run(settings) == 3  # refuses before touching anything
    finally:
        holder.close()


def test_run_streams_phases_and_cancels_at_boundaries(settings, monkeypatch):
    """`run()` iterates the graph phase-by-phase (stream) so `cancel_check` can
    abort at a phase boundary (§6); a completed stream still returns 0."""
    import pytest

    from assistant.agent import orchestrator
    from assistant.platform.worker import Cancelled

    class FakeGraph:
        def __init__(self, phases):
            self.phases = phases

        def stream(self, initial, stream_mode=None):
            assert stream_mode == "values"
            for p in self.phases:
                yield {**initial, "phase": p}

    monkeypatch.setattr(orchestrator, "build_graph",
                        lambda deps: FakeGraph(["collect", "done"]))
    assert orchestrator.run(settings) == 0            # normal completion

    monkeypatch.setattr(orchestrator, "build_graph",
                        lambda deps: FakeGraph(["collect", "profile", "done"]))
    seen = {"n": 0}

    def check():
        seen["n"] += 1
        if seen["n"] > 2:                             # cancel after 2 phases
            raise Cancelled()

    with pytest.raises(Cancelled):
        orchestrator.run(settings, cancel_check=check)
    assert seen["n"] == 3                             # checked at each boundary
