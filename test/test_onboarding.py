"""First-contact self-onboarding: invite lifecycle, the code→name→provision
state machine, transactional rollback, the identity gate, and serve `/chat`
routing (unknown account onboards; every other route stays fail-closed)."""

import threading

import httpx
import pytest
import yaml

from assistant import admin
from assistant.config import Settings
from assistant.identity import onboarding_candidate
from assistant.onboarding import (InviteStore, handle,
                                  provision_user)
from assistant.registry import UserRegistry
from assistant.serve import make_server


def _base(tmp_path, **kw):
    return Settings(_env_file=None, data_dir=tmp_path / "data",
                    deployment_mode="multi_tenant", **kw)


# ── invite store ─────────────────────────────────────────────────────

def test_invite_create_reserve_single_use_and_expiry(tmp_path):
    base = _base(tmp_path)
    store = InviteStore(base.shared_dir)
    code = store.create(ttl_days=7)
    assert len(code) == 14 and code.count("-") == 2          # K7PQ-M9RT-3XVW
    # messy input (lowercase, extra spaces) normalizes to the same code
    assert store.reserve(f"  {code.lower().replace('-', ' ')} ", "wx-A") == "reserved"
    # same account re-reserving is idempotent; another account is refused
    assert store.reserve(code, "wx-A") == "reserved"
    assert store.reserve(code, "wx-B") == "bad"
    store.mark_used("wx-A")
    assert store.reserve(code, "wx-A") == "used"             # single-use
    assert store.reserve("ZZZZ-ZZZZ-ZZZZ", "wx-C") == "bad"  # unknown
    # expired
    raw = yaml.safe_load(store.path.read_text())
    raw["invites"].append({"code_hash": __import__("assistant.registry", fromlist=["hash_token"])
                           .hash_token("AAAA-AAAA-AAAA"), "status": "open",
                           "created": "2000-01-01T00:00:00+00:00",
                           "expires": "2000-01-02T00:00:00+00:00"})
    store.path.write_text(yaml.safe_dump(raw))
    assert store.reserve("AAAA-AAAA-AAAA", "wx-D") == "expired"


def test_invite_active_lists_no_secrets(tmp_path):
    base = _base(tmp_path)
    store = InviteStore(base.shared_dir)
    store.create()
    rows = store.active()
    assert len(rows) == 1 and rows[0]["status"] == "open"
    assert "code_hash" not in rows[0] and "code" not in rows[0]


# ── provisioning ─────────────────────────────────────────────────────

def test_provision_creates_isolated_tenant_no_cred_leak(tmp_path):
    base = _base(tmp_path)
    uid = provision_user(base, "wx-new", "小明")
    reg = UserRegistry(base.data_dir)
    assert reg.by_channel("weixin", "wx-new") == uid
    udir = base.data_dir / "users" / uid
    assert (udir / "profile" / "profile.yaml").exists()
    assert yaml.safe_load((udir / "profile" / "profile.yaml").read_text()
                          )["identity"]["name"] == "小明"
    # config.env exists but carries NO credentials (no owner-cred inheritance)
    cfg = (udir / "config.env").read_text()
    assert "GITHUB_TOKEN=\n" in cfg and "sk-" not in cfg
    # everything lives under THIS base's data dir, not the real one
    assert str(udir).startswith(str(base.data_dir))


def test_provision_rolls_back_on_failure(tmp_path):
    base = _base(tmp_path)
    reg = UserRegistry(base.data_dir)
    reg.add_user("other")
    reg.bind_channel("other", "weixin", "wx-taken")          # account already bound
    before = {u["uid"] for u in reg.users()}
    with pytest.raises(ValueError):                          # uniqueness → bind fails
        provision_user(base, "wx-taken", "dupe")
    after = {u["uid"] for u in UserRegistry(base.data_dir).users()}
    assert after == before                                   # no half-created user
    # the partial data dir was cleaned (only 'other' may exist, never a new uid)
    stray = [p.name for p in (base.data_dir / "users").iterdir()
             if p.name != "other"] if (base.data_dir / "users").exists() else []
    assert stray == []


# ── state machine ────────────────────────────────────────────────────

def test_handle_full_flow(tmp_path):
    base = _base(tmp_path)
    code = InviteStore(base.shared_dir).create()
    assert "邀请码" in handle("wx-1", "", base)               # ask for code
    assert "无效" in handle("wx-1", "NOPE-NOPE-NOPE", base)   # bad code
    assert "请回复" in handle("wx-1", code, base)             # good → ask name
    assert "欢迎" in handle("wx-1", "阿力", base)             # name → provisioned
    assert UserRegistry(base.data_dir).by_channel("weixin", "wx-1")
    # a now-bound account is no longer an onboarding candidate
    assert onboarding_candidate("t", {"account_id": "wx-1"}, base,
                                UserRegistry(base.data_dir)) is None


def test_handle_bounds_bad_code_attempts(tmp_path):
    base = _base(tmp_path)
    for _ in range(4):
        assert handle("wx-x", "WRONG-WRONG-WRON", base) == \
            "邀请码无效或已过期，请检查后重发 🙏"
    # 5th bad attempt → go quiet (fail-closed SAFE), not a helpful prompt
    assert handle("wx-x", "WRONG-WRONG-WRON", base) == "系统暂时不可用，请稍后再试 🙏"


def test_handle_name_validation_and_already_registered(tmp_path):
    base = _base(tmp_path)
    code = InviteStore(base.shared_dir).create()
    handle("wx-2", code, base)
    assert "请回复" in handle("wx-2", "   ", base)            # empty name re-asks
    handle("wx-2", "Bob", base)
    # already onboarded → defensive SAFE if it somehow reaches handle again
    assert handle("wx-2", "hi", base) == "系统暂时不可用，请稍后再试 🙏"


def test_two_racing_onboards_use_code_once(tmp_path):
    base = _base(tmp_path)
    code = InviteStore(base.shared_dir).create()
    # two different accounts race the same code through the full flow
    results = {}

    def run(acct):
        handle(acct, code, base)
        results[acct] = handle(acct, f"name-{acct}", base)

    threads = [threading.Thread(target=run, args=(f"wx-r{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    reg = UserRegistry(base.data_dir)
    onboarded = [a for a in ("wx-r0", "wx-r1") if reg.by_channel("weixin", a)]
    assert len(onboarded) == 1                               # code admitted exactly one


# ── identity gate ────────────────────────────────────────────────────

def test_onboarding_candidate_gate(tmp_path):
    base = _base(tmp_path)
    reg = UserRegistry(base.data_dir)
    reg.set_bridge_token("tok")
    reg.add_user("user0001")
    reg.bind_channel("user0001", "weixin", "wx-known")
    # valid: multi_tenant + good token + unknown weixin account
    assert onboarding_candidate("tok", {"account_id": "wx-unknown"}, base, reg) == "wx-unknown"
    # refused: bad token, bound account, other channel, no account
    assert onboarding_candidate("bad", {"account_id": "wx-unknown"}, base, reg) is None
    assert onboarding_candidate("tok", {"account_id": "wx-known"}, base, reg) is None
    assert onboarding_candidate("tok", {"account_id": "x", "channel": "email"}, base, reg) is None
    assert onboarding_candidate("tok", {}, base, reg) is None
    # off by config, and in single_user
    off = _base(tmp_path, self_onboarding=False)
    assert onboarding_candidate("tok", {"account_id": "wx-unknown"}, off, reg) is None
    single = Settings(_env_file=None, data_dir=base.data_dir)
    assert onboarding_candidate("tok", {"account_id": "wx-unknown"}, single, reg) is None


# ── admin invite ─────────────────────────────────────────────────────

def test_admin_invite_and_list(tmp_path):
    base = _base(tmp_path)
    out = admin.create_invite(base, ttl_days=3)
    assert "invite code" in out and "channels login" in out and "A.8" in out
    assert admin.list_invites(base).startswith("open ·")


# ── serve routing ────────────────────────────────────────────────────

BRIDGE = "bridge-secret-token"


@pytest.fixture
def mt_server(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_tenant")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    reg = UserRegistry(data_dir)
    reg.set_bridge_token(BRIDGE)
    code = InviteStore(Settings(_env_file=None).shared_dir).create()

    class FakeLLM:
        def complete_json(self, prompt, system=None, **kw):
            return {"reply": "ok", "actions": []}

    srv = make_server(settings_factory=lambda: Settings(_env_file=None),
                      llm_factory=lambda s: FakeLLM(), port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", data_dir, code
    srv.shutdown()


def _auth(tok=BRIDGE):
    return {"Authorization": f"Bearer {tok}"}


def test_serve_unknown_account_onboards_only_on_chat(mt_server):
    base, data_dir, code = mt_server
    # /chat from an unknown account → onboarding (200), not 401
    r = httpx.post(f"{base}/chat", headers=_auth(),
                   json={"account_id": "wx-fresh", "text": ""})
    assert r.status_code == 200 and "邀请码" in r.json()["reply"]
    # walk the flow through HTTP
    r = httpx.post(f"{base}/chat", headers=_auth(),
                   json={"account_id": "wx-fresh", "text": code})
    assert "请回复" in r.json()["reply"]
    r = httpx.post(f"{base}/chat", headers=_auth(),
                   json={"account_id": "wx-fresh", "text": "Fresh"})
    assert "欢迎" in r.json()["reply"]
    assert UserRegistry(data_dir).by_channel("weixin", "wx-fresh")
    # other routes for an unknown account stay fail-closed even with the token
    assert httpx.post(f"{base}/actions/list_todos", headers=_auth(),
                      json={"account_id": "wx-other"}).status_code == 401
    assert httpx.post(f"{base}/run", headers=_auth(),
                      json={"account_id": "wx-other"}).status_code == 401
    # and a bad bridge token never onboards
    assert httpx.post(f"{base}/chat", headers=_auth("wrong"),
                      json={"account_id": "wx-other", "text": "hi"}).status_code == 401
