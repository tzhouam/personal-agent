"""Per-role model routing: role → (client, model), provider caching, fallback."""

import assistant.llm as llm_mod
from assistant.config import Settings
from assistant.llm import LLM


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
