"""Finance ledger: store math, action handlers, chat context, receipt flow."""

from assistant.actions import run_action
from assistant.chat.agent import build_context, handle_message
from assistant.finance_store import FinanceStore, render_summary


class FakeLLM:
    def __init__(self, result):
        self.result = result
        self.prompts = []

    def complete_json(self, prompt, system=None, **kw):
        self.prompts.append(prompt)
        return self.result


def test_store_add_validates(settings):
    store = FinanceStore(settings.profile_dir)
    assert store.add("expense", 45.5, category="Food", note="午饭")[1]["id"] == "f1"
    assert store.add("income", "12000", category="salary")[1]["amount"] == 12000.0
    assert store.add("expense", -5) == ("invalid", None)
    assert store.add("expense", "abc") == ("invalid", None)
    assert store.add("borrow", 10) == ("invalid", None)
    assert store.add("expense", 10, when="not-a-date") == ("invalid", None)
    assert store.add("expense", 10, time="25:99") == ("invalid", None)
    assert store.add("expense", 10, when="2026-06-30")[1]["date"] == "2026-06-30"
    # normalization
    rec = next(r for r in store.records() if r["id"] == "f1")
    assert rec["category"] == "food" and rec["currency"] == "CNY"


def test_store_void_and_filters(settings):
    store = FinanceStore(settings.profile_dir)
    store.add("expense", 100, when="2026-06-01")
    store.add("expense", 50, when="2026-07-01")
    assert store.void("f1") and not store.void("f1")
    assert not store.void("f99")
    assert [r["id"] for r in store.records()] == ["f2"]
    assert store.records("2026-06") == []
    assert store.months() == ["2026-07"]


def test_summary_math(settings):
    store = FinanceStore(settings.profile_dir)
    store.add("income", 20000, category="salary", when="2026-07-01")
    store.add("expense", 3000, category="housing", when="2026-07-02")
    store.add("expense", 1000, category="food", when="2026-07-03")
    store.add("expense", 500, category="food", when="2026-07-15")
    store.add("income", 18000, category="salary", when="2026-06-01")
    store.add("expense", 9000, category="travel", when="2026-06-05")
    s = store.summary("2026-07")
    assert s["income"] == 20000 and s["expense"] == 4500 and s["net"] == 15500
    assert s["savings_rate"] == 0.775
    assert list(s["by_category"]) == ["housing", "food"] and s["by_category"]["food"] == 1500
    assert s["prev_month_net"] == {"month": "2026-06", "income": 18000,
                                   "expense": 9000, "net": 9000}
    # no-income month → savings rate None, renders as n/a
    empty = store.summary("2026-05")
    assert empty["savings_rate"] is None and empty["count"] == 0
    assert "n/a" in render_summary(empty)


def test_actions_roundtrip(settings):
    out = run_action("log_transaction",
                     {"kind": "expense", "amount": 45, "category": "food",
                      "note": "lunch"}, settings)
    assert out.startswith("logged f1: expense 45.0 CNY · food · lunch")
    assert "rejected" in run_action("log_transaction", {"kind": "expense",
                                                        "amount": 0}, settings)
    assert "[f1]" in run_action("list_transactions", {}, settings)
    assert "income 0" in run_action("finance_summary", {}, settings)
    assert run_action("void_transaction", {"id": "f1"}, settings) == "transaction f1 voided"
    assert "(no transactions recorded)" in run_action("list_transactions", {}, settings)


def test_chat_context_and_llm_logging(settings):
    FinanceStore(settings.profile_dir).add("expense", 88, category="food", note="dinner")
    llm = FakeLLM({"reply": "记好了", "actions": [
        {"type": "log_transaction", "kind": "expense", "amount": 45,
         "category": "transport", "note": "打车"}]})
    reply = handle_message("打车花了45", settings, llm)
    assert "## Finance ledger" in llm.prompts[0]
    assert "[f1]" in llm.prompts[0] and "dinner" in llm.prompts[0]
    assert "logged f2: expense 45.0 CNY · transport · 打车" in reply
    # empty ledger → no finance section (context stays lean)
    assert "## Finance ledger" not in build_context(
        type(settings)(_env_file=None, data_dir=settings.data_dir / "other"))


def test_context_finance_coexists_with_profile(settings):
    # regression: the finance render_summary import must not shadow the
    # profile render_summary used earlier in build_context (UnboundLocalError)
    settings.profile_dir.mkdir(parents=True, exist_ok=True)
    (settings.profile_dir / "profile.yaml").write_text(
        "identity:\n  name: Tester\n  github: tester\n")
    FinanceStore(settings.profile_dir).add("expense", 10, category="food")
    ctx = build_context(settings)
    assert "## Owner profile" in ctx and "## Finance ledger" in ctx


def test_receipt_screenshot_flow(settings, tmp_path, monkeypatch):
    pic = tmp_path / "receipt.png"
    pic.write_bytes(b"\x89PNG fake")
    monkeypatch.setattr(
        "assistant.vision.describe_images",
        lambda s, p: ["WeChat Pay payment receipt: paid ¥68.00 to 深圳面点王餐饮 "
                      "on 2026-07-11, transaction id 42000..."])
    llm = FakeLLM({"reply": "已记账：面点王 68 元。", "actions": [
        {"type": "log_transaction", "kind": "expense", "amount": 68,
         "category": "food", "note": "面点王"}]})
    reply = handle_message("记一下这笔", settings, llm, image_paths=[str(pic)])
    assert "¥68.00" in llm.prompts[0]  # description reached the model
    assert "logged f1: expense 68.0 CNY · food · 面点王" in reply
    assert FinanceStore(settings.profile_dir).records()[0]["amount"] == 68.0


def test_dedup_rejects_same_signature(settings):
    store = FinanceStore(settings.profile_dir)
    assert store.add("expense", 68, note="面点王", time="12:30")[0] == "created"
    # identical signature → duplicate, nothing written
    status, existing = store.add("expense", 68, note=" 面点王 ", time="12:30",
                                 category="other")
    assert status == "duplicate" and existing["id"] == "f1"
    assert len(store.records()) == 1
    # different time, note, or amount → separate legitimate transactions
    assert store.add("expense", 68, note="面点王", time="19:05")[0] == "created"
    assert store.add("expense", 68, note="外卖")[0] == "created"
    assert store.add("expense", 68.5, note="面点王", time="12:30")[0] == "created"
    # voided records don't block re-logging
    store.void("f1")
    assert store.add("expense", 68, note="面点王", time="12:30")[0] == "created"


def test_log_transaction_reports_duplicate(settings):
    run_action("log_transaction", {"kind": "expense", "amount": 68,
                                   "note": "面点王", "time": "12:30"}, settings)
    out = run_action("log_transaction", {"kind": "expense", "amount": 68,
                                         "note": "面点王", "time": "12:30"}, settings)
    assert out.startswith("NOT logged — duplicate of f1")
    assert "12:30" in out
