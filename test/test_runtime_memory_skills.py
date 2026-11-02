"""Migration-safe memory, trusted skill dispatch and evidence-based learning."""

import json
from pathlib import Path

import pytest
import yaml

from assistant.agent.events_store import EventsStore
from assistant.agent.runtime.memory import Memory
from assistant.agent.runtime.skills import SkillCatalog


def _yaml(path: Path, value: dict) -> bytes:
    """Create an old-format fixture and retain its byte-for-byte baseline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, allow_unicode=True))
    return path.read_bytes()


def _evidence(settings, *, verified=True) -> Path:
    """Model the authoritative evidence file written by the project worker."""
    path = settings.data_dir / "runtime" / "project-evidence" / "run-fixture.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"verified": verified, "project": "orion",
                               "checks": [{"argv": ["python", "-m", "pytest"],
                                           "returncode": 0 if verified else 1}]}))
    return path


def test_memory_is_append_only_persistent_and_project_scoped(settings):
    memory = Memory(settings)
    first = memory.remember("Orion uses SQLite", "conversation:1", project="orion")
    second = memory.remember("Orion uses SQLite", "conversation:2", project="orion")
    memory.remember("Owner prefers concise SQLite reports", "conversation:3")
    memory.remember("Secret project uses SQLite", "conversation:4", project="other")
    assert first["id"] != second["id"]
    rows = Memory(settings).recall("SQLite", project="orion")
    assert {row["id"] for row in rows} >= {first["id"], second["id"]}
    assert {row["project"] for row in rows} == {"", "orion"}
    assert memory.path.stat().st_mode & 0o777 == 0o600
    other = settings.model_copy(update={"data_dir": settings.data_dir.parent / "other-owner"})
    assert Memory(other).recall("SQLite") == []


def test_memory_reads_existing_stores_without_rewriting_them(settings):
    baselines = {}
    def seed(relative, value):
        """Write a pre-rewrite store without invoking its migration methods."""
        path = settings.data_dir / relative
        baselines[path] = _yaml(path, value)
    seed("profile/profile.yaml", {"identity": {"name": "Orion Owner"},
                                 "projects": [{"name": "Orion", "evidence": ["https://example.com/orion"]}]})
    seed("profile/lessons.yaml", {"next_id": 2, "lessons": [{"id": "L1", "status": "active",
         "rule": "Orion decisions need evidence", "source": "owner", "created": "2026-01-01"}]})
    seed("profile/todos.yaml", {"next_id": 2, "items": [{"id": "t1", "title": "Orion launch",
                                                        "status": "open", "created": "2026-01-01"}]})
    seed("profile/reading_list.yaml", {"next_id": 2, "items": [{"id": "r1", "title": "Orion paper",
                                                               "status": "open"}]})
    seed("profile/workflows.yaml", {"next_id": 2, "workflows": [{"id": "wf1", "name": "Orion review",
                                                                "status": "active", "steps": ["Read evidence"]}]})
    seed("profile/finance.yaml", {"next_id": 2, "records": [{"id": "f1", "note": "PRIVATE Orion expense"}]})
    seed("profile/health.yaml", {"next_id": 2, "records": [{"id": "h1", "note": "PRIVATE Orion health"}]})
    session = settings.data_dir / "sessions" / "old-flat.json"
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({"session": "old", "turns": [{"owner": "Orion kickoff",
                                    "assistant": "Remembered", "ts": "2026-01-01T12:00:00Z"}]}))
    baselines[session] = session.read_bytes()
    events = EventsStore(settings.events_db)
    events.add_observations("old-run", [{"source": "github", "kind": "commit", "ts": "2026-01-01",
                                        "title": "Orion implementation", "url": "https://example.com/commit"}])
    events.close()
    rows = Memory(settings).recall("Orion", limit=50)
    assert {row["source"] for row in rows} >= {"profile", "lessons", "todos", "reading",
                                               "workflows", "sessions", "observations"}
    assert not any("PRIVATE" in row["text"] for row in rows)
    assert all(path.read_bytes() == before for path, before in baselines.items())
    assert not (settings.profile_dir / "health").exists()
    assert not (settings.profile_dir / "finance").exists()
    assert not (settings.data_dir / "sessions" / "old-flat").exists()


def test_memory_keeps_private_categories_out_of_ambient_recall(settings):
    memory = Memory(settings)
    memory.remember("Orion meal details", "owner:1", kind="health")
    memory.remember("Orion bank details", "owner:2", kind="finance")
    public = memory.remember("Orion release decision", "owner:3")
    assert [row["id"] for row in memory.recall("Orion")] == [public["id"]]
    assert Memory(settings).recall("", limit=1)[0]["id"] == public["id"]


def test_memory_requires_provenance_and_redacts_credentials(settings):
    memory = Memory(settings)
    with pytest.raises(ValueError, match="provenance"):
        memory.remember("A fact", "")
    raw_key = "sk-proj-" + "A" * 40
    row = memory.remember("Never retain " + raw_key, "owner:1")
    assert raw_key not in row["text"]
    assert raw_key.encode() not in memory.path.read_bytes()
    with pytest.raises(ValueError, match="limit"):
        memory.recall("fact", limit=True)


def test_catalog_preserves_workflow_families_and_loads_instructions_progressively(settings):
    catalog = SkillCatalog(settings)
    rows = catalog.list()
    assert {row["family"] for row in rows} >= {"daily", "profile", "research", "website", "resume",
           "todos", "reading", "finance", "health", "reminders", "routines", "preferences", "workflows", "memory"}
    assert not any("instructions" in row for row in rows)
    assert "Finance" in catalog.describe("finance_summary")["instructions"] or "finance" in catalog.describe("finance_summary")["instructions"]
    assert catalog.describe("finance_summary")["parameters"]["properties"]["month"]["type"] == "string"
    runbooks = [row for row in rows if row["family"] == "runbooks"]
    assert runbooks and all(not row["executable"] for row in runbooks)
    names = {row["name"] for row in rows}
    assert not names.intersection({"execute_task", "plan_task", "approve_task", "reboot", "connect_github", "trigger_run", "run_workflow"})


def test_catalog_dispatches_existing_todo_data_and_rejects_untrusted_arguments(settings):
    _yaml(settings.profile_dir / "todos.yaml", {"next_id": 10, "items": [{"id": "t9", "key": "old",
           "title": "Preserved todo", "status": "open", "created": "2026-01-01"}]})
    catalog = SkillCatalog(settings)
    assert "Preserved todo" in catalog.execute("list_todos", {}).text
    assert catalog.execute("add_todo", {"title": "New runtime work"}).ok
    result = catalog.execute("list_todos", {})
    assert "t9" in result.text and "New runtime work" in result.text
    assert catalog.execute("done_todo", {"id": "t9"}).ok
    assert not catalog.execute("add_todo", {"title": "Bad", "handler": "os.system"}).ok
    assert not catalog.execute("log_transaction", {"kind": "expense", "amount": True}).ok
    assert not catalog.execute("approve_task", {"id": "task-other"}).ok


def test_bundled_runbooks_survive_a_non_editable_install(settings, tmp_path, monkeypatch):
    import assistant.agent.runtime.skills as skills
    monkeypatch.setattr(skills, "_REPO_RUNBOOKS", tmp_path / "no-checkout")
    rows = [row for row in SkillCatalog(settings).list() if row["family"] == "runbooks"]
    assert len(rows) == 14
    assert "Diagnose" in SkillCatalog(settings).describe("runbook_arxiv-query-recall")["instructions"]


def test_explicit_finance_and_health_skills_read_old_records(settings):
    finance = settings.profile_dir / "finance.yaml"
    finance_before = _yaml(finance, {"next_id": 2, "records": [{"id": "f1", "date": "2026-01-02",
          "type": "expense", "amount": 12.5, "currency": "CNY", "category": "food", "note": "old lunch"}]})
    health = settings.profile_dir / "health.yaml"
    health_before = _yaml(health, {"next_id": 2, "profile": {}, "needs": [], "records": [
          {"id": "h1", "date": "2026-01-02", "kind": "meal", "description": "old rice",
           "calories_kcal": 300, "protein_g": 7}]})
    catalog = SkillCatalog(settings)
    finances = catalog.execute("query_transactions", {"date": "2026-01-02"})
    health_records = catalog.execute("query_health", {"date": "2026-01-02"})
    assert finances.ok and "f1" in finances.text and "12.5" in finances.text
    assert health_records.ok and "h1" in health_records.text and "old rice" in health_records.text
    assert finance.read_bytes() == finance_before and health.read_bytes() == health_before


@pytest.mark.parametrize("phase", ["all", "run", "daily", "digest", "collect", "profile", "deliver", "website", "research", "todos", "consolidate"])
def test_all_indirect_publication_paths_require_approval(settings, phase):
    assert SkillCatalog(settings).requires_approval("run_phase", {"phase": phase})


def test_skill_effects_do_not_trust_legacy_risk_metadata(settings):
    catalog = SkillCatalog(settings)
    assert catalog.requires_approval("daily", {"dry_run": True})
    assert catalog.requires_approval("research", {})
    assert catalog.requires_approval("consolidate", {})
    assert catalog.requires_approval("set_reminder", {"message": "test", "when": "+1h"})
    assert catalog.requires_approval("enrich_profile", {"since": "2026-01"})
    assert not catalog.requires_approval("enrich_profile", {"since": "2026-01", "no_consolidate": True})
    assert not catalog.requires_approval("run_phase", {"phase": "curate"})
    assert not catalog.requires_approval("list_todos", {})
    with pytest.raises(ValueError):
        catalog.requires_approval("run_phase", {"phase": "unknown"})


def test_phase_skill_executes_in_current_worker_without_spawning(settings, monkeypatch):
    import assistant.agent.tasks.curate as task_curate
    seen = []
    monkeypatch.setattr(task_curate, "curate", lambda store: seen.append(store.dir) or {"decayed": []})
    result = SkillCatalog(settings).execute("run_phase", {"phase": "curate"})
    assert result.ok and seen == [settings.profile_dir]
    assert result.data == {"decayed": []}


def test_phase_skill_preserves_a_failed_publication_outcome(settings, monkeypatch):
    import assistant.agent.runtime.skills as skills
    monkeypatch.setattr(skills, "_sync_site", lambda config, profile: {"status": "failed", "note": "conflict"})
    result = SkillCatalog(settings).execute("website", {})
    assert not result.ok and result.data["status"] == "failed"
    assert "conflict" in result.text


def test_skill_draft_requires_verified_evidence_and_owner_promotion(settings):
    catalog = SkillCatalog(settings)
    evidence = _evidence(settings)
    draft = catalog.create_draft("orion-review", "Read tests, verify evidence, then report.", [str(evidence)], "orion")
    assert draft["status"] == "draft"
    assert "orion-review" not in {row["name"] for row in catalog.list()}
    approved = catalog.approve_draft(draft["id"])
    assert approved["status"] == "approved"
    desc = catalog.describe("orion-review")
    assert desc["kind"] == "documentation" and not desc["executable"]
    assert "Read tests" in desc["instructions"]
    assert Path(approved["path"]).stat().st_mode & 0o777 == 0o600
    assert catalog.approve_draft(draft["id"])["id"] == draft["id"]


def test_learned_markdown_cannot_supply_a_handler_or_run_commands(settings):
    catalog = SkillCatalog(settings)
    marker = settings.data_dir / "must-not-exist"
    instructions = f"---\nhandler: os.system\n---\n```sh\ntouch {marker}\n```"
    draft = catalog.create_draft("text-only", instructions, [str(_evidence(settings))])
    catalog.approve_draft(draft["id"])
    result = catalog.execute("text-only", {})
    assert result.ok and result.data["documentation_only"]
    assert "os.system" in result.text and not marker.exists()
    assert not catalog.execute("text-only", {"command": "touch ignored"}).ok


def test_draft_refuses_missing_failed_changed_and_outside_evidence(settings, tmp_path):
    catalog = SkillCatalog(settings)
    with pytest.raises(ValueError, match="verified evidence"):
        catalog.create_draft("no-proof", "A procedure", [])
    evidence = _evidence(settings, verified=False)
    with pytest.raises(ValueError, match="successful verification"):
        catalog.create_draft("failed-proof", "A procedure", [str(evidence)])
    evidence.write_text('{"verified": true}')
    with pytest.raises(ValueError, match="checks"):
        catalog.create_draft("unmeasured-proof", "A procedure", [str(evidence)])
    evidence = _evidence(settings)
    draft = catalog.create_draft("changed-proof", "A procedure", [str(evidence)])
    evidence.write_text(json.dumps({"verified": True, "changed": True,
                                   "checks": [{"argv": ["test"], "returncode": 0}]}))
    with pytest.raises(ValueError, match="changed"):
        catalog.approve_draft(draft["id"])
    outside = tmp_path / "outside.json"
    outside.write_text('{"verified": true}')
    with pytest.raises(ValueError, match="owner"):
        catalog.create_draft("outside-proof", "A procedure", [str(outside)])
    for name in ("../escape", "daily", "finance_summary", "/absolute"):
        with pytest.raises(ValueError, match="name"):
            catalog.create_draft(name, "A procedure", [str(evidence)])
    with pytest.raises(ValueError, match="draft id"):
        catalog.approve_draft("../../escape")


def test_draft_refuses_symlinked_evidence_and_output_paths(settings, tmp_path):
    catalog = SkillCatalog(settings)
    evidence = _evidence(settings)
    alias = settings.data_dir / "alias.json"
    alias.symlink_to(evidence)
    with pytest.raises(ValueError, match="symlink"):
        catalog.create_draft("alias-proof", "A procedure", [str(alias)])
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (settings.data_dir / "runtime" / "skill-drafts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        catalog.create_draft("symlink-output", "A procedure", [str(evidence)])
    assert list(outside.iterdir()) == []
