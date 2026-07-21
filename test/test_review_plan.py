"""The local plan reviewer (scripts/review_plan.py — dev-process tool, no
outside tools) plus the LLM_REVIEW config slot it rides on."""

import importlib.util
from pathlib import Path

from assistant.platform.config import Settings

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review_plan.py"
spec = importlib.util.spec_from_file_location("review_plan", _SCRIPT)
review_plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review_plan)


def _settings(**kw):
    return Settings(_env_file=None, anthropic_model="default-model", **kw)


# ── verdict parsing (tolerant of real model phrasings) ───────────────

def test_parse_verdict_variants():
    parse = review_plan.parse_verdict
    assert parse("...\nAPPROVE") == "APPROVE"
    assert parse("**Verdict: APPROVE-WITH-CHANGES** — fix x") == "APPROVE-WITH-CHANGES"
    assert parse("APPROVE WITH CHANGES: 1) ...") == "APPROVE-WITH-CHANGES"
    assert parse("approved") == "APPROVE"
    assert parse("Verdict — REVISE, must fix: ...") == "REVISE"
    # the LAST verdict wins (models restate options while reasoning)
    assert parse("could APPROVE, but...\nREVISE") == "REVISE"
    assert parse("no verdict here at all") is None
    assert parse("") is None


# ── reviewer resolution: LLM_REVIEW → aggregator → default ───────────

def test_resolve_reviewer_order():
    resolve = review_plan.resolve_reviewer
    s = _settings(llm_review={"model": "big-brain", "api_key": "k"},
                  llm_mixture={"members": [{"model": "m1"}, {"model": "m2"}],
                               "aggregator": {"model": "agg-model"}})
    assert resolve(s)["model"] == "big-brain"
    s2 = _settings(llm_mixture={"members": [{"model": "m1"}],
                                "aggregator": {"model": "agg-model"}})
    assert resolve(s2)["model"] == "agg-model"
    assert resolve(_settings())["model"] == "default-model"


def test_llm_review_config_degrades_and_routes(monkeypatch):
    import assistant.platform.llm as llm_mod
    from assistant.platform.llm import LLM

    # malformed JSON degrades to {} (feature off, never a crash)
    assert _settings(llm_review='{"model": broken').llm_review == {}
    # a valid spec seeds the "review" role on the LLM router
    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", FakeClient)
    llm = LLM(_settings(anthropic_api_key="k",
                        llm_review={"model": "big-brain",
                                    "base_url": "https://strong.example/anthropic",
                                    "api_key": "rk"}))
    client, model = llm._resolve("review", None)
    assert model == "big-brain"
    assert client.kwargs["base_url"] == "https://strong.example/anthropic"
    # an explicit LLM_ROLES review entry wins over the LLM_REVIEW seed
    llm2 = LLM(_settings(anthropic_api_key="k",
                         llm_roles={"review": {"model": "explicit"}},
                         llm_review={"model": "seeded"}))
    assert llm2._resolve("review", None)[1] == "explicit"
    # never part of the MoA role set
    llm3 = LLM(_settings(anthropic_api_key="k",
                         llm_review={"model": "big-brain"},
                         llm_mixture={"members": [{"model": "a"}, {"model": "b"}]}))
    assert "review" not in llm3._mixture_roles


# ── the probe knows the new knob ─────────────────────────────────────

def test_probe_reports_llm_review(tmp_path, monkeypatch):
    from assistant.init_wizard import FAIL, OK, probe_model_routing

    for key in ("LLM_ROLES", "LLM_MIXTURE", "LLM_REVIEW"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text('LLM_REVIEW={"model": "big-brain", "api_key": "sk-SECRET"}\n')
    status, detail = probe_model_routing(None, env_files=(env,))
    assert status == OK and "review→big-brain" in detail and "sk-SECRET" not in detail
    env.write_text('LLM_REVIEW={"model": broken\n')
    status, detail = probe_model_routing(None, env_files=(env,))
    assert status == FAIL and "LLM_REVIEW malformed JSON" in detail


# ── end-to-end with a fake LLM (no network) ──────────────────────────

def test_review_file_exit_codes(tmp_path, monkeypatch):
    plan = tmp_path / "plan.md"
    plan.write_text("# a plan\ndo the thing")

    class FakeLLM:
        def __init__(self, out):
            self.out = out

        def complete(self, prompt, role=None, mixture=True, max_tokens=0):
            assert role == "review" and mixture is False
            assert "do the thing" in prompt
            if isinstance(self.out, Exception):
                raise self.out
            return self.out

        roles = {}

    def run(out):
        fake = FakeLLM(out)
        monkeypatch.setattr("assistant.platform.llm.LLM", lambda s: fake)
        fake.roles = {}
        return review_plan.review_file(plan, "ctx")

    assert run("looks solid\nAPPROVE") == 0
    assert (tmp_path / "plan.md.review.md").exists()
    assert run("fine but fix x first\nAPPROVE-WITH-CHANGES") == 1
    assert run("needs work\nVerdict: REVISE — fix a, b") == 2
    assert run("rambling with no verdict at all........") == 3
    assert run(RuntimeError("endpoint down")) == 3