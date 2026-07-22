"""Self-serve website ability: connect_github + build_personal_website, the
per-user schedule gating, and the persistence-layer secret masker."""
import httpx
import pytest

from assistant.agent.actions import run_action
from assistant.platform.config import Settings
from assistant.platform.jobs import JobQueue
from assistant.platform.user_config import update_user_config


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPError(f"status {self.status_code}")


class _FakeClient:
    """Context-managed stand-in for the GitHub httpx.Client used by
    site_repo_status — every GET returns the same canned response."""
    def __init__(self, resp):
        self.resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return self.resp


@pytest.fixture
def mt_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return Settings.for_user("alice1")


# ── connect_github ───────────────────────────────────────────────────

def test_connect_github_validates_stores_and_never_echoes_token(mt_settings, monkeypatch):
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: _Resp(200, {"login": "octo"},
                                              {"x-oauth-scopes": "repo, gist"}))
    token = "ghp_" + "A" * 36
    msg = run_action("connect_github", {"token": token}, mt_settings)
    assert "connected as octo" in msg
    assert token not in msg                     # the raw token is never echoed back
    cfg = (mt_settings.data_dir / "config.env").read_text()
    assert f"GITHUB_TOKEN={token}" in cfg and "GITHUB_USER=octo" in cfg
    # the write is visible to the next-turn per-user Settings
    assert Settings.for_user("alice1").github_token == token


def test_connect_github_warns_on_missing_repo_scope(mt_settings, monkeypatch):
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: _Resp(200, {"login": "octo"},
                                              {"x-oauth-scopes": "gist"}))
    msg = run_action("connect_github", {"token": "ghp_" + "B" * 36}, mt_settings)
    assert "'repo' scope" in msg


def test_connect_github_accepts_finegrained_without_scope_header(mt_settings, monkeypatch):
    # fine-grained tokens send NO x-oauth-scopes header → accept, no warning
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, {"login": "octo"}, {}))
    msg = run_action("connect_github", {"token": "github_pat_" + "c" * 60}, mt_settings)
    assert "connected as octo" in msg and "missing" not in msg


def test_connect_github_rejects_bad_token(mt_settings, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(401))
    msg = run_action("connect_github", {"token": "ghp_" + "D" * 36}, mt_settings)
    assert "401" in msg
    assert not (mt_settings.data_dir / "config.env").exists()   # nothing persisted


def test_connect_github_rejects_masked_token_without_crashing(mt_settings, monkeypatch):
    # Regression: a masked token (github_pat_…KFh, the form the LLM echoes from
    # history) must NOT reach httpx — the non-ASCII '…' used to crash on header
    # encode ('ascii' codec can't encode '…').
    def _boom(*a, **k):
        raise AssertionError("httpx.get must not be called with a masked token")
    monkeypatch.setattr(httpx, "get", _boom)
    msg = run_action("connect_github", {"token": "github_pat_…KFh"}, mt_settings)
    assert "完整" in msg or "complete token" in msg
    assert not (mt_settings.data_dir / "config.env").exists()


def test_find_and_validate_github_token():
    from assistant.platform.secrets import find_github_token, looks_like_github_token
    real = "github_pat_11AT4MNKI03po409CESWrQ_QZpnRzich1PBxAhxldWUZkCAkkdMj27FRpZi2IVGjsd4OHZM"
    assert find_github_token(f"here you go: {real} thanks") == real
    assert find_github_token("ghp_" + "A" * 36) == "ghp_" + "A" * 36
    # a token that wrapped with a stray space still resolves
    assert find_github_token("github_pat_ " + "B" * 40) == "github_pat_" + "B" * 40
    assert find_github_token("no token here") == ""
    # masked form is rejected by the validator (non-ASCII '…')
    assert looks_like_github_token("github_pat_…KFh") is None
    assert looks_like_github_token(real) == real


def test_bind_github_token_uses_raw_message_not_llm_echo():
    from assistant.agent.chat.agent import _bind_github_token
    real = "ghp_" + "Z" * 36
    actions = [{"type": "connect_github", "token": "github_pat_…KFh"}]   # LLM's masked echo
    bound = _bind_github_token(actions, f"connect my github {real}")
    assert bound[0]["token"] == real                                     # overridden from raw text
    # no token in the raw message → cleared so the handler asks for it
    assert _bind_github_token([{"type": "connect_github", "token": "x"}], "connect it")[0]["token"] == ""


# ── build_personal_website (synchronous pre-flight) ──────────────────

def _stub_status(monkeypatch, state):
    from assistant.agent.tasks import build_website as bw
    monkeypatch.setattr(bw, "site_repo_status",
                        lambda s: {"state": state, "login": "octo",
                                   "repo": "octo/octo.github.io",
                                   "url": "https://octo.github.io"})


def test_build_website_asks_for_token_when_not_connected(mt_settings, monkeypatch):
    _stub_status(monkeypatch, "missing_token")
    msg = run_action("build_personal_website", {}, mt_settings)
    assert "token first" in msg
    assert JobQueue(mt_settings.shared_dir).claim() is None      # nothing enqueued


def test_build_website_enqueues_when_repo_absent(mt_settings, monkeypatch):
    _stub_status(monkeypatch, "absent")
    msg = run_action("build_personal_website", {}, mt_settings)
    assert "building your site" in msg and "octo.github.io" in msg
    job = JobQueue(mt_settings.shared_dir).claim()
    assert job["kind"] == "build_website" and job["args"] == {"overwrite": False}


def test_build_website_guards_existing_site_then_overwrites(mt_settings, monkeypatch):
    _stub_status(monkeypatch, "nonempty")
    # no confirm → ask, do not enqueue
    msg = run_action("build_personal_website", {}, mt_settings)
    assert "overwrite" in msg.lower()
    assert JobQueue(mt_settings.shared_dir).claim() is None
    # confirm → enqueue with overwrite:true
    run_action("build_personal_website", {"confirm": "overwrite"}, mt_settings)
    job = JobQueue(mt_settings.shared_dir).claim()
    assert job["kind"] == "build_website" and job["args"] == {"overwrite": True}


def test_build_website_dedupes_in_flight(mt_settings, monkeypatch):
    _stub_status(monkeypatch, "absent")
    run_action("build_personal_website", {}, mt_settings)          # queued
    msg = run_action("build_personal_website", {}, mt_settings)    # second while in flight
    assert "already running" in msg


def test_site_repo_status_maps_states(mt_settings, monkeypatch):
    from assistant.agent.tasks import build_website as bw
    update_user_config(mt_settings.data_dir, {"GITHUB_TOKEN": "ghp_x", "GITHUB_USER": "octo"})
    s = Settings.for_user("alice1")
    monkeypatch.setattr(bw, "_api_client", lambda t: _FakeClient(_Resp(404)))
    assert bw.site_repo_status(s)["state"] == "absent"
    monkeypatch.setattr(bw, "_api_client", lambda t: _FakeClient(_Resp(200, {"size": 0})))
    assert bw.site_repo_status(s)["state"] == "empty"
    monkeypatch.setattr(bw, "_api_client", lambda t: _FakeClient(_Resp(200, {"size": 42})))
    assert bw.site_repo_status(s)["state"] == "nonempty"


# ── persistence-layer masking ────────────────────────────────────────

def test_history_append_masks_pasted_token(tmp_path):
    from assistant.platform.serve import SessionStore
    store = SessionStore(tmp_path)
    tok = "ghp_" + "E" * 36
    store.append("sess", owner=f"connect this {tok} please", assistant="ok")
    dumped = str(store.history("sess"))
    assert tok not in dumped
    assert "ghp_…EEEE" in dumped
