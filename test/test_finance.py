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


def test_every_record_has_full_time_identity(settings):
    store = FinanceStore(settings.profile_dir)
    _, stated = store.add("expense", 30, note="咖啡", time="09:15")
    _, auto = store.add("expense", 25, note="打车")
    assert stated["time"] == "09:15" and stated["time_source"] == "stated"
    assert auto["time"] and auto["time_source"] == "auto"   # logging clock time
    assert len(auto["logged_at"]) == 16                     # YYYY-MM-DD HH:MM
    from assistant.finance_store import timestamp_of
    assert timestamp_of(stated) == f"{stated['date']} 09:15"


def test_auto_time_does_not_weaken_dedup(settings):
    # the same forgotten-and-resent NL entry minutes apart must still be
    # caught: auto-filled clock times are excluded from the signature
    store = FinanceStore(settings.profile_dir)
    assert store.add("expense", 45, note="午饭")[0] == "created"
    status, existing = store.add("expense", 45, note="午饭")
    assert status == "duplicate" and existing["id"] == "f1"
    # but a STATED time distinguishes a genuine second purchase
    assert store.add("expense", 45, note="午饭", time="19:00")[0] == "created"


def test_log_transaction_reports_duplicate(settings):
    run_action("log_transaction", {"kind": "expense", "amount": 68,
                                   "note": "面点王", "time": "12:30"}, settings)
    out = run_action("log_transaction", {"kind": "expense", "amount": 68,
                                         "note": "面点王", "time": "12:30"}, settings)
    assert out.startswith("NOT logged — duplicate of f1")
    assert "12:30" in out


def test_recategorize(settings):
    run_action("log_transaction", {"kind": "expense", "amount": 456.96,
                                   "note": "物业管理服务中心",
                                   "category": "shopping"}, settings)
    out = run_action("recategorize_transaction",
                     {"id": "f1", "category": "housing"}, settings)
    assert out == "f1 recategorized: shopping → housing"
    assert FinanceStore(settings.profile_dir).records()[0]["category"] == "housing"
    assert "no active transaction" in run_action(
        "recategorize_transaction", {"id": "f9", "category": "housing"}, settings)
    # off-list categories are kept but flagged
    out = run_action("recategorize_transaction",
                     {"id": "f1", "category": "misc"}, settings)
    assert "not a standard category" in out


def test_bill_identity_dedups_across_note_wordings(settings):
    # owner scenario: a receipt image of an already-recorded payment must not
    # double-log even when the merchant/note is worded differently —
    # (date, stated time, amount) IS the bill identity
    store = FinanceStore(settings.profile_dir)
    assert store.add("expense", 68, note="面点王", time="12:30")[0] == "created"
    status, existing = store.add("expense", 68, note="深圳面点王餐饮有限公司",
                                 time="12:30", category="food")
    assert status == "duplicate" and existing["id"] == "f1"
    assert len(store.records()) == 1
    # different stated time → genuinely another purchase
    assert store.add("expense", 68, note="深圳面点王餐饮有限公司", time="18:40")[0] == "created"


def test_similar_warning_on_same_day_amount(settings):
    out1 = run_action("log_transaction",
                      {"kind": "expense", "amount": 45, "note": "午饭"}, settings)
    assert "⚠" not in out1
    # same amount, same day, but a stated time → logged with a warning
    out2 = run_action("log_transaction",
                      {"kind": "expense", "amount": 45, "note": "星巴克",
                       "time": "16:00"}, settings)
    assert out2.startswith("logged f2")
    assert "same amount already recorded that day: f1" in out2


def test_category_detail_drilldown(settings):
    store = FinanceStore(settings.profile_dir)
    store.add("expense", 45, category="food", note="美团", time="12:10")
    store.add("expense", 55, category="food", note="美团", time="12:40",
              when=_today(-1))
    store.add("expense", 342, category="food", note="虎东白", time="20:34")
    store.add("expense", 30, category="food", note="奶茶", time="22:30")
    store.add("expense", 100, category="shopping", note="MUJI", time="15:00")
    d = store.category_detail("food")
    assert d["count"] == 4 and d["total"] == 472.0 and d["avg"] == 118.0
    assert d["max"]["amount"] == 342.0 and d["max"]["note"] == "虎东白"
    assert d["by_note"]["美团"] == {"total": 100.0, "count": 2}
    assert d["by_daypart"]["dinner"]["total"] == 342.0
    assert d["by_daypart"]["lunch"]["count"] == 2
    assert d["by_daypart"]["late-night"] == {"total": 30.0, "count": 1}
    assert store.category_detail("travel")["count"] == 0


def test_render_summary_drills_into_dominant_categories(settings):
    store = FinanceStore(settings.profile_dir)
    store.add("income", 100, category="salary")
    store.add("expense", 342, category="food", note="虎东白", time="20:34")
    store.add("expense", 45, category="food", note="美团", time="12:10")
    store.add("expense", 100, category="shopping", note="MUJI", time="15:00")
    store.add("expense", 20, category="transport", note="地铁")
    text = render_summary(store.summary(), store=store)
    assert "food 387.0 (76%)" in text
    assert "food detail: 2 txns, avg 193.5, max 342.0 (虎东白" in text
    assert "food top: 虎东白 342.0×1, 美团 45.0×1" in text
    assert "food by time: dinner 342.0 (1), lunch 45.0 (1)" in text
    # transport (4%) gets no drill-down; without a store no drill-down at all
    assert "transport detail" not in text
    assert "detail" not in render_summary(store.summary())


def _today(offset):
    from datetime import date, timedelta
    return (date.today() + timedelta(days=offset)).isoformat()


def test_finance_query_by_range_category_and_text(settings):
    store = FinanceStore(settings.profile_dir)
    store.add("expense", 45, category="food", note="午饭 星巴克", when="2026-05-10")
    store.add("expense", 300, category="housing", note="房租", when="2026-05-01")
    store.add("expense", 20, category="food", note="外卖", when="2026-06-02")
    assert {r["id"] for r in store.query(start="2026-05-01", end="2026-05-31")} == {"f1", "f2"}
    assert [r["note"] for r in store.query(category="food")] == ["午饭 星巴克", "外卖"]
    assert [r["note"] for r in store.query(contains="星巴克")] == ["午饭 星巴克"]


def test_query_transactions_action_totals(settings):
    store = FinanceStore(settings.profile_dir)
    store.add("expense", 45, category="food", note="午饭", when="2026-05-10")
    store.add("income", 1000, category="salary", note="兼职", when="2026-05-15")
    out = run_action("query_transactions", {"start": "2026-05-01", "end": "2026-05-31"}, settings)
    assert "income 1000" in out and "expense 45" in out and "net 955" in out
    assert "午饭" in out
    assert "no transactions" in run_action("query_transactions", {"month": None, "start": "2000-01-01", "end": "2000-01-02"}, settings)
