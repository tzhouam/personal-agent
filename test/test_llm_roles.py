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
