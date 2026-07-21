from assistant.agent.profile_store import ProfileStore, render_summary


def make_store(tmp_path):
    return ProfileStore(tmp_path / "profile")


def base_profile():
    return {
        "identity": {"name": "Tester", "github": "tester", "emails": [], "affiliations": []},
        "skills": [
            {"name": "Python", "level": "expert", "evidence": [], "last_seen": "2026-01-01", "status": "active"}
        ],
        "interests": [],
        "projects": [
            {"name": "proj-a", "role": "owner", "highlights": [], "evidence": [], "status": "active"}
        ],
        "education": [{"school": "ExampleU"}],
    }


def test_bump_and_evidence(tmp_path):
    store = make_store(tmp_path)
    profile, applied, rejected = store.apply_ops(
        base_profile(),
        [
            {"op": "bump_last_seen", "section": "skills", "name": "python"},  # case-insensitive
            {"op": "add_evidence", "section": "skills", "name": "Python", "evidence": "wrote a lib"},
        ],
        today="2026-07-02",
    )
    assert len(applied) == 2 and not rejected
    assert profile["skills"][0]["last_seen"] == "2026-07-02"
    assert profile["skills"][0]["evidence"] == ["wrote a lib"]


def test_protected_sections_rejected(tmp_path):
    store = make_store(tmp_path)
    profile, applied, rejected = store.apply_ops(
        base_profile(),
        [
            {"op": "add_evidence", "section": "education", "name": "ExampleU", "evidence": "x"},
            {"op": "delete_everything", "section": "skills", "name": "Python"},
        ],
    )
    assert not applied and len(rejected) == 2
    assert profile["education"] == [{"school": "ExampleU"}]


def test_new_skill_starts_emerging_and_dormant_never_deletes(tmp_path):
    store = make_store(tmp_path)
    profile, applied, _ = store.apply_ops(
        base_profile(),
        [
            {"op": "add_skill", "name": "Rust", "evidence": ["repo x"]},
            {"op": "mark_dormant", "section": "skills", "name": "Python"},
        ],
    )
    assert len(applied) == 2
    rust = next(s for s in profile["skills"] if s["name"] == "Rust")
    assert rust["level"] == "emerging"
    python = next(s for s in profile["skills"] if s["name"] == "Python")
    assert python["status"] == "dormant" and python in profile["skills"]  # still there


def test_duplicate_ops_rejected(tmp_path):
    store = make_store(tmp_path)
    profile = base_profile()
    profile, applied, rejected = store.apply_ops(
        profile,
        [
            {"op": "add_project", "name": "proj-a"},  # already exists
            {"op": "update_highlight", "section": "projects", "name": "proj-a", "highlight": "h1"},
            {"op": "update_highlight", "section": "projects", "name": "proj-a", "highlight": "h1"},
        ],
    )
    assert len(applied) == 1 and len(rejected) == 2
    assert profile["projects"][0]["highlights"] == ["h1"]


def test_save_commits_and_diffs(tmp_path):
    store = make_store(tmp_path)
    diff1 = store.save(base_profile(), "initial")
    assert diff1 == "(initial profile version)"
    profile = store.load()
    profile["skills"][0]["last_seen"] = "2026-07-02"
    diff2 = store.save(profile, "update")
    assert "2026-07-02" in diff2
    assert store.save(profile, "no-op") == ""  # unchanged → no commit


def test_render_summary_skips_dormant(tmp_path):
    profile = base_profile()
    profile["skills"][0]["status"] = "dormant"
    assert "Python" not in render_summary(profile)


def test_render_summary_includes_avoid_topics():
    profile = base_profile()
    profile["preferences"] = {"avoid_topics": ["medical imaging"]}
    summary = render_summary(profile)
    assert "NOT interested" in summary and "medical imaging" in summary
    assert "NOT interested" not in render_summary(base_profile())
