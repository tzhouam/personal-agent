import sqlite3
from datetime import datetime, timedelta, timezone

from assistant.agent.collectors.chrome import ChromeCollector, _to_chrome_time
from assistant.agent.collectors.github import GitHubCollector, _api_to_html_url


def test_api_to_html_url():
    assert (
        _api_to_html_url("https://api.github.com/repos/o/r/pulls/123")
        == "https://github.com/o/r/pull/123"
    )
    assert (
        _api_to_html_url("https://api.github.com/repos/o/r/issues/9")
        == "https://github.com/o/r/issues/9"
    )
    assert (
        _api_to_html_url("https://api.github.com/repos/o/r/commits/abc123")
        == "https://github.com/o/r/commit/abc123"
    )
    assert _api_to_html_url(None) is None


def test_push_event_uses_payload_size(settings):
    collector = GitHubCollector(settings)
    ts = datetime.now(timezone.utc)
    obs = collector._event_to_observation(
        {"type": "PushEvent", "repo": {"name": "o/r"},
         "payload": {"size": 5, "commits": []}},
        ts,
    )
    assert "Pushed 5 commit(s) to o/r" == obs["title"]
    assert obs["entities"] == ["o/r"]


def test_merged_pr_event(settings):
    collector = GitHubCollector(settings)
    obs = collector._event_to_observation(
        {"type": "PullRequestEvent", "repo": {"name": "o/r"},
         "payload": {"action": "closed",
                     "pull_request": {"merged": True, "title": "Fix bug",
                                      "html_url": "https://github.com/o/r/pull/1"}}},
        datetime.now(timezone.utc),
    )
    assert obs["title"] == "PR merged in o/r: Fix bug"
    assert obs["url"] == "https://github.com/o/r/pull/1"


def test_authored_item_rfc_and_pr_detection(settings):
    collector = GitHubCollector(settings)
    rfc = collector._issue_to_observation(
        {"title": "[RFC]: Modular audio pipeline", "state": "open", "number": 7,
         "repository_url": "https://api.github.com/repos/vllm-project/vllm-omni",
         "body": "## Motivation\r\nUnify  talker interfaces.",
         "html_url": "https://github.com/vllm-project/vllm-omni/issues/7", "labels": []},
    )
    assert rfc["kind"] == "rfc"
    assert rfc["title"].startswith("RFC [open] in vllm-project/vllm-omni: [RFC]: Modular audio pipeline")
    assert "Unify talker interfaces." in rfc["title"]  # body snippet, whitespace collapsed

    pr = collector._issue_to_observation(
        {"title": "Fix scheduler", "state": "closed", "number": 8,
         "repository_url": "https://api.github.com/repos/o/r", "body": None,
         "html_url": "https://github.com/o/r/pull/8", "labels": [],
         "pull_request": {}},
    )
    assert pr["kind"] == "pr_authored" and pr["title"] == "PR [closed] in o/r: Fix scheduler"

    issue = collector._issue_to_observation(
        {"title": "Bug report", "state": "open", "number": 9,
         "repository_url": "https://api.github.com/repos/o/r", "body": "",
         "html_url": "https://github.com/o/r/issues/9",
         "labels": [{"name": "rfc"}]},
    )
    assert issue["kind"] == "rfc"  # label-based detection


def _make_history(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
    conn.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER)")
    for i, (url, title, when) in enumerate(rows, start=1):
        conn.execute("INSERT INTO urls VALUES (?, ?, ?)", (i, url, title))
        conn.execute("INSERT INTO visits VALUES (?, ?, ?)", (i, i, _to_chrome_time(when)))
    conn.commit()
    conn.close()


def test_chrome_privacy_tiers(settings):
    now = datetime.now(timezone.utc)
    _make_history(
        settings.chrome_history_path,
        [
            ("https://arxiv.org/abs/2501.00001", "Great Paper", now),          # allowlisted
            ("https://www.mybank.com/login", "My Bank", now),                  # denylisted
            ("https://news.ycombinator.com/item?id=1", "HN thread", now),      # domain-count only
            ("https://news.ycombinator.com/item?id=2", "HN thread 2", now),
            ("https://arxiv.org/abs/2501.00002", "Old Paper", now - timedelta(days=9)),  # too old
        ],
    )
    obs = ChromeCollector(settings).collect(now - timedelta(hours=26))
    titles = [o["title"] for o in obs]

    assert "Visited: Great Paper" in titles
    assert not any("Bank" in t or "mybank" in t for t in titles)
    assert "Browsed news.ycombinator.com (2 visits)" in titles
    assert not any("Old Paper" in t for t in titles)
    # non-allowlisted URLs never appear verbatim
    assert not any("ycombinator.com/item" in (o.get("url") or "") for o in obs)


def test_chrome_missing_file_is_noop(settings):
    assert ChromeCollector(settings).collect(datetime.now(timezone.utc)) == []
