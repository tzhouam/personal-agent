"""Per-role model routing: role → (client, model), provider caching, fallback."""

import pytest

import assistant.platform.llm as llm_mod
from assistant.platform.config import Settings
from assistant.platform.llm import LLM


@pytest.fixture(autouse=True)
def _scratch_data_dir(tmp_path, monkeypatch):
    """Every Settings built in this module points at a scratch data dir — the
    MoA metrics sink writes to events.db, and tests must never touch the live
    ~/.personal-agent (owner rule)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(llm_mod, "_sleep", lambda _seconds: None)


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


def test_single_member_config_stays_single_model(monkeypatch):
    """A one-member config is routing data, not an implicit MoA invocation."""
    calls = []
    metrics = []

    class Resp:
        content = [type("B", (), {"type": "text", "text": "single"})()]
        stop_reason = "end_turn"
        usage = None

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**kw):
                    calls.append(kw["model"])
                    return Resp()
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    settings = _settings(llm_mixture={
        "members": [{"model": "only-member"}], "roles": ["pipeline"]})
    llm = LLM(settings, metrics_sink=lambda *args: metrics.append(args))
    assert llm.complete("x", role="pipeline") == "single"
    assert calls == ["default-model"]
    assert metrics == []


@pytest.mark.parametrize("mixture", [
    {"members": 1, "roles": 1, "aggregator": 1, "layers": {"bad": True}},
    {"members": {"model": "not-a-list"}, "roles": "pipeline"},
    {"members": [None, {"model": 7}, {"model": "only-valid"}],
     "aggregator": ["bad"], "layers": "not-an-int"},
])
def test_malformed_mixture_runtime_disables_moa(monkeypatch, mixture):
    """Valid JSON with invalid shapes degrades to one default-model call."""
    calls = []

    class Resp:
        content = [type("B", (), {"type": "text", "text": "single"})()]
        stop_reason = "end_turn"
        usage = None

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**request):
                    calls.append(request["model"])
                    return Resp()
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    llm = LLM(_settings(llm_mixture=mixture))
    assert llm._mixture_roles == set()
    assert llm.complete("x", role="pipeline") == "single"
    assert calls == ["default-model"]


def test_malformed_mixture_fields_filter_and_default_safely(monkeypatch):
    """Two valid members survive bad peers; roles/agg/layers use safe defaults."""
    calls = []

    class Resp:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]
            self.stop_reason = "end_turn"
            self.usage = None

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**request):
                    model = request["model"]
                    content = request["messages"][0]["content"]
                    calls.append(model)
                    if model == "m1" and "[Reference answers]" in content:
                        return Resp("SYNTH")
                    return Resp(f"proposal-{model}")
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    llm = LLM(_settings(llm_mixture={
        "members": [
            {"model": " m1 "}, {"model": 2},
            {"model": "bad-route", "base_url": 3}, {"model": "m2"}],
        "aggregator": 7, "roles": 1, "layers": {"bad": True}}))

    assert [member["model"] for member in llm.mixture["members"]] == ["m1", "m2"]
    assert llm.mixture["aggregator"]["model"] == "m1"
    assert llm.mixture["roles"] == ["pipeline", "research", "task", "evolve"]
    assert llm.mixture["layers"] == 1
    assert llm.complete("x", role="pipeline") == "SYNTH"
    assert calls.count("m1") == 2 and calls.count("m2") == 1


def test_canonical_duplicate_mixture_routes_do_not_enable_moa(monkeypatch):
    """The same URL/key/model twice is one proposer, not false independence."""
    calls = []

    class Resp:
        content = [type("B", (), {"type": "text", "text": "single"})()]
        stop_reason = "end_turn"
        usage = None

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**request):
                    calls.append(request["model"])
                    return Resp()
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    llm = LLM(_settings(llm_mixture={
        "members": [
            {"model": "same", "base_url": "https://ROUTE.example:443/api/",
             "api_key": "same-key"},
            {"model": "same", "base_url": "https://route.example/api",
             "api_key": "same-key"}],
        "roles": ["pipeline"]}))

    assert [member["model"] for member in llm.mixture["members"]] == ["same"]
    assert llm._mixture_roles == set()
    assert llm.complete("x", role="pipeline") == "single"
    assert calls == ["default-model"]


def test_same_mixture_route_with_different_credentials_stays_independent(
        monkeypatch):
    """Credential fingerprints keep otherwise identical proposers distinct."""
    calls = []

    class Resp:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]
            self.stop_reason = "end_turn"
            self.usage = None

    def make_client(**client_kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**request):
                    calls.append((client_kwargs["api_key"], request["model"]))
                    return Resp("SYNTH" if request["model"] == "agg" else "proposal")
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    llm = LLM(_settings(llm_mixture={
        "members": [
            {"model": "same", "base_url": "https://route.example/api",
             "api_key": "key-one"},
            {"model": "same", "base_url": "https://route.example/api",
             "api_key": "key-two"}],
        "aggregator": {"model": "agg", "api_key": "agg-key"},
        "roles": ["pipeline"]}))

    assert len(llm.mixture["members"]) == 2
    assert llm.complete("x", role="pipeline") == "SYNTH"
    assert ("key-one", "same") in calls and ("key-two", "same") in calls


def test_request_timeouts_are_role_bounded(monkeypatch):
    """Chat uses 45s while offline calls and SDK defaults use 120s."""
    requests = []
    clients = []

    class Resp:
        content = [type("B", (), {"type": "text", "text": "ok"})()]
        stop_reason = "end_turn"
        usage = None

    def make_client(**client_kwargs):
        clients.append(client_kwargs)

        class C:
            class messages:
                @staticmethod
                def create(**request):
                    requests.append(request)
                    return Resp()
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    llm = LLM(_settings())
    assert llm.complete("chat", role="chat", mixture=False) == "ok"
    assert llm.complete("batch", role="pipeline", mixture=False) == "ok"
    assert llm.force_probe("doctor") == "ok"

    assert clients[0]["timeout"] == 120 and clients[0]["max_retries"] == 0
    assert [request["timeout"] for request in requests] == [45, 120, 45]


def test_all_hung_chat_mixture_returns_at_latency_bound(monkeypatch):
    """No-success proposer pool cannot wait unbounded for FIRST_COMPLETED."""
    import threading
    import time

    release = threading.Event()

    class Resp:
        content = [type("B", (), {"type": "text", "text": "late"})()]
        stop_reason = "end_turn"
        usage = None

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**request):
                    release.wait(2)
                    return Resp()
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    monkeypatch.setattr(llm_mod, "_INTERACTIVE_TIMEOUT_S", 0.05)
    llm = LLM(_settings(
        moa_chat_proposer_timeout_s=10,
        llm_mixture={"members": [{"model": "m1"}, {"model": "m2"}],
                     "aggregator": {"model": "agg"}, "roles": ["chat"]}))

    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="latency bound"):
            llm.complete("x", role="chat")
        assert time.monotonic() - started < 0.5
    finally:
        release.set()


def test_zero_chat_proposer_timeout_preserves_wait_for_all(monkeypatch):
    """Configured zero disables only the outer MoA cutoff, as documented."""
    import time

    calls = []

    class Resp:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]
            self.stop_reason = "end_turn"
            self.usage = None

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**request):
                    model = request["model"]
                    calls.append(model)
                    if model == "slow":
                        time.sleep(0.05)
                    return Resp("SYNTH" if model == "agg" else f"proposal-{model}")
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    # A regression that translated configured 0 into this outer timeout would
    # abandon ``slow`` and skip synthesis. Per-request fakes ignore the value.
    monkeypatch.setattr(llm_mod, "_INTERACTIVE_TIMEOUT_S", 0.01)
    llm = LLM(_settings(
        moa_chat_proposer_timeout_s=0,
        llm_mixture={"members": [{"model": "fast"}, {"model": "slow"}],
                     "aggregator": {"model": "agg"}, "roles": ["chat"]}))

    assert llm.complete("x", role="chat") == "SYNTH"
    assert set(calls) == {"fast", "slow", "agg"}


def test_later_chat_layer_timeout_keeps_prior_proposals(monkeypatch):
    """Timed-out refinements cannot erase complete first-layer evidence."""
    import threading

    release = threading.Event()
    calls = []

    class Resp:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]
            self.stop_reason = "end_turn"
            self.usage = None

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**request):
                    model = request["model"]
                    content = request["messages"][0]["content"]
                    calls.append(model)
                    if model in {"m1", "m2"} and "[Reference answers]" in content:
                        release.wait(2)
                        return Resp(f"late-{model}")
                    return Resp("SYNTH" if model == "agg" else f"first-{model}")
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    monkeypatch.setattr(llm_mod, "_INTERACTIVE_TIMEOUT_S", 0.05)
    llm = LLM(_settings(
        moa_chat_proposer_timeout_s=10,
        llm_mixture={"members": [{"model": "m1"}, {"model": "m2"}],
                     "aggregator": {"model": "agg"}, "layers": 2,
                     "roles": ["chat"]}))

    try:
        assert llm.complete("x", role="chat") == "SYNTH"
        assert "agg" in calls
    finally:
        release.set()


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
    # One surviving proposer is returned directly: synthesis requires two
    # independent healthy answers and must not manufacture consensus.
    assert llm.complete("x", role="pipeline") == "live-answer"


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


def test_retry_policy_lives_on_call_not_complete():
    # Platform-owned retry stays on each underlying proposer/aggregator call;
    # there is no SDK/tenacity wrapper that can multiply attempts invisibly.
    assert llm_mod._RETRY_DELAYS_S == (1, 4)
    assert not hasattr(LLM._call, "retry")
    assert not hasattr(LLM.complete, "retry")


# ── temporal anchor: appended to the user-content TAIL of every call, never
# the system prompt, never before existing tokens (cache-safety) ────────────
from datetime import datetime, timedelta, timezone as _tz

_FROZEN = datetime(2026, 7, 17, 9, 32, tzinfo=_tz(timedelta(hours=8), "HKT"))


def _freeze_clock(monkeypatch):
    from assistant.platform import timeutil
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


def _connection_error():
    """Transport failure whose message is deliberately unsafe to persist."""
    import httpx

    return httpx.ConnectError(
        "PRIVATE_EXCEPTION_MESSAGE",
        request=httpx.Request("POST", "https://provider.example/v1/messages"))


def _unknown_error():
    """Unclassified failure with a non-integer status-like value."""
    exc = RuntimeError("PRIVATE_EXCEPTION_MESSAGE")
    exc.status_code = "PRIVATE_STATUS_TEXT"
    return exc


def _unsafe_named_error():
    """An exception class name is metadata too and may be attacker-controlled."""
    unsafe = type("https://PRIVATE_API_KEY\n" + "x" * 100,
                  (RuntimeError,), {})
    return unsafe("PRIVATE_EXCEPTION_MESSAGE")


@pytest.mark.parametrize("factory, error_type, status, classification", [
    (lambda: _Err(401, "PRIVATE_EXCEPTION_MESSAGE"), "_Err", 401, "prov"),
    (lambda: _Err(403, "PRIVATE_EXCEPTION_MESSAGE"), "_Err", 403, "model"),
    (lambda: _Err(429, "PRIVATE_EXCEPTION_MESSAGE"), "_Err", 429, "prov"),
    (lambda: _Err(503, "PRIVATE_EXCEPTION_MESSAGE"), "_Err", 503, "model"),
    (_connection_error, "ConnectError", None, "prov"),
    (_unknown_error, "RuntimeError", None, "none"),
    (_unsafe_named_error, "redacted_error", None, "none"),
])
def test_failed_call_span_has_safe_bounded_metadata(
        monkeypatch, tmp_path, factory, error_type, status, classification):
    """Failed spans expose routing health without owner/provider payloads."""
    from assistant.platform import tracing

    _fake_anthropic(monkeypatch)
    settings = _settings()
    llm = LLM(settings)
    exc = factory()
    exc.response_body = "PRIVATE_RESPONSE_BODY"

    class Client:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise exc

    trace_path = tmp_path / "failed-call.jsonl"
    token = tracing._default.set(None)
    try:
        tracing.init("failed-call", trace_path)
        with pytest.raises(type(exc)):
            llm._call(Client(), "safe-model", "PRIVATE_PROMPT",
                      "PRIVATE_SYSTEM", 100)
    finally:
        tracing._default.reset(token)

    spans = [s for s in tracing.load_spans(trace_path) if s["name"] == "llm"]
    expected_attempts = 3 if llm_mod._is_transient(exc) else 1
    assert len(spans) == expected_attempts
    for span in spans:
        attrs = span["attr"]
        assert attrs["error_type"] == error_type
        assert attrs["breaker_classification"] == classification
        if status is None:
            assert "status_code" not in attrs
        else:
            assert attrs["status_code"] == status
            assert isinstance(attrs["status_code"], int)

    serialized = trace_path.read_text(encoding="utf-8")
    for forbidden in ("PRIVATE_EXCEPTION_MESSAGE", "PRIVATE_RESPONSE_BODY",
                      "PRIVATE_STATUS_TEXT", "PRIVATE_PROMPT", "PRIVATE_SYSTEM",
                      "PRIVATE_API_KEY", "https://", settings.anthropic_api_key):
        assert forbidden not in serialized


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
        assert out == "answer-from-m1"
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
    assert llm.complete("q", role="pipeline") == "answer-from-m4"
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
    assert out == "L1-ANSWER"                    # retained, but not synthesized alone


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
    from assistant.agent.events_store import EventsStore

    events = EventsStore(settings.events_db)
    rows = [r for r in events.metrics_window(1) if r["step"] == "moa"]
    events.close()
    return {r["name"]: r["value"] for r in rows}


def test_mixture_observability_spans_and_durable_metrics(monkeypatch, tmp_path):
    from assistant.platform import tracing

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
    assert mix["attr"]["members_attempted"] == 2
    assert mix["attr"]["members_skipped"] == 0
    assert mix["attr"]["members_failed"] == 0
    assert mix["attr"]["proposals_ok"] == 2
    assert mix["attr"]["proposals_final"] == 2
    assert mix["attr"]["aggregator_attempted"] == 1
    assert mix["attr"]["aggregator_skipped"] == 0
    assert mix["attr"]["aggregator_failed"] == 0
    assert mix["attr"]["aggregator_ok"] == 1
    assert mix["attr"]["fallback_used"] == 0
    assert mix["attr"]["degraded"] == 0
    # the durable numeric row lands even without any tracer (chat turns)
    moa = _moa_rows(settings)
    for name, value in mix["attr"].items():
        if name not in {"role"}:
            assert moa[name] == value


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
    assert LLM(settings).complete("go", role="pipeline") == "survivor answer"
    moa = _moa_rows(settings)
    assert moa["members_total"] == 2
    assert moa["members_attempted"] == 2
    assert moa["members_skipped"] == 0 and moa["members_failed"] == 1
    assert moa["proposals_ok"] == 1 and moa["proposals_final"] == 1
    assert moa["aggregator_attempted"] == 0
    assert moa["aggregator_skipped"] == 1 and moa["aggregator_failed"] == 0
    assert moa["aggregator_ok"] == 0 and moa["fallback_used"] == 1
    assert moa["degraded"] == 1


def test_mixture_metrics_count_breaker_skipped_member(monkeypatch):
    """A cooling proposer is skipped, not attempted or failed this call."""
    calls = []
    _scripted(monkeypatch, {
        "m1": lambda kw: "survivor", "m2": lambda kw: "must-not-run",
        "agg": lambda kw: "SYNTH"}, calls)
    settings = _settings(llm_mixture={
        "members": [{"model": "m1"}, {"model": "m2"}],
        "aggregator": {"model": "agg"}, "roles": ["pipeline"]})
    scopes = llm_mod._route_scopes(settings, None, None, "m2")
    for _ in range(settings.moa_member_fail_threshold):
        mode, gens, claimed = llm_mod._breaker_check(
            scopes, settings.moa_member_cooldown_s)
        llm_mod._breaker_record(
            scopes, gens, claimed, "model", settings.moa_member_fail_threshold,
            settings.moa_member_cooldown_s)

    assert LLM(settings).complete("go", role="pipeline") == "survivor"
    assert "m2" not in calls
    moa = _moa_rows(settings)
    assert moa["members_attempted"] == 1
    assert moa["members_skipped"] == 1 and moa["members_failed"] == 0
    assert moa["proposals_ok"] == 1 and moa["proposals_final"] == 1
    assert moa["aggregator_attempted"] == 0 and moa["aggregator_skipped"] == 1
    assert moa["aggregator_ok"] == 0 and moa["fallback_used"] == 1
    assert moa["degraded"] == 1


def test_mixture_degraded_uses_final_layer_proposal_count(monkeypatch):
    """Earlier successes cannot hide a one-proposal final synthesis layer."""
    seen = {"m2": 0}

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**kw):
                    model = kw["model"]
                    if model == "m2":
                        seen["m2"] += 1
                        if seen["m2"] == 2:
                            raise RuntimeError("second layer failed")
                    if model == "aggregator":
                        return _MoaResp("SYNTH")
                    return _MoaResp(f"proposal-{model}")
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    settings = _settings(llm_mixture={
        "members": [{"model": "m1"}, {"model": "m2"}],
        "aggregator": {"model": "aggregator"},
        "layers": 2, "roles": ["pipeline"]})
    assert LLM(settings).complete("go", role="pipeline") == "proposal-m1"
    moa = _moa_rows(settings)
    assert moa["proposals_ok"] == 3       # two in layer 1, one in layer 2
    assert moa["proposals_final"] == 1    # only this set reached synthesis
    assert moa["aggregator_attempted"] == 0
    assert moa["aggregator_skipped"] == 1 and moa["aggregator_ok"] == 0
    assert moa["fallback_used"] == 1
    assert moa["degraded"] == 1


def test_mixture_metrics_count_failed_aggregator(monkeypatch):
    """A failed synthesis is observable even when a proposal is returned."""
    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**kw):
                    if kw["model"] == "aggregator":
                        raise RuntimeError("aggregator unavailable")
                    return _MoaResp(f"proposal-{kw['model']}")
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    settings = _settings(llm_mixture={
        "members": [{"model": "m1"}, {"model": "m2"}],
        "aggregator": {"model": "aggregator"}, "roles": ["pipeline"]})
    assert LLM(settings).complete("go", role="pipeline").startswith("proposal-")
    moa = _moa_rows(settings)
    assert moa["members_attempted"] == 2 and moa["proposals_ok"] == 2
    assert moa["proposals_final"] == 2
    assert moa["aggregator_attempted"] == 1
    assert moa["aggregator_skipped"] == 0 and moa["aggregator_failed"] == 1
    assert moa["aggregator_ok"] == 0 and moa["fallback_used"] == 1
    assert moa["degraded"] == 1


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
    assert moa["members_attempted"] == 2
    assert moa["members_skipped"] == 0 and moa["members_failed"] == 2
    assert moa["proposals_ok"] == 0 and moa["proposals_final"] == 0
    assert moa["fallback_used"] == 1
    assert moa["aggregator_attempted"] == 0
    assert moa["aggregator_skipped"] == 1 and moa["aggregator_failed"] == 0
    assert moa["aggregator_ok"] == 0 and moa["degraded"] == 1
    llm_mod._reset_breaker()
