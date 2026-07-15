"""Vendored tracer + its wiring into LLM.complete."""
import importlib


def _fresh(monkeypatch, enabled="1"):
    monkeypatch.setenv("AGENT_TRACE", enabled)
    import assistant.tracing as tracing
    importlib.reload(tracing)
    return tracing


def test_span_tree_and_inflight(monkeypatch, tmp_path):
    tr = _fresh(monkeypatch)
    path = tmp_path / "trace.jsonl"
    tr.init("run-1", path)
    with tr.span("phase", phase="research"):
        with tr.span("llm", model="m") as sp:
            sp.set(prompt_tokens=10)
    spans = {s["name"]: s for s in tr.load_spans(path)}
    assert spans["llm"]["parent"] == spans["phase"]["span_id"]
    assert spans["llm"]["attr"]["inflight"] == 1
    assert spans["phase"]["attr"]["phase"] == "research"


def test_disabled_noop(monkeypatch, tmp_path):
    tr = _fresh(monkeypatch, enabled="0")
    path = tmp_path / "t.jsonl"
    assert tr.init("r", path) is None
    with tr.span("llm", model="m") as sp:
        sp.set(x=1)
        sp.mark_ttft()
    assert not path.exists()


def test_llm_complete_records_span(monkeypatch, tmp_path, settings):
    tr = _fresh(monkeypatch)
    tr.init("run-llm", tmp_path / "trace.jsonl")
    from assistant.llm import LLM

    class Usage:
        input_tokens = 1200
        output_tokens = 34
        cache_read_input_tokens = 0

    class Resp:
        stop_reason = "end_turn"
        usage = Usage()

        class _B:
            type = "text"
            text = "hello"
        content = [_B()]

    llm = LLM(settings)
    monkeypatch.setattr(llm.client.messages, "create", lambda **kw: Resp())
    assert llm.complete("hi") == "hello"
    spans = [s for s in tr.load_spans(tmp_path / "trace.jsonl") if s["name"] == "llm"]
    assert len(spans) == 1
    assert spans[0]["attr"]["prompt_tokens"] == 1200
    assert spans[0]["attr"]["completion_tokens"] == 34
    assert spans[0]["attr"]["stop_reason"] == "end_turn"


def test_report_smoke(monkeypatch, tmp_path):
    tr = _fresh(monkeypatch)
    path = tmp_path / "t.jsonl"
    tr.init("r", path)
    with tr.span("phase", phase="deliver"):
        with tr.span("llm", model="m") as sp:
            tr.set_usage(sp, {"input_tokens": 500, "output_tokens": 10,
                              "cache_read_input_tokens": 0})
    assert "inference-engine view" in tr.report(path)


def test_tracer_is_context_scoped(monkeypatch, tmp_path):
    # Concurrent runs must not overwrite each other's tracer (§3) — a module
    # global would make interleaved inits clobber, so B's tracer would capture
    # A's spans. With a ContextVar, each context keeps its own.
    import contextvars

    tr = _fresh(monkeypatch)
    pa, pb = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    ctx_a, ctx_b = contextvars.copy_context(), contextvars.copy_context()

    def emit(who):
        with tr.span("phase", who=who):
            pass

    ctx_a.run(tr.init, "run-a", pa)      # install tracer A in ctx_a
    ctx_b.run(tr.init, "run-b", pb)      # install tracer B in ctx_b (would clobber a global)
    ctx_a.run(emit, "a")                 # ctx_a still sees tracer A
    ctx_b.run(emit, "b")                 # ctx_b sees tracer B

    a, b = tr.load_spans(pa), tr.load_spans(pb)
    assert [s["attr"]["who"] for s in a] == ["a"] and all(s["trace_id"] == "run-a" for s in a)
    assert [s["attr"]["who"] for s in b] == ["b"] and all(s["trace_id"] == "run-b" for s in b)


def test_moa_proposers_stay_traced(monkeypatch, tmp_path):
    # MoA proposers run in a thread pool; the ContextVar tracer must propagate
    # (llm.py copies the context per proposer) or their spans vanish.
    tr = _fresh(monkeypatch)
    from assistant.config import Settings
    import assistant.llm as llm_mod

    class Resp:
        def __init__(self, t):
            self.content = [type("B", (), {"type": "text", "text": t})()]
            self.stop_reason = "end_turn"; self.usage = None

    def make_client(**kwargs):
        class C:
            class messages:
                @staticmethod
                def create(**kw):
                    m = kw["model"]
                    return Resp("AGG" if m == "agg" else f"p-{m}")
        return C()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", make_client)
    path = tmp_path / "trace.jsonl"
    tr.init("run-moa", path)
    s = Settings(_env_file=None, anthropic_api_key="k", anthropic_model="d",
                 llm_mixture={"members": [{"model": "m1"}, {"model": "m2"}],
                              "aggregator": {"model": "agg"}, "roles": ["pipeline"]})
    assert llm_mod.LLM(s).complete("x", role="pipeline") == "AGG"
    models = {sp["attr"].get("model") for sp in tr.load_spans(path) if sp["name"] == "llm"}
    assert {"m1", "m2", "agg"} <= models          # both pool proposers + main-thread agg traced
