"""Per-role model routing: role → (client, model), provider caching, fallback."""

import pytest

import assistant.llm as llm_mod
from assistant.config import Settings
from assistant.llm import LLM


@pytest.fixture(autouse=True)
def _scratch_data_dir(tmp_path, monkeypatch):
    """Every Settings built in this module points at a scratch data dir — the
    MoA metrics sink writes to events.db, and tests must never touch the live
    ~/.personal-agent (owner rule)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


def _settings(**kw):
    return Settings(_env_file=None, anthropic_api_key="def-key",
                    anthropic_base_url="https://default.example/anthropic",
                    anthropic_model="default-model",
                    anthropic_default_haiku_model="cheap-model", **kw)


def _fake_anthropic(monkeypatch):
    made = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            made.append(kwargs)

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", FakeClient)
    return made


def test_default_and_cheap_fallback(monkeypatch):
    made = _fake_anthropic(monkeypatch)
    llm = LLM(_settings())
    assert llm._resolve(None, None)[1] == "default-model"
    assert llm._resolve("chat", None)[1] == "default-model"      # unconfigured → default
    assert llm._resolve("research", None)[1] == "cheap-model"    # cheap-ish role
    assert llm._resolve("pipeline", "explicit")[1] == "explicit" # explicit model wins
    # all four resolved on the one default client — a single Anthropic build
    assert len(made) == 1


def test_role_routes_to_other_provider(monkeypatch):
    made = _fake_anthropic(monkeypatch)
    llm = LLM(_settings(llm_roles={
        "chat": {"model": "mimo-v2.5"},
        "research": {"model": "qwen3.6-plus",
                     "base_url": "https://dashscope.example/anthropic",
                     "api_key": "qwen-key"},
    }))
    # chat: different model, default provider (no url/key override)
    c_chat, m_chat = llm._resolve("chat", None)
    assert m_chat == "mimo-v2.5" and c_chat is llm.client
    # research: different model AND provider
    c_res, m_res = llm._resolve("research", None)
    assert m_res == "qwen3.6-plus" and c_res is not llm.client
    assert c_res.kwargs["base_url"] == "https://dashscope.example/anthropic"
    assert c_res.kwargs["api_key"] == "qwen-key"
    # a second research resolve reuses the cached client (no new build)
    before = len(made)
    llm._resolve("research", None)
    assert len(made) == before


def test_two_models_at_once(monkeypatch):
    _fake_anthropic(monkeypatch)
    llm = LLM(_settings(llm_roles={
        "chat": {"model": "mimo-v2.5"},
        "research": {"model": "qwen3.6-plus",
                     "base_url": "https://dashscope.example/anthropic",
                     "api_key": "qwen-key"}}))
    assert llm._resolve("chat", None)[1] == "mimo-v2.5"
    assert llm._resolve("research", None)[1] == "qwen3.6-plus"    # both live, different providers


def test_complete_uses_resolved_client(monkeypatch):
    _fake_anthropic(monkeypatch)
    captured = {}

    class Resp:
        content = [type("B", (), {"type": "text", "text": "ok"})()]
        stop_reason = "end_turn"
        usage = None

    llm = LLM(_settings(llm_roles={"chat": {"model": "mimo-v2.5"}}))
    def fake_create(**kw):
        captured.update(kw); return Resp()
    llm.client.messages = type("M", (), {"create": staticmethod(fake_create)})()
    out = llm.complete("hi", role="chat")
    assert out == "ok" and captured["model"] == "mimo-v2.5"


def test_mixture_proposes_and_aggregates(monkeypatch):
    # each model returns a tagged answer; the aggregator sees all proposals
    calls = []

    class Resp:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]
            self.stop_reason = "end_turn"; self.usage = None

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**kw):
                    model = kw["model"]
                    calls.append((model, kw["messages"][0]["content"]))
                    if model == "aggregator":
                        return Resp("SYNTHESIZED")
                    return Resp(f"answer-from-{model}")
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    llm = LLM(_settings(llm_mixture={
        "members": [{"model": "m1"}, {"model": "m2"}],
        "aggregator": {"model": "aggregator"},
        "roles": ["pipeline"]}))
    out = llm.complete("do the thing", role="pipeline")
    assert out == "SYNTHESIZED"
    proposers = [c for c in calls if c[0] in ("m1", "m2")]
    assert {c[0] for c in proposers} == {"m1", "m2"}          # both proposed
    agg = next(c for c in calls if c[0] == "aggregator")
    assert "answer-from-m1" in agg[1] and "answer-from-m2" in agg[1]  # saw both
    assert "Synthesize" in agg[1]


def test_mixture_only_for_configured_roles(monkeypatch):
    _fake_anthropic(monkeypatch)
    llm = LLM(_settings(llm_mixture={
        "members": [{"model": "m1"}, {"model": "m2"}], "roles": ["pipeline"]}))
    assert "chat" not in llm._mixture_roles     # chat not listed → single-model
    assert "pipeline" in llm._mixture_roles
    # a single member never triggers MoA
    llm2 = LLM(_settings(llm_mixture={"members": [{"model": "m1"}], "roles": ["pipeline"]}))
    assert llm2._mixture_roles == set()


def test_mixture_survives_one_dead_proposer(monkeypatch):
    class Resp:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]
            self.stop_reason = "end_turn"; self.usage = None

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**kw):
                    if kw["model"] == "dead":
                        raise RuntimeError("provider down")
                    if kw["model"] == "agg":
                        return Resp("OK")
                    return Resp("live-answer")
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    llm = LLM(_settings(llm_mixture={
        "members": [{"model": "dead"}, {"model": "live"}],
        "aggregator": {"model": "agg"}, "roles": ["pipeline"]}))
    assert llm.complete("x", role="pipeline") == "OK"   # degraded, not failed


def _mixture_client(behavior):
    """Fake Anthropic factory whose create() dispatches on model via `behavior`
    (model -> str answer, or a callable raising to simulate failure)."""
    calls = []

    class Resp:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]
            self.stop_reason = "end_turn"; self.usage = None

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**kw):
                    model = kw["model"]
                    calls.append((model, kw["messages"][0]["content"]))
                    out = behavior[model]
                    if callable(out):
                        return out()
                    return Resp(out)
        return C()
    return make_client, calls


def test_mixture_layers_refine(monkeypatch):
    # layers>1: each round's proposers must see the previous round's answers,
    # then a single final aggregation. Previously untested.
    make_client, calls = _mixture_client(
        {"m1": "ans-m1", "m2": "ans-m2", "agg": "FINAL"})
    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    llm = LLM(_settings(llm_mixture={
        "members": [{"model": "m1"}, {"model": "m2"}],
        "aggregator": {"model": "agg"}, "layers": 3, "roles": ["pipeline"]}))
    out = llm.complete("do it", role="pipeline")
    assert out == "FINAL"
    proposer_calls = [c for c in calls if c[0] in ("m1", "m2")]
    agg_calls = [c for c in calls if c[0] == "agg"]
    assert len(proposer_calls) == 6      # 2 members x 3 layers
    assert len(agg_calls) == 1           # one final synthesis
    # layers 2 & 3 (4 proposer calls) receive the prior answers to refine over
    refined = [c for c in proposer_calls if "Synthesize" in c[1]]
    assert len(refined) == 4


def test_mixture_survives_dead_aggregator(monkeypatch):
    # aggregator is not a single point of failure: if it dies after proposers
    # succeed, fall back to a proposer answer instead of raising.
    def boom():
        raise ValueError("aggregator auth failed")
    make_client, _ = _mixture_client(
        {"m1": "proposal-m1", "m2": "proposal-m2", "agg": boom})
    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    llm = LLM(_settings(llm_mixture={
        "members": [{"model": "m1"}, {"model": "m2"}],
        "aggregator": {"model": "agg"}, "roles": ["pipeline"]}))
    out = llm.complete("x", role="pipeline")
    assert out in ("proposal-m1", "proposal-m2")   # degraded to a proposal


def test_mixture_empty_aggregator_falls_back(monkeypatch):
    # a reasoning-model aggregator that emits only hidden thinking returns "";
    # don't hand back an empty MoA answer when a proposer succeeded.
    make_client, _ = _mixture_client(
        {"m1": "proposal-m1", "m2": "proposal-m2", "agg": "   "})
    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    llm = LLM(_settings(llm_mixture={
        "members": [{"model": "m1"}, {"model": "m2"}],
        "aggregator": {"model": "agg"}, "roles": ["pipeline"]}))
    out = llm.complete("x", role="pipeline")
    assert out in ("proposal-m1", "proposal-m2")   # not the empty aggregator output


def test_retry_lives_on_call_not_complete():
    # retry moved onto _call so each mixture proposer/aggregator retries
    # independently (a transient blip no longer silently drops a proposer, and
    # an aggregator retry doesn't re-run every proposer). tenacity attaches a
    # `.retry` controller to the wrapped function.
    assert hasattr(LLM._call, "retry")
    assert not hasattr(LLM.complete, "retry")


# ── temporal anchor: appended to the user-content TAIL of every call, never
# the system prompt, never before existing tokens (cache-safety) ────────────
from datetime import datetime, timedelta, timezone as _tz

_FROZEN = datetime(2026, 7, 17, 9, 32, tzinfo=_tz(timedelta(hours=8), "HKT"))


def _freeze_clock(monkeypatch):
    from assistant import timeutil
    monkeypatch.setattr(timeutil, "_now", lambda: _FROZEN)
    return timeutil.temporal_anchor()


class _Resp:
    content = [type("B", (), {"type": "text", "text": "ok"})()]
    stop_reason = "end_turn"
    usage = None


def test_anchor_appended_to_user_tail_never_system(monkeypatch):
    anchor = _freeze_clock(monkeypatch)
    _fake_anthropic(monkeypatch)
    captured = {}
    llm = LLM(_settings())
    llm.client.messages = type("M", (), {"create": staticmethod(
        lambda **kw: captured.update(kw) or _Resp())})()
    llm.complete("the stable long prompt", system="STATIC SYSTEM")
    content = captured["messages"][0]["content"]
    assert content.startswith("the stable long prompt")   # prefix byte-identical
    assert content.endswith(anchor)                       # anchor at the very tail
    assert content.count("[temporal anchor]") == 1
    assert "[temporal anchor]" not in captured["system"]  # static prefix untouched


def test_anchor_on_image_content_list(monkeypatch, tmp_path):
    anchor = _freeze_clock(monkeypatch)
    _fake_anthropic(monkeypatch)
    captured = {}
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"png-bytes")
    llm = LLM(_settings())
    llm.client.messages = type("M", (), {"create": staticmethod(
        lambda **kw: captured.update(kw) or _Resp())})()
    llm.complete("look at this", images=[str(pic)])
    blocks = captured["messages"][0]["content"]
    assert blocks[0]["type"] == "image"                            # order kept
    assert blocks[1] == {"type": "text", "text": "look at this"}
    assert blocks[-1] == {"type": "text", "text": anchor}


def test_anchor_never_mutates_shared_list_content(monkeypatch):
    # the mixture path hands ONE list to every proposer — an in-place append
    # would stack one anchor per call onto the shared prompt
    anchor = _freeze_clock(monkeypatch)
    _fake_anthropic(monkeypatch)
    seen = []
    llm = LLM(_settings())
    llm.client.messages = type("M", (), {"create": staticmethod(
        lambda **kw: seen.append(kw["messages"][0]["content"]) or _Resp())})()
    shared = [{"type": "text", "text": "prompt"}]
    llm._call(llm.client, "m", shared, None, 100)
    llm._call(llm.client, "m", shared, None, 100)
    assert shared == [{"type": "text", "text": "prompt"}]   # caller's list untouched
    for content in seen:
        anchors = [b for b in content if b == {"type": "text", "text": anchor}]
        assert len(anchors) == 1 and content[-1] == anchors[0]


def test_mixture_calls_each_carry_one_anchor(monkeypatch):
    _freeze_clock(monkeypatch)
    make_client, calls = _mixture_client({"m1": "a1", "m2": "a2", "agg": "FINAL"})
    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    llm = LLM(_settings(llm_mixture={
        "members": [{"model": "m1"}, {"model": "m2"}],
        "aggregator": {"model": "agg"}, "roles": ["pipeline"]}))
    assert llm.complete("go", role="pipeline") == "FINAL"
    assert len(calls) == 3                                  # m1 + m2 + aggregator
    for _model, content in calls:
        text = content if isinstance(content, str) else str(content)
        assert text.count("[temporal anchor]") == 1


def test_malformed_mixture_config_degrades(monkeypatch):
    # a broken LLM_MIXTURE/LLM_ROLES must degrade to {} — never crash Settings.
    # (classic cause: a multi-line value dotenv truncates to its first line.)
    s = _settings(llm_mixture='{"members":[{', llm_roles="not json at all")
    assert s.llm_mixture == {}
    assert s.llm_roles == {}
    # and a well-formed JSON string still parses (the env path, not kwargs)
    s2 = _settings(llm_mixture='{"members":[{"model":"a"},{"model":"b"}],"roles":["pipeline"]}')
    assert [m["model"] for m in s2.llm_mixture["members"]] == ["a", "b"]
    # a non-object JSON (e.g. a list) also degrades rather than mis-typing
    assert _settings(llm_roles="[1,2,3]").llm_roles == {}


def test_mixture_chat_abandons_slow_proposer(monkeypatch):
    """Chat latency bound: a proposer slower than moa_chat_proposer_timeout_s is
    abandoned once a proposal is in — a degraded provider can't stall the turn
    for minutes. Offline roles still wait for everyone."""
    import time

    class Resp:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]
            self.stop_reason = "end_turn"; self.usage = None

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**kw):
                    model = kw["model"]
                    if model == "slowpoke":
                        time.sleep(5)               # far past the 1s bound
                        return Resp("late answer")
                    if model == "aggregator":
                        return Resp("SYNTH:" + kw["messages"][0]["content"][-200:])
                    return Resp(f"answer-from-{model}")
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    settings = _settings(llm_mixture={
        "members": [{"model": "fastie"}, {"model": "slowpoke"}],
        "aggregator": {"model": "aggregator"},
        "roles": ["chat", "pipeline"]})
    settings = settings.model_copy(update={"moa_chat_proposer_timeout_s": 1})
    llm = LLM(settings)

    t0 = time.monotonic()
    out = llm.complete("quick question", role="chat")
    took = time.monotonic() - t0
    assert took < 4                                  # did NOT wait for slowpoke
    assert "answer-from-fastie" in out               # aggregated the survivor
    assert "late answer" not in out

    # offline role: same mixture waits for every proposer (no bound applied)
    t0 = time.monotonic()
    out = llm.complete("batch job", role="pipeline")
    assert time.monotonic() - t0 >= 5                # waited for slowpoke
    assert "late answer" in out or "answer-from-fastie" in out


# ── provider circuit breaker + all-fail fallback (MoA resilience) ─────────

import pytest


@pytest.fixture(autouse=True)
def _clean_breaker():
    llm_mod._reset_breaker()
    yield
    llm_mod._reset_breaker()


class _Err(Exception):
    """Stub API error carrying a status_code (non-retryable → fast tests)."""
    def __init__(self, status=None, msg="boom"):
        super().__init__(msg)
        if status is not None:
            self.status_code = status


def _reset400():
    return _Err(400, "recvAddress(..) failed: Connection reset by peer")


def _scripted(monkeypatch, behavior, calls):
    """Fake Anthropic client whose per-model behavior comes from `behavior`:
    model → callable(kw) returning text (or raising)."""
    class Resp:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]
            self.stop_reason = "end_turn"; self.usage = None

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**kw):
                    calls.append(kw["model"])
                    return Resp(behavior[kw["model"]](kw))
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)


def _mix_settings(**kw):
    return _settings(llm_mixture={
        "members": [{"model": "m1", "base_url": "https://prov-a/x", "api_key": "ka"},
                    {"model": "m2", "base_url": "https://prov-b/x", "api_key": "kb"}],
        "aggregator": {"model": "agg", "base_url": "https://prov-c/x", "api_key": "kc"},
        "roles": ["pipeline"]}, **kw)


def _raise(exc):
    def f(kw):
        raise exc
    return f


def test_classify_failure_scopes():
    import httpx
    assert llm_mod._classify_failure(_Err(429)) == "prov"
    assert llm_mod._classify_failure(_Err(401)) == "prov"
    assert llm_mod._classify_failure(_reset400()) == "prov"
    assert llm_mod._classify_failure(_Err(500)) == "model"
    assert llm_mod._classify_failure(_Err(400, "invalid param")) is None
    assert llm_mod._classify_failure(TypeError("bug")) is None
    req = httpx.Request("POST", "https://x")
    assert llm_mod._classify_failure(
        llm_mod.anthropic.APIConnectionError(request=req)) == "prov"


def test_allfail_falls_back_to_aggregator(monkeypatch):
    calls = []
    _scripted(monkeypatch, {"m1": _raise(_reset400()), "m2": _raise(_reset400()),
                            "agg": lambda kw: "AGG-DIRECT"}, calls)
    out = LLM(_mix_settings()).complete("q", role="pipeline")
    assert out == "AGG-DIRECT"                      # no RuntimeError
    assert calls.count("agg") == 1


def test_fallback_chain_role_then_default(monkeypatch):
    calls = []
    _scripted(monkeypatch, {
        "m1": _raise(_reset400()), "m2": _raise(_reset400()),
        "agg": lambda kw: "",                        # blank = failed fallback
        "role-model": lambda kw: "ROLE-ANSWER",
        "default-model": lambda kw: "unused"}, calls)
    llm = LLM(_mix_settings(llm_roles={"pipeline": {
        "model": "role-model", "base_url": "https://prov-d/x", "api_key": "kd"}}))
    assert llm.complete("q", role="pipeline") == "ROLE-ANSWER"
    assert calls.count("agg") == 1                  # tried, blank, moved on


def test_fallback_dedupes_aggregator_sharing_member_route(monkeypatch):
    calls = []
    _scripted(monkeypatch, {"m1": _raise(_reset400()), "m2": _raise(_reset400()),
                            "default-model": lambda kw: "DEFAULT-ANSWER"}, calls)
    # no explicit aggregator → agg IS members[0]; its route already failed
    llm = LLM(_settings(llm_mixture={
        "members": [{"model": "m1", "base_url": "https://prov-a/x", "api_key": "ka"},
                    {"model": "m2", "base_url": "https://prov-b/x", "api_key": "kb"}],
        "roles": ["pipeline"]}))
    assert llm.complete("q", role="pipeline") == "DEFAULT-ANSWER"
    assert calls.count("m1") == 1                   # never re-attempted as aggregator


def test_role_route_overlapping_failed_member_skipped(monkeypatch):
    calls = []
    _scripted(monkeypatch, {"m1": _raise(_reset400()), "m2": _raise(_reset400()),
                            "agg": _raise(_reset400()),
                            "default-model": lambda kw: "DEFAULT-ANSWER"}, calls)
    # role route == m2's exact route → must be skipped in the chain
    llm = LLM(_mix_settings(llm_roles={"pipeline": {
        "model": "m2", "base_url": "https://prov-b/x", "api_key": "kb"}}))
    assert llm.complete("q", role="pipeline") == "DEFAULT-ANSWER"
    assert calls.count("m2") == 1


def test_breaker_skips_sick_member_after_threshold(monkeypatch):
    calls = []
    _scripted(monkeypatch, {"m1": lambda kw: "answer-from-m1",
                            "m2": _raise(_reset400()),
                            "agg": lambda kw: "SYNTH"}, calls)
    for turn in range(3):                            # fresh LLM per turn (per-request)
        out = LLM(_mix_settings()).complete("q", role="pipeline")
        assert out == "SYNTH"
    # threshold=2: turns 1+2 attempted m2, turn 3 skipped it
    assert calls.count("m2") == 2
    assert calls.count("m1") == 3


def test_cross_model_provider_suppression(monkeypatch):
    calls = []
    _scripted(monkeypatch, {"m1": lambda kw: "answer-from-m1",
                            "m2": _raise(_reset400()),
                            "m3": lambda kw: "answer-from-m3",
                            "m4": lambda kw: "answer-from-m4",
                            "agg": lambda kw: "SYNTH"}, calls)
    for _ in range(2):                               # open prov-b (m2's provider)
        LLM(_mix_settings()).complete("q", role="pipeline")
    # m3 = DIFFERENT model on the same provider+credential → suppressed;
    # m4 = same provider, DIFFERENT credential → not suppressed
    llm = LLM(_settings(llm_mixture={
        "members": [{"model": "m3", "base_url": "https://prov-b/x", "api_key": "kb"},
                    {"model": "m4", "base_url": "https://prov-b/x", "api_key": "OTHER"}],
        "aggregator": {"model": "agg", "base_url": "https://prov-c/x", "api_key": "kc"},
        "roles": ["pipeline"]}))
    assert llm.complete("q", role="pipeline") == "SYNTH"
    assert calls.count("m3") == 0                    # cross-model suppression
    assert calls.count("m4") == 1                    # other tenant unaffected


def test_call_local_provider_dedupe_in_fallback(monkeypatch):
    calls = []
    # aggregator = different model on m2's provider+credential; m1+m2 die with
    # provider-scoped failures → the chain must NOT try agg on the dead provider
    _scripted(monkeypatch, {"m1": _raise(_reset400()), "m2": _raise(_reset400()),
                            "agg-on-b": lambda kw: "should not run",
                            "default-model": lambda kw: "DEFAULT-ANSWER"}, calls)
    llm = LLM(_settings(llm_mixture={
        "members": [{"model": "m1", "base_url": "https://prov-a/x", "api_key": "ka"},
                    {"model": "m2", "base_url": "https://prov-b/x", "api_key": "kb"}],
        "aggregator": {"model": "agg-on-b", "base_url": "https://prov-b/x",
                       "api_key": "kb"},
        "roles": ["pipeline"]}))
    assert llm.complete("q", role="pipeline") == "DEFAULT-ANSWER"
    assert calls.count("agg-on-b") == 0              # no fresh window on dead endpoint


def test_multilayer_keeps_prior_proposals_on_later_failure(monkeypatch):
    calls = []
    seen = {"m1": 0}

    def m1(kw):
        seen["m1"] += 1
        if seen["m1"] > 1:
            raise _reset400()
        return "L1-ANSWER"

    _scripted(monkeypatch, {"m1": m1, "m2": _raise(_reset400()),
                            "agg": lambda kw: "SYNTH:" + kw["messages"][0]["content"][-300:]},
              calls)
    llm = LLM(_settings(llm_mixture={
        "members": [{"model": "m1", "base_url": "https://prov-a/x", "api_key": "ka"},
                    {"model": "m2", "base_url": "https://prov-b/x", "api_key": "kb"}],
        "aggregator": {"model": "agg", "base_url": "https://prov-c/x", "api_key": "kc"},
        "layers": 2, "roles": ["pipeline"]}))
    out = llm.complete("q", role="pipeline")
    assert out.startswith("SYNTH:") and "L1-ANSWER" in out   # layer-1 retained


def test_fail_fast_when_everything_cooling(monkeypatch):
    calls = []
    _scripted(monkeypatch, {"m1": _raise(_reset400()), "m2": _raise(_reset400()),
                            "agg": _raise(_reset400()),
                            "default-model": _raise(_reset400())}, calls)
    s = _mix_settings()
    for _ in range(2):                               # open every route
        with pytest.raises(RuntimeError):
            LLM(s).complete("q", role="pipeline")
    calls.clear()
    with pytest.raises(RuntimeError):                # third call: zero attempts
        LLM(s).complete("q", role="pipeline")
    assert calls == []


def test_probe_lease_and_stale_gen_units():
    cooldown = 180
    scopes = llm_mod._route_scopes(_settings(), "https://prov-z/x", "kz", "mz")
    # open both scopes
    for _ in range(2):
        mode, gens, claimed = llm_mod._breaker_check(scopes, cooldown)
        llm_mod._breaker_record(scopes, gens, claimed, "prov", 2, cooldown)
        llm_mod._breaker_record(scopes, gens, claimed, "model", 2, cooldown)
    assert llm_mod._breaker_check(scopes, cooldown)[0] == "open"
    # force expiry → exactly one probe admitted
    with llm_mod._BREAKER_LOCK:
        for e in llm_mod._BREAKER.values():
            e["until"] = 0.0
    mode1, gens1, claimed1 = llm_mod._breaker_check(scopes, cooldown)
    mode2, _, _ = llm_mod._breaker_check(scopes, cooldown)
    assert mode1 == "probe" and mode2 == "open"      # single admission
    # neutral outcome releases the lease → a new probe is possible
    llm_mod._breaker_record(scopes, gens1, claimed1, None, 2, cooldown)
    mode3, gens3, claimed3 = llm_mod._breaker_check(scopes, cooldown)
    assert mode3 == "probe"
    # STALE success (older gen) must not close the current open state
    stale_gens = {k: g - 1 for k, g in gens3.items()}
    llm_mod._breaker_record(scopes, stale_gens, frozenset(), "ok", 2, cooldown)
    assert llm_mod._breaker_check(scopes, cooldown)[0] == "open"  # lease3 held
    # a REAL probe success closes it
    llm_mod._breaker_record(scopes, gens3, claimed3, "ok", 2, cooldown)
    assert llm_mod._breaker_check(scopes, cooldown)[0] == "closed"


# ── MoA observability: stage-tagged spans + the durable moa metrics row ──────

class _MoaResp:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = "end_turn"; self.usage = None


def _moa_rows(settings):
    from assistant.events_store import EventsStore

    events = EventsStore(settings.events_db)
    rows = [r for r in events.metrics_window(1) if r["step"] == "moa"]
    events.close()
    return {r["name"]: r["value"] for r in rows[-6:]}


def test_mixture_observability_spans_and_durable_metrics(monkeypatch, tmp_path):
    from assistant import tracing

    llm_mod._reset_breaker()

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**kw):
                    if kw["model"] == "aggregator":
                        return _MoaResp("SYNTHESIZED")
                    return _MoaResp(f"answer-from-{kw['model']}")
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    settings = _settings(llm_mixture={
        "members": [{"model": "m1"}, {"model": "m2"}],
        "aggregator": {"model": "aggregator"}, "roles": ["pipeline"]})
    token = tracing._default.set(None)
    try:
        tracing.init("moa-test", tmp_path / "trace.jsonl")
        assert LLM(settings).complete("go", role="pipeline") == "SYNTHESIZED"
    finally:
        tracing._default.reset(token)

    spans = tracing.load_spans(tmp_path / "trace.jsonl")
    stages = [s["attr"].get("mixture_stage") for s in spans if s["name"] == "llm"]
    assert stages.count("proposer") == 2 and stages.count("aggregator") == 1
    mix = next(s for s in spans if s["name"] == "mixture")
    assert mix["attr"]["members_total"] == 2
    assert mix["attr"]["proposals_ok"] == 2
    assert mix["attr"]["aggregator_ok"] == 1
    assert mix["attr"]["fallback_used"] == 0
    # the durable numeric row lands even without any tracer (chat turns)
    moa = _moa_rows(settings)
    assert moa["proposals_ok"] == 2 and moa["aggregator_ok"] == 1


def test_mixture_metrics_count_dead_proposer(monkeypatch):
    llm_mod._reset_breaker()

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**kw):
                    if kw["model"] == "deadbeat":
                        raise RuntimeError("boom")
                    if kw["model"] == "aggregator":
                        return _MoaResp("SYNTH")
                    return _MoaResp("survivor answer")
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    settings = _settings(llm_mixture={
        "members": [{"model": "deadbeat"}, {"model": "ok-model"}],
        "aggregator": {"model": "aggregator"}, "roles": ["pipeline"]})
    assert LLM(settings).complete("go", role="pipeline") == "SYNTH"
    moa = _moa_rows(settings)
    assert moa["members_total"] == 2 and moa["proposals_ok"] == 1
    assert moa["aggregator_ok"] == 1


def test_mixture_metrics_record_fallback(monkeypatch):
    llm_mod._reset_breaker()

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**kw):
                    if kw["model"] == "aggregator":
                        return _MoaResp("fallback answer")
                    raise RuntimeError("all proposers down")
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    settings = _settings(llm_mixture={
        "members": [{"model": "p1"}, {"model": "p2"}],
        "aggregator": {"model": "aggregator"}, "roles": ["pipeline"]})
    assert LLM(settings).complete("go", role="pipeline") == "fallback answer"
    moa = _moa_rows(settings)
    assert moa["proposals_ok"] == 0 and moa["fallback_used"] == 1
    assert moa["aggregator_ok"] == 0
    llm_mod._reset_breaker()
