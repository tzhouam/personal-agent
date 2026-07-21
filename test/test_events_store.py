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
