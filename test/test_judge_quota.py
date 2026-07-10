"""Weekly profile judge (faithfulness/staleness/contradiction) and the
adaptive reading quota (doc/PIPELINE_METRICS.md §2 + §6)."""

from datetime import date, timedelta

from assistant.events_store import EventsStore
from assistant.metrics import build_health
from assistant.profile_store import ProfileStore
from assistant.research.pipeline import adaptive_paper_quota
from assistant.tasks import profile_consolidate
from assistant.tasks.profile_consolidate import consolidate_profile, judge_profile

TODAY = date.today()


def _seed_profile(settings):
    store = ProfileStore(settings.profile_dir)
    store.ensure_repo()
    store.save({"identity": {"name": "T"}, "skills": [], "interests": [],
                "projects": [{"name": "p", "highlights": ["did X"],
                              "evidence": ["https://x"], "status": "active"}],
                "education": [], "experience": []}, "seed")
    return store


class JudgeLLM:
    """No consolidation ops; one contradiction + one stale finding on audit."""

    def complete_json(self, prompt, system=None, **kw):
        if "audit" in (system or ""):
            assert "Profile (active entries)" in prompt
            return {"contradictions": [{"where": "p", "detail": "RFC #1 vs RFC #2"}],
                    "stale": [{"where": "p", "detail": "claim superseded"}],
                    "unsupported": [], "claims_checked": 7}
        return {"ops": [], "notes": ""}


def test_judge_records_metrics_and_emails_findings(settings, monkeypatch):
    sent = []
    monkeypatch.setattr(profile_consolidate, "send_email",
                        lambda s, subject, body: sent.append(body) or "smtp")
    store = _seed_profile(settings)
    result = consolidate_profile(JudgeLLM(), store, settings)

    judge = result["judge"]
    assert len(judge["contradictions"]) == 1 and judge["claims_checked"] == 7
    # findings email even though 0 ops were applied
    assert result["emailed"] and "Quality audit findings" in sent[0]
    assert "RFC #1 vs RFC #2" in sent[0]

    events = EventsStore(settings.events_db)
    rows = {(r["step"], r["name"]): r["value"] for r in events.metrics_window(1)}
    assert rows[("consolidate", "contradictions")] == 1.0
    assert rows[("consolidate", "stale_claims")] == 1.0
    assert rows[("consolidate", "claims_checked")] == 7.0
    # the health footer picks the audit up
    lines = dict(build_health(events, settings.profile_dir))
    assert lines["profile audit (weekly)"] == "1 contradictions · 1 stale · 0 unsupported of 7 claims"
    events.close()


def test_judge_failure_never_breaks_consolidation(settings, monkeypatch):
    monkeypatch.setattr(profile_consolidate, "send_email", lambda *a: "smtp")
    monkeypatch.setattr(profile_consolidate, "judge_profile",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("judge down")))
    store = _seed_profile(settings)
    result = consolidate_profile(JudgeLLM(), store, settings)
    assert result["judge"]["claims_checked"] == 0  # degraded, not raised


def test_judge_dry_run_skips_llm_audit(settings):
    store = _seed_profile(settings)

    class NoAudit(JudgeLLM):
        def complete_json(self, prompt, system=None, **kw):
            assert "audit" not in (system or ""), "dry-run must not spend judge tokens"
            return {"ops": [], "notes": ""}

    result = consolidate_profile(NoAudit(), store, settings, dry_run=True)
    assert not result["emailed"]


# ── adaptive paper quota ─────────────────────────────────────────────

def _items(surfaced: int, done: int, unrelated: int = 0, age_days: int = 3):
    day = (TODAY - timedelta(days=age_days)).isoformat()
    items = [{"id": f"r{i}", "created": day, "status": "open"} for i in range(surfaced)]
    for i in range(done):
        items[i].update(status="done", done_at=TODAY.isoformat())
    for i in range(done, done + unrelated):
        items[i].update(status="unrelated", unrelated_at=TODAY.isoformat())
    return items


def test_quota_cold_start_keeps_configured(settings):
    quota, note = adaptive_paper_quota(settings, _items(surfaced=5, done=0))
    assert quota == settings.research_top_papers and note == ""


def test_quota_throttles_when_ignored(settings):
    quota, note = adaptive_paper_quota(settings, _items(surfaced=60, done=0))
    assert quota == 2  # floor — discovery never fully stops
    assert "paper quota" in note and "0 of 60" in note


def test_quota_scales_with_engagement(settings):
    # 28 acted in 14d = 2/day → ceil(3)+1 = 4
    quota, note = adaptive_paper_quota(settings, _items(surfaced=60, done=20, unrelated=8))
    assert quota == 4 and "28 of 60" in note
    # heavy reader → configured cap, no note
    quota, note = adaptive_paper_quota(
        settings, _items(surfaced=120, done=100), today=TODAY)
    assert quota == settings.research_top_papers and note == ""


def test_quota_counts_unrelated_as_engagement(settings):
    only_done, _ = adaptive_paper_quota(settings, _items(surfaced=60, done=14))
    with_unrelated, _ = adaptive_paper_quota(settings, _items(surfaced=60, done=7, unrelated=7))
    assert only_done == with_unrelated
