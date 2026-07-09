"""Profile-v2 (doc/RESEARCH_AGENT_MEMORY_2026.md §4): merge/move/rewrite ops,
stability gating, initiatives, ops-log context, and the consolidation task."""

import json

from assistant.profile_store import (ALIASES_TEMPLATE, CONSOLIDATE_OPS, DAILY_OPS,
                                     ProfileStore, append_ops_log, load_aliases,
                                     recent_ops, render_initiatives, render_markdown)
from assistant.tasks import profile_consolidate
from assistant.tasks.profile_consolidate import consolidate_profile
from assistant.tasks.profile_update import update_profile


class FakeLLM:
    def __init__(self, result):
        self.result = result
        self.prompts = []

    def complete_json(self, prompt, system=None, **kw):
        self.prompts.append(prompt)
        return self.result if not callable(self.result) else self.result(prompt)


def fragmented_profile():
    return {
        "identity": {"name": "Tester", "github": "tester", "emails": [], "affiliations": []},
        "skills": [
            {"name": "Java", "level": "working",
             "evidence": ["GitHub repos: course-lab-2021"], "status": "active"},
        ],
        "interests": [],
        "projects": [
            {"name": "main-project", "role": "core contributor",
             "highlights": ["Authored the Phase 1 RFC (#100)",
                            "Authored and merged the Phase 1 RFC (#100), validated bit-exact."],
             "evidence": ["https://github.com/org/main"], "status": "active"},
            {"name": "side-repo", "role": "owner",
             "highlights": ["Fixed streaming bug (issue #41)"],
             "evidence": ["https://github.com/me/side-repo",
                          "Fixed streaming HNR threshold issue #41 (https://github.com/org/main/issues/41)"],
             "status": "active"},
            {"name": "docs-repo", "role": "owner", "highlights": [],
             "evidence": ["https://github.com/me/docs-repo"], "status": "active"},
        ],
        "education": [{"school": "X"}],
        "experience": [{"title": "Engineer", "org": "Co",
                        "highlights": ["Led the Phase 1 design end-to-end"]}],
    }


# ── new ops ──────────────────────────────────────────────────────────

def test_merge_projects_unions_and_stubs(tmp_path):
    store = ProfileStore(tmp_path / "p")
    profile, applied, rejected = store.apply_ops(
        fragmented_profile(),
        [{"op": "merge_projects", "into": "main-project", "from": "docs-repo"}],
        today="2026-07-09")
    assert applied and not rejected
    main = profile["projects"][0]
    assert "https://github.com/me/docs-repo" in main["evidence"]
    stub = profile["projects"][2]
    assert stub == {"name": "docs-repo", "status": "merged",
                    "merged_into": "main-project", "merged_at": "2026-07-09"}
    # merged stubs disappear from the rendered site/markdown
    assert "docs-repo" not in render_markdown(profile)


def test_move_evidence_fixes_misattribution(tmp_path):
    store = ProfileStore(tmp_path / "p")
    profile, applied, _ = store.apply_ops(
        fragmented_profile(),
        [{"op": "move_evidence", "from": "side-repo", "to": "main-project",
          "match": "issue #41"}],
        today="2026-07-09")
    assert applied
    assert any("issue #41" in e for e in profile["projects"][0]["evidence"])
    assert not any("issue #41" in e for e in profile["projects"][1]["evidence"])
    # unmatched move is rejected, not silently dropped
    _, applied2, rejected2 = store.apply_ops(
        profile, [{"op": "move_evidence", "from": "side-repo", "to": "main-project",
                   "match": "nonexistent"}])
    assert not applied2 and rejected2


def test_rewrite_entry_keeps_history_and_dedupes(tmp_path):
    store = ProfileStore(tmp_path / "p")
    profile, applied, _ = store.apply_ops(
        fragmented_profile(),
        [{"op": "rewrite_entry", "section": "projects", "name": "main-project",
          "highlights": ["Designed and shipped Phase 1 KV-cache management "
                         "(RFC #100, https://github.com/org/main), validated bit-exact"]}],
        today="2026-07-09", allowed=CONSOLIDATE_OPS)
    assert applied
    main = profile["projects"][0]
    assert len(main["highlights"]) == 1
    # both superseded near-duplicates preserved as audit rows
    assert len(main["history"]) == 2


def test_rewrite_stability_gate(tmp_path):
    store = ProfileStore(tmp_path / "p")
    profile = fragmented_profile()
    profile["projects"][0]["confirmations"] = 5  # stable entry
    profile["projects"][0]["highlights"] = [
        "Did X (https://github.com/org/main/pull/1)",
        "Did Y (https://github.com/org/main/pull/2)"]
    # rewrite citing fewer URLs than it replaces → rejected
    _, applied, rejected = store.apply_ops(
        profile, [{"op": "rewrite_entry", "section": "projects", "name": "main-project",
                   "highlights": ["Did things"]}], allowed=CONSOLIDATE_OPS)
    assert not applied and rejected
    # citing at least as many sources → accepted
    _, applied2, _ = store.apply_ops(
        profile, [{"op": "rewrite_entry", "section": "projects", "name": "main-project",
                   "highlights": ["Did X and Y (https://github.com/org/main/pull/1, "
                                  "https://github.com/org/main/pull/2)"]}],
        allowed=CONSOLIDATE_OPS)
    assert applied2


def test_rewrite_evidence_may_not_lose_urls(tmp_path):
    store = ProfileStore(tmp_path / "p")
    profile, applied, _ = store.apply_ops(
        fragmented_profile(),
        [{"op": "rewrite_entry", "section": "projects", "name": "side-repo",
          "highlights": ["kept"],
          "evidence": ["only one thing"]}],  # would drop both cited URLs
        allowed=CONSOLIDATE_OPS)
    assert applied  # highlight rewrite applies…
    side = profile["projects"][1]
    assert len(side["evidence"]) == 2  # …but the lossy evidence list is ignored


def test_confirmations_bump_and_daily_cannot_rewrite(tmp_path):
    store = ProfileStore(tmp_path / "p")
    profile = fragmented_profile()
    profile, applied, _ = store.apply_ops(
        profile,
        [{"op": "add_evidence", "section": "projects", "name": "main-project",
          "evidence": "merged PR #7 (https://github.com/org/main/pull/7)"},
         {"op": "bump_last_seen", "section": "projects", "name": "main-project"}])
    assert profile["projects"][0]["confirmations"] == 2
    # rewrite_entry is not in the daily op set
    _, applied2, rejected2 = store.apply_ops(
        profile, [{"op": "rewrite_entry", "section": "projects", "name": "main-project",
                   "highlights": ["x (https://a https://b https://c)"]}])
    assert not applied2 and rejected2
    assert "rewrite_entry" in CONSOLIDATE_OPS and "rewrite_entry" not in DAILY_OPS


def test_rewrite_skill_evidence_only(tmp_path):
    """Skills hold evidence, not highlights — an evidence/level rewrite must
    work without highlights, and stripping everything must not."""
    store = ProfileStore(tmp_path / "p")
    profile = fragmented_profile()
    profile["skills"][0]["evidence"] = [
        "GitHub repos: course-lab-2021",
        "Built the AR engine (https://github.com/org/main/pull/4)",
        "Also built the AR engine (https://github.com/org/main/pull/4)"]
    profile, applied, _ = store.apply_ops(
        profile,
        [{"op": "rewrite_entry", "section": "skills", "name": "Java",
          "evidence": ["AR engine + model runners (https://github.com/org/main/pull/4)"],
          "level": "expert"}],
        allowed=CONSOLIDATE_OPS)
    assert applied
    java = profile["skills"][0]
    assert java["level"] == "expert" and len(java["evidence"]) == 1
    # pure strip (no highlights, no evidence, no level) is rejected
    _, applied2, rejected2 = store.apply_ops(
        profile, [{"op": "rewrite_entry", "section": "skills", "name": "Java"}],
        allowed=CONSOLIDATE_OPS)
    assert not applied2 and rejected2


# ── aliases + ops log ────────────────────────────────────────────────

def test_aliases_load_and_render(tmp_path):
    assert load_aliases(tmp_path) == []
    (tmp_path / "aliases.yaml").write_text(
        "initiatives:\n"
        "  - name: BDE\n    entry: main-project\n    patterns: [docs-repo, 'RFC #100']\n")
    aliases = load_aliases(tmp_path)
    assert aliases[0]["name"] == "BDE"
    block = render_initiatives(aliases)
    assert "BDE" in block and "main-project" in block and "RFC #100" in block
    assert "initiatives" in ALIASES_TEMPLATE


def test_ops_log_roundtrip(tmp_path):
    append_ops_log(tmp_path, [{"op": "add_evidence", "name": "x"}], "2026-07-09")
    append_ops_log(tmp_path, [{"op": "add_project", "name": "old"}], "2020-01-01")
    recent = recent_ops(tmp_path, days=7)
    assert [o["name"] for o in recent] == ["x"]  # old entries filtered


def test_daily_prompt_carries_initiatives_and_week_ops(tmp_path):
    store = ProfileStore(tmp_path / "p")
    store.ensure_repo()
    store.save(fragmented_profile(), "seed")
    (store.dir / "aliases.yaml").write_text(
        "initiatives:\n  - name: BDE initiative\n    entry: main-project\n    patterns: [docs-repo]\n")
    from datetime import date
    append_ops_log(store.dir, [{"op": "add_evidence", "section": "projects",
                                "name": "main-project"}], date.today().isoformat())
    llm = FakeLLM({"ops": [], "notes": ""})
    update_profile(llm, store, [{"source": "github", "kind": "pr", "title": "t", "url": "u"}])
    prompt = llm.prompts[0]
    assert "BDE initiative" in prompt          # P1: join keys visible
    assert "last 7 days" in prompt             # P4: multi-day arc visible
    assert "add_evidence projects/main-project" in prompt


# ── consolidation task ───────────────────────────────────────────────

def _consolidation_result(prompt):
    if "Section to consolidate: projects" in prompt:
        return {"ops": [
            {"op": "merge_projects", "into": "main-project", "from": "docs-repo"},
            {"op": "move_evidence", "from": "side-repo", "to": "main-project",
             "match": "issue #41"},
            {"op": "rewrite_entry", "name": "main-project",
             "highlights": ["Designed and shipped Phase 1 (RFC #100, "
                            "https://github.com/org/main)"]},
        ], "notes": "merged fragments"}
    if "Section to consolidate: skills" in prompt:
        return {"ops": [
            {"op": "add_skill", "name": "LLM inference systems",
             "evidence": ["main-project engine work (https://github.com/org/main)"]},
            {"op": "mark_dormant", "name": "Java"},
        ], "notes": "re-based skills"}
    return {"ops": [], "notes": ""}


def test_consolidate_applies_merges_and_emails(tmp_path, settings, monkeypatch):
    sent = []
    monkeypatch.setattr(profile_consolidate, "send_email",
                        lambda s, subject, body: sent.append((subject, body)) or "smtp")
    store = ProfileStore(settings.profile_dir)
    store.ensure_repo()
    store.save(fragmented_profile(), "seed")
    llm = FakeLLM(_consolidation_result)

    result = consolidate_profile(llm, store, settings)
    assert len(result["applied"]) == 5 and not result["rejected"]
    assert result["emailed"] and "Profile consolidation" in sent[0][0]
    assert result["diff"]  # committed

    profile = store.load()
    main = next(p for p in profile["projects"] if p["name"] == "main-project")
    assert main["highlights"] == ["Designed and shipped Phase 1 (RFC #100, "
                                  "https://github.com/org/main)"]
    assert len(main["history"]) == 2
    assert next(p for p in profile["projects"] if p["name"] == "docs-repo")["status"] == "merged"
    assert any(s["name"] == "LLM inference systems" for s in profile["skills"])
    java = next(s for s in profile["skills"] if s["name"] == "Java")
    assert java["status"] == "dormant"
    # style reference (manual experience section) was in the prompt
    assert "Led the Phase 1 design end-to-end" in llm.prompts[0]
    # ops log written for the P4 context window
    assert (store.dir / "ops_log.jsonl").exists()


def test_consolidate_dry_run_changes_nothing(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(profile_consolidate, "send_email",
                        lambda *a: (_ for _ in ()).throw(AssertionError("emailed in dry-run")))
    store = ProfileStore(settings.profile_dir)
    store.ensure_repo()
    store.save(fragmented_profile(), "seed")
    result = consolidate_profile(FakeLLM(_consolidation_result), store, settings,
                                 section="projects", dry_run=True)
    assert result["applied"] and not result["emailed"] and result["diff"] == ""
    # nothing persisted
    assert store.load()["projects"][2]["status"] == "active"


def test_consolidate_survives_llm_failure(tmp_path, settings):
    store = ProfileStore(settings.profile_dir)
    store.ensure_repo()
    store.save(fragmented_profile(), "seed")

    class BoomLLM:
        def complete_json(self, *a, **k):
            raise RuntimeError("api down")

    result = consolidate_profile(BoomLLM(), store, settings)
    assert result["applied"] == [] and "failed" in result["notes"]
