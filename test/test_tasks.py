import json
from datetime import date, timedelta

import pytest

from assistant.platform.llm import _parse_json
from assistant.agent.profile_store import ProfileStore
from assistant.agent.tasks.curate import curate
from assistant.agent.tasks.github_digest import build_digest


class BrokenLLM:
    """Simulates total LLM failure — the digest must fall back deterministically."""

    def complete_json(self, *a, **k):
        raise RuntimeError("llm down")


def _digest_prompt_ids(prompt):
    payload = prompt.split("## Notifications to triage\n", 1)[1]
    return [str(json.loads(line)["id"]) for line in payload.splitlines()]


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
    assert digest["llm_requested"] == 2
    assert digest["fallback_count"] == 2
    assert digest["degraded"] is True
    assert digest["fallback_reason_code"] == "llm_error"


def test_digest_empty():
    digest = build_digest(BrokenLLM(), {}, [], [])
    assert digest["total"] == 0
    assert digest["llm_requested"] == 0
    assert digest["llm_triaged"] == 0
    assert digest["fallback_count"] == 0
    assert digest["degraded"] is False
    assert digest["fallback_reason_code"] == "none"


def test_digest_partial_response_accounts_only_missing_ids():
    class PartialLLM:
        def complete_json(self, *a, **k):
            return [{"id": "1", "priority": "yellow", "summary": "LLM summary"}]

    notifications = [
        {"id": "1", "repo": "o/r", "reason": "mention", "type": "Issue",
         "title": "Mentioned", "updated_at": "t", "url": "u1"},
        {"id": "2", "repo": "o/r", "reason": "subscribed", "type": "Issue",
         "title": "FYI", "updated_at": "t", "url": "u2"},
    ]
    digest = build_digest(PartialLLM(), {}, notifications, [])

    assert digest["llm_requested"] == 2
    assert digest["llm_triaged"] == 1
    assert digest["fallback_count"] == 1
    assert digest["degraded"] is True
    assert digest["fallback_reason_code"] == "partial_response"
    assert digest["sections"]["yellow"][0]["summary"] == "LLM summary"
    assert digest["sections"]["white"][0]["summary"] == "[subscribed] FYI"


def test_digest_missing_summary_is_malformed_fallback():
    class MissingSummaryLLM:
        def complete_json(self, *a, **k):
            return [{"id": "1", "priority": "red", "summary": ""}]

    notifications = [
        {"id": "1", "repo": "o/r", "reason": "mention", "type": "Issue",
         "title": "Mentioned", "updated_at": "t", "url": "u1"},
    ]
    digest = build_digest(MissingSummaryLLM(), {}, notifications, [])
    assert digest["llm_triaged"] == 0 and digest["fallback_count"] == 1
    assert digest["degraded"] is True
    assert digest["fallback_reason_code"] == "malformed_response"
    assert digest["sections"]["red"][0]["summary"] == "[mention] Mentioned"


@pytest.mark.parametrize("count,fallback,reason", [
    (60, 0, "none"),
    (61, 1, "overflow"),
])
def test_digest_cap_batches_and_overflow_accounting(count, fallback, reason):
    class CompleteLLM:
        def __init__(self):
            self.calls = []

        def complete_json(self, prompt, **kwargs):
            ids = _digest_prompt_ids(prompt)
            self.calls.append((ids, kwargs))
            # Response order must not become digest order.
            return [{"id": item_id, "priority": "white", "summary": f"s{item_id}"}
                    for item_id in reversed(ids)]

    notifications = [
        {"id": str(i), "repo": "o/r", "reason": "subscribed", "type": "Issue",
         "title": f"n{i}", "updated_at": "t", "url": f"u{i}"}
        for i in range(count)
    ]
    llm = CompleteLLM()
    digest = build_digest(llm, {}, notifications, [])
    assert digest["llm_requested"] == 60 and digest["llm_triaged"] == 60
    assert digest["overflow"] == fallback and digest["fallback_count"] == fallback
    assert digest["degraded"] is bool(fallback)
    assert digest["fallback_reason_code"] == reason
    assert [len(ids) for ids, _ in llm.calls] == [15, 15, 15, 15]
    assert [item_id for ids, _ in llm.calls for item_id in ids] == [
        str(i) for i in range(60)
    ]
    assert all(kwargs["max_tokens"] == 8000 and kwargs["mixture"] is False
               for _, kwargs in llm.calls)
    assert [item["id"] for item in digest["sections"]["white"]] == [
        str(i) for i in range(count)
    ]


def test_digest_batch_failure_and_partial_response_fallback_only_their_ids():
    class BatchLLM:
        def complete_json(self, prompt, **kwargs):
            ids = _digest_prompt_ids(prompt)
            if ids[0] == "15":
                raise RuntimeError("only this batch failed")
            if ids[0] == "30":
                ids = ids[:-1]
            return [{"id": item_id, "priority": "white", "summary": f"ok-{item_id}"}
                    for item_id in ids]

    notifications = [
        {"id": str(i), "repo": "o/r", "reason": "subscribed", "type": "Issue",
         "title": f"n{i}", "updated_at": "t", "url": f"u{i}"}
        for i in range(45)
    ]
    digest = build_digest(BatchLLM(), {}, notifications, [])

    assert digest["llm_requested"] == 45
    assert digest["llm_triaged"] == 29
    assert digest["fallback_count"] == 16
    assert digest["fallback_reason_code"] == "llm_error"
    by_id = {item["id"]: item for item in digest["sections"]["white"]}
    assert by_id["14"]["summary"] == "ok-14"
    assert by_id["15"]["summary"] == "[subscribed] n15"
    assert by_id["30"]["summary"] == "ok-30"
    assert by_id["44"]["summary"] == "[subscribed] n44"


def test_digest_duplicate_rows_first_valid_wins_without_count_inflation():
    class DuplicateLLM:
        def complete_json(self, prompt, **kwargs):
            first, second = _digest_prompt_ids(prompt)
            return [
                {"id": first, "priority": "white", "summary": "first"},
                {"id": first, "priority": "red", "summary": "duplicate"},
                {"id": second, "priority": "white", "summary": "second"},
            ]

    notifications = [
        {"id": str(i), "repo": "o/r", "reason": "subscribed", "type": "Issue",
         "title": f"n{i}", "updated_at": "t", "url": f"u{i}"}
        for i in range(2)
    ]
    digest = build_digest(DuplicateLLM(), {}, notifications, [])

    assert digest["llm_triaged"] == 2
    assert digest["fallback_count"] == 0
    assert digest["fallback_reason_code"] == "none"
    assert [(item["id"], item["summary"])
            for item in digest["sections"]["white"]] == [
        ("0", "first"), ("1", "second"),
    ]


def test_digest_dedupes_input_ids_within_batch_and_keeps_newest_first():
    class CompleteLLM:
        def __init__(self):
            self.prompt_ids = []

        def complete_json(self, prompt, **kwargs):
            self.prompt_ids.extend(_digest_prompt_ids(prompt))
            return [
                {"id": item_id, "priority": "white", "summary": f"ok-{item_id}"}
                for item_id in self.prompt_ids
            ]

    notifications = [
        {"id": "same", "repo": "o/r", "reason": "subscribed", "type": "Issue",
         "title": "newest", "updated_at": "new", "url": "new-url"},
        {"id": "same", "repo": "o/r", "reason": "subscribed", "type": "Issue",
         "title": "older duplicate", "updated_at": "old", "url": "old-url"},
        {"id": "other", "repo": "o/r", "reason": "subscribed", "type": "Issue",
         "title": "other", "updated_at": "new", "url": "other-url"},
    ]
    llm = CompleteLLM()
    digest = build_digest(llm, {}, notifications, [])

    assert llm.prompt_ids == ["same", "other"]
    assert digest["total"] == digest["llm_requested"] == digest["llm_triaged"] == 2
    assert digest["fallback_count"] == digest["overflow"] == 0
    rendered = digest["sections"]["white"]
    assert [item["id"] for item in rendered] == ["same", "other"]
    assert rendered[0]["title"] == "newest" and rendered[0]["url"] == "new-url"


def test_digest_dedupes_across_raw_batch_boundary_before_unique_cap():
    """A duplicate at raw position 15 must not shift a unique item past 60."""
    class CompleteLLM:
        def __init__(self):
            self.calls = []

        def complete_json(self, prompt, **kwargs):
            ids = _digest_prompt_ids(prompt)
            self.calls.append(ids)
            return [{"id": item_id, "priority": "white", "summary": f"ok-{item_id}"}
                    for item_id in ids]

    def notification(item_id, title=None):
        return {"id": str(item_id), "repo": "o/r", "reason": "subscribed",
                "type": "Issue", "title": title or f"n{item_id}",
                "updated_at": "t", "url": f"u{item_id}"}

    # Unique IDs 0..14 fill the first batch. An older duplicate of 0 sits at
    # the raw batch boundary, followed by 15..60: 61 unique notifications.
    notifications = ([notification(i) for i in range(15)]
                     + [notification(0, "older duplicate")]
                     + [notification(i) for i in range(15, 61)])
    llm = CompleteLLM()
    digest = build_digest(llm, {}, notifications, [])

    assert [len(ids) for ids in llm.calls] == [15, 15, 15, 15]
    assert [item_id for ids in llm.calls for item_id in ids] == \
        [str(i) for i in range(60)]
    assert digest["total"] == 61
    assert digest["llm_requested"] == digest["llm_triaged"] == 60
    assert digest["overflow"] == digest["fallback_count"] == 1
    rendered = digest["sections"]["white"]
    assert [item["id"] for item in rendered] == [str(i) for i in range(61)]
    assert rendered[0]["title"] == "n0"


def test_digest_parseable_malformed_response_has_stable_reason():
    class MalformedLLM:
        def complete_json(self, *a, **k):
            return [None]

    notifications = [
        {"id": "1", "repo": "o/r", "reason": "mention", "type": "Issue",
         "title": "Mentioned", "updated_at": "t", "url": "u1"},
    ]
    digest = build_digest(MalformedLLM(), {}, notifications, [])
    assert digest["fallback_count"] == 1
    assert digest["fallback_reason_code"] == "malformed_response"


def test_digest_nested_malformed_priority_falls_back():
    class MalformedLLM:
        def complete_json(self, *a, **k):
            return [{"id": "1", "priority": [], "summary": "looks valid"}]

    notifications = [
        {"id": "1", "repo": "o/r", "reason": "mention", "type": "Issue",
         "title": "Mentioned", "updated_at": "t", "url": "u1"},
    ]
    digest = build_digest(MalformedLLM(), {}, notifications, [])
    assert digest["fallback_count"] == 1
    assert digest["fallback_reason_code"] == "malformed_response"


@pytest.mark.parametrize("field,value", [("action", ["bad"]),
                                           ("todo", {"bad": True})])
def test_digest_nested_optional_text_falls_back_safely(field, value):
    class MalformedLLM:
        def complete_json(self, *a, **k):
            return [{"id": "1", "priority": "yellow", "summary": "ok",
                     field: value}]

    notifications = [
        {"id": "1", "repo": "o/r", "reason": "mention", "type": "Issue",
         "title": "Mentioned", "updated_at": "t", "url": "u1"},
    ]
    digest = build_digest(MalformedLLM(), {}, notifications, [])
    rendered = digest["sections"]["red"][0]
    assert digest["llm_triaged"] == 0 and digest["fallback_count"] == 1
    assert digest["fallback_reason_code"] == "malformed_response"
    assert rendered["action"] is None and rendered["todo"] is None


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


def test_seen_key_ignores_updated_at_but_tracks_reason():
    """The seen key must survive a new comment (which bumps `updated_at`) or the
    same thread resurfaces as 🔴 every day — 84 of 165 threads repeated over
    2026-07-20→27, one on seven consecutive days. A changed `reason` is a
    genuine escalation and does re-surface."""
    from assistant.agent.orchestrator import seen_key

    monday = {"id": "24782330193", "reason": "mention",
              "updated_at": "2026-07-26T22:34:23Z"}
    commented = {**monday, "updated_at": "2026-07-27T09:00:00Z"}
    escalated = {**commented, "reason": "review_requested"}

    assert seen_key(monday) == seen_key(commented)   # suppressed tomorrow
    assert seen_key(escalated) != seen_key(monday)   # escalation re-surfaces
    # digest section items carry the same fields, so marking seen stays in lockstep
    assert seen_key({**monday, "summary": "…", "todo": "…"}) == seen_key(monday)


def test_stale_runs_are_not_resumed():
    """A run stuck at a phase was re-entered forever: one tenant's 07-17 run was
    resumed at `deliver` on every scheduled run for days, failing each time."""
    from datetime import date

    from assistant.agent.orchestrator import _resumable

    today = date(2026, 7, 27)
    assert _resumable("run-20260727-070037", today)      # today
    assert _resumable("run-20260725-070039", today)      # within the window
    assert not _resumable("run-20260717-070103", today)  # the stuck one
    assert _resumable("custom-run-id", today)            # unknown format → keep work


def test_run_starts_fresh_when_the_saved_run_is_stale(settings, monkeypatch):
    """`run(resume=True)` on a stale state.json mints a new run id instead of
    re-entering the old one."""
    from assistant.agent import orchestrator
    from assistant.agent.state import persist_state

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    persist_state(settings.state_file, run_id="run-20260101-070000", phase="deliver")

    started = {}

    class FakeGraph:
        def stream(self, initial, stream_mode=None):
            started["run_id"] = initial["run_id"]
            started["phase"] = initial["phase"]
            yield {**initial, "phase": "done"}

    monkeypatch.setattr(orchestrator, "build_graph", lambda deps: FakeGraph())
    assert orchestrator.run(settings, resume=True) == 0
    assert started["run_id"] != "run-20260101-070000"
    assert started["phase"] == "collect"
