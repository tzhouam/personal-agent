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
