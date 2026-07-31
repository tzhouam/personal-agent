from assistant.agent.events_store import EventsStore


def test_seen_dedup(tmp_path):
    store = EventsStore(tmp_path / "events.db")
    ids = ["a", "b", "c"]
    assert store.filter_unseen(ids) == ["a", "b", "c"]
    store.mark_seen(["a", "c"])
    assert store.filter_unseen(ids) == ["b"]
    store.mark_seen(["a"])  # idempotent upsert
    assert store.filter_unseen(ids) == ["b"]
    store.close()


def test_observations_persist_and_fts(tmp_path):
    store = EventsStore(tmp_path / "events.db")
    row_ids = store.add_observations(
        "run-1",
        [{"source": "github", "ts": "t", "kind": "commit", "title": "Fixed scheduler bug",
          "url": None, "entities": ["repo/x"], "raw": {}}],
    )
    assert len(row_ids) == 1
    hits = store.conn.execute(
        "SELECT rowid FROM observations_fts WHERE observations_fts MATCH 'scheduler'"
    ).fetchall()
    assert len(hits) == 1
    store.close()


def test_versioned_seen_fingerprint_and_cooldown(settings):
    """F21: unchanged items never resurface; new activity resurfaces at most
    once per cooldown; legacy rows adopt without a resurface storm."""
    from datetime import datetime, timedelta, timezone

    from assistant.agent.events_store import EventsStore

    ev = EventsStore(settings.events_db)
    try:
        # new id surfaces, then is marked with its fingerprint
        assert ev.filter_unseen_versioned([("n1|mention", "t1")]) == ["n1|mention"]
        ev.mark_seen_versioned([("n1|mention", "t1")])
        # unchanged: suppressed forever (no bare-TTL resurface)
        assert ev.filter_unseen_versioned([("n1|mention", "t1")]) == []
        # new activity inside the cooldown: still suppressed…
        assert ev.filter_unseen_versioned([("n1|mention", "t2")]) == []
        # …but after the cooldown it surfaces (backdate last_seen 8 days)
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        ev.conn.execute("UPDATE seen SET last_seen = ? WHERE item_id = ?",
                        (old, "n1|mention"))
        ev.conn.commit()
        assert ev.filter_unseen_versioned([("n1|mention", "t2")]) == ["n1|mention"]
        assert ev.filter_unseen_versioned([("n1|mention", "t1")]) == []  # fp match

        # legacy row (pre-fingerprint context): suppressed AND adopted
        ev.mark_seen(["legacy|assign"], context="digest 2026-07-30")
        assert ev.filter_unseen_versioned([("legacy|assign", "t9")]) == []
        ev.conn.execute("UPDATE seen SET last_seen = ? WHERE item_id = ?",
                        (old, "legacy|assign"))
        ev.conn.commit()
        # adopted fingerprint t9: same fp stays suppressed, new one surfaces
        assert ev.filter_unseen_versioned([("legacy|assign", "t9")]) == []
        assert ev.filter_unseen_versioned([("legacy|assign", "t10")]) == \
            ["legacy|assign"]
    finally:
        ev.close()
