from assistant import search as search_mod
from assistant.actions import run_action
from assistant.search import (_real_url, fetch_page, format_results, web_search,
                              web_search_answer)

_DDG_PAGE = """<html><body><table>
<tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x" class='result-link'>First &amp; Best</a></td></tr>
<tr><td class='result-snippet'>Snippet   one <b>bold</b>
spans lines</td></tr>
<tr><td><a rel="nofollow" href="https://direct.example.com/b" class='result-link'>Second</a></td></tr>
<tr><td class='result-snippet'>Snippet two</td></tr>
</table></body></html>"""


def test_ddg_parse_and_url_decoding(monkeypatch):
    class FakeResp:
        text = _DDG_PAGE

        def raise_for_status(self):
            pass

    monkeypatch.setattr(search_mod.httpx, "get", lambda *a, **k: FakeResp())
    results = web_search("anything")
    assert results[0] == {"title": "First & Best", "url": "https://example.com/a",
                          "snippet": "Snippet one bold spans lines"}
    assert results[1]["url"] == "https://direct.example.com/b"
    assert "First & Best" in format_results(results)
    assert _real_url("plain") == "plain"


def test_search_failures_degrade_to_empty(monkeypatch):
    monkeypatch.setattr(search_mod.httpx, "get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked")))
    assert web_search("q") == []
    assert web_search("   ") == []
    monkeypatch.setattr(search_mod.httpx, "get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    assert fetch_page("https://x") == ""


def test_gemini_grounding_preferred_and_falls_back(settings, monkeypatch):
    class GeminiResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{
                "content": {"parts": [{"text": "Grounded answer."}]},
                "groundingMetadata": {"groundingChunks": [
                    {"web": {"uri": "https://src1", "title": "src1.com"}},
                    {"other": {}}]}}]}

    urls = []
    monkeypatch.setattr(search_mod.httpx, "post",
                        lambda url, **k: urls.append(url) or GeminiResp())
    s = settings.model_copy(update={"gemini_api_key": "gm", "tavily_api_key": "tv"})
    out = web_search_answer("q", settings=s)
    assert "generativelanguage.googleapis.com" in urls[0]
    assert "gemini-2.5-flash" in urls[0]
    assert out["answer"] == "Grounded answer."
    assert out["results"] == [{"title": "src1.com", "url": "https://src1", "snippet": ""}]
    # gemini failing → next backend (tavily here) still serves
    def boom(url, **k):
        if "generativelanguage" in url:
            raise RuntimeError("quota")
        class TavilyResp:
            def raise_for_status(self):
                pass
            def json(self):
                return {"results": [{"title": "T", "url": "https://t", "content": "c"}]}
        return TavilyResp()
    monkeypatch.setattr(search_mod.httpx, "post", boom)
    assert web_search_answer("q", settings=s)["results"][0]["title"] == "T"


def test_web_search_action_uses_grounded_answer(settings, monkeypatch):
    monkeypatch.setattr(
        "assistant.search.web_search_answer",
        lambda q, max_results=8, settings=None: {
            "answer": "It is 42.",
            "results": [{"title": "hitchhikers.com", "url": "https://h", "snippet": ""}]})
    result = run_action("web_search", {"query": "meaning of life"}, settings)
    assert result.startswith("It is 42.")
    assert "sources:" in result and "https://h" in result


def test_google_backend_and_fallback_chain(settings, monkeypatch):
    class GoogleResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": [{"title": "G", "link": "https://g", "snippet": "sg"}]}

    seen = []
    monkeypatch.setattr(search_mod.httpx, "get",
                        lambda url, **k: seen.append(url) or GoogleResp())
    s = settings.model_copy(update={"google_api_key": "gk", "google_cse_id": "cx1"})
    results = web_search("q", settings=s)
    assert seen[0].startswith("https://www.googleapis.com/customsearch")
    assert results == [{"title": "G", "url": "https://g", "snippet": "sg"}]
    # key without cx → google skipped entirely, DDG used
    seen.clear()
    monkeypatch.setattr(search_mod, "_search_ddg", lambda q, n: [{"title": "D", "url": "u", "snippet": ""}])
    s2 = settings.model_copy(update={"google_api_key": "gk"})
    assert web_search("q", settings=s2)[0]["title"] == "D" and not seen
    # google failing → falls back to DDG instead of returning nothing
    monkeypatch.setattr(search_mod, "_search_google",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quota")))
    assert web_search("q", settings=s)[0]["title"] == "D"


def test_tavily_used_when_key_present(settings, monkeypatch):
    calls = []

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"title": "T", "url": "https://t", "content": "c" * 400}]}

    monkeypatch.setattr(search_mod.httpx, "post",
                        lambda url, **k: calls.append(url) or FakeResp())
    s = settings.model_copy(update={"tavily_api_key": "tv-key"})
    results = web_search("q", settings=s)
    assert calls == ["https://api.tavily.com/search"]
    assert results[0]["title"] == "T" and len(results[0]["snippet"]) == 300


def test_web_search_action_synthesizes(settings, monkeypatch):
    monkeypatch.setattr("assistant.search.web_search_answer",
                        lambda q, max_results=8, settings=None: {
                            "answer": "",
                            "results": [{"title": "Doc", "url": "https://d",
                                         "snippet": "answer here"}]})

    class FakeLLM:
        def __init__(self, settings):
            pass

        def complete(self, prompt, system=None, **kw):
            assert "answer here" in prompt
            return "The answer (https://d)."

    monkeypatch.setattr("assistant.llm.LLM", FakeLLM)
    assert run_action("web_search", {"query": "what is X"}, settings) \
        == "The answer (https://d)."
    # synthesis failure → raw results, still useful
    monkeypatch.setattr(FakeLLM, "complete",
                        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("api")))
    assert "top results:" in run_action("web_search", {"query": "what is X"}, settings)
    # no results → honest message
    monkeypatch.setattr("assistant.search.web_search_answer",
                        lambda q, max_results=8, settings=None: {"answer": "", "results": []})
    assert "returned nothing" in run_action("web_search", {"query": "zzz"}, settings)


def test_plan_task_gets_search_enrichment(settings, monkeypatch):
    monkeypatch.setattr("assistant.search.web_search",
                        lambda q, max_results=6, settings=None: [
                            {"title": "Yu Zhi Lan", "url": "https://r", "snippet": "sichuan"}])
    prompts = []

    class FakeLLM:
        def __init__(self, settings):
            pass

        def complete_json(self, prompt, system=None, **kw):
            prompts.append(prompt)
            return {"title": "Book dinner", "due": None,
                    "steps": [{"who": "owner", "step": "call Yu Zhi Lan"}],
                    "next": "call"}

    monkeypatch.setattr("assistant.llm.LLM", FakeLLM)
    result = run_action("plan_task", {"request": "find a sichuan restaurant"}, settings)
    assert "Web search results" in prompts[0] and "Yu Zhi Lan" in prompts[0]
    assert result.startswith("planned: Book dinner")
