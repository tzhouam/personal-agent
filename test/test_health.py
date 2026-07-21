"""Health subprofile: store math (BMI/trends/averages), actions, dedup,
needs lifecycle, chat context, and the food-photo flow."""

from datetime import date, timedelta

from assistant.agent.actions import run_action
from assistant.agent.chat.agent import build_context, handle_message
from assistant.agent.health_store import HealthStore, render_summary


class FakeLLM:
    def __init__(self, result):
        self.result = result
        self.prompts = []

    def complete_json(self, prompt, system=None, **kw):
        self.prompts.append(prompt)
        return self.result


def _day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


def test_profile_set_and_validation(settings):
    store = HealthStore(settings.profile_dir)
    profile = store.set_profile(sex="男", birth_year=1999, height_cm=178)
    assert profile == {"sex": "male", "birth_year": 1999, "height_cm": 178.0}
    profile = store.set_profile(sex="alien", birth_year=1500, height_cm=999)
    assert profile["sex"] == "male" and profile["height_cm"] == 178.0  # unchanged


def test_records_validation_and_kinds(settings):
    store = HealthStore(settings.profile_dir)
    assert store.add("meal", description="牛肉面", calories_kcal=550,
                     protein_g=25)[0] == "created"
    assert store.add("meal", description="")[0] == "invalid"
    assert store.add("exercise", activity="跑步", duration_min=30)[0] == "created"
    assert store.add("exercise", activity="跑步")[0] == "created"  # duration optional
    assert store.add("exercise", activity="")[0] == "invalid"  # activity still required
    assert store.add("weight", weight_kg=70.5)[0] == "created"
    assert store.add("weight", weight_kg=5)[0] == "invalid"
    assert store.add("sleep", hours=8)[0] == "invalid"
    assert store.add("meal", description="x", time="25:00")[0] == "invalid"
    # out-of-range macro estimates are dropped, not fatal
    _, r = store.add("meal", description="沙拉", calories_kcal=99999, time="13:00")
    assert "calories_kcal" not in r


def test_record_dedup_on_stated_time(settings):
    store = HealthStore(settings.profile_dir)
    assert store.add("meal", description="牛肉面", time="12:30")[0] == "created"
    status, existing = store.add("meal", description="香浓牛肉面一碗",
                                 time="12:30")  # same dish, reworded
    assert status == "duplicate" and existing["id"] == "h1"
    assert store.add("meal", description="牛肉面", time="19:00")[0] == "created"
    # weight: timeless same-day same-kg re-send dedups
    assert store.add("weight", weight_kg=70.5)[0] == "created"
    assert store.add("weight", weight_kg=70.5)[0] == "duplicate"
    assert store.add("weight", weight_kg=70.9)[0] == "created"
    # void frees the slot
    store.void("h1")
    assert store.add("meal", description="牛肉面", time="12:30")[0] == "created"


def test_summary_math(settings):
    store = HealthStore(settings.profile_dir)
    store.set_profile(sex="male", birth_year=2000, height_cm=175)
    store.add("weight", weight_kg=72.0, when=_day(-6), time="08:00")
    store.add("weight", weight_kg=70.5, when=_day(0), time="08:00")
    store.add("exercise", activity="跑步", duration_min=30, when=_day(-2))
    store.add("exercise", activity="跑步", duration_min=20, when=_day(-1))
    store.add("exercise", activity="游泳", duration_min=40, when=_day(-1))
    store.add("meal", description="早餐", calories_kcal=400, protein_g=20, when=_day(-1))
    store.add("meal", description="午餐", calories_kcal=700, protein_g=35, when=_day(-1))
    store.add("meal", description="晚餐", calories_kcal=600, when=_day(0))
    store.add_need("维生素D", why="久坐室内")
    s = store.summary(7)
    assert s["profile"]["age"] == date.today().year - 2000
    assert s["profile"]["bmi"] == round(70.5 / 1.75 ** 2, 1)
    assert s["latest_weight"]["kg"] == 70.5 and s["weight_delta"] == -1.5
    assert s["exercise_sessions"] == 3 and s["exercise_minutes"] == 90
    assert s["exercise_by_activity"] == {"游泳": 40, "跑步": 50}
    assert s["meals_logged"] == 3
    assert s["avg_daily_kcal"] == (1100 + 600) // 2
    assert s["avg_daily_protein_g"] == 55.0  # only one day carried protein
    assert [n["item"] for n in s["needs"]] == ["维生素D"]
    text = render_summary(s)
    assert "bmi" in text and "-1.5 kg" in text and "维生素D" in text


def test_needs_lifecycle(settings):
    store = HealthStore(settings.profile_dir)
    need = store.add_need("蛋白质", why="增肌")
    assert need["id"] == "n1"
    assert store.add_need("蛋白质") is None  # dup while open
    assert store.done_need("n1") and not store.done_need("n1")
    assert store.open_needs() == []
    assert store.add_need("蛋白质")["id"] == "n2"  # reopenable after done


def test_actions_roundtrip(settings):
    out = run_action("log_meal", {"description": "牛肉面", "calories_kcal": 550,
                                  "protein_g": 25, "time": "12:30"}, settings)
    assert out.startswith("logged h1: meal · 牛肉面 · calories 550.0 protein 25.0")
    assert "12:30" in out
    out = run_action("log_exercise", {"activity": "跑步", "duration_min": 30}, settings)
    assert "exercise · 跑步 30" in out
    out = run_action("log_weight", {"weight_kg": 70.5}, settings)
    assert "weight · 70.5 kg" in out
    assert "NOT logged" in run_action(
        "log_meal", {"description": "面", "time": "12:30"}, settings)
    assert "rejected" in run_action("log_weight", {"weight_kg": "heavy"}, settings)
    out = run_action("set_health_profile", {"height_cm": 178, "sex": "male"}, settings)
    assert "height_cm=178.0" in out and "sex=male" in out
    out = run_action("add_health_need", {"item": "维生素D"}, settings)
    assert out == "tracking need n4: 维生素D"
    assert run_action("done_health_need", {"id": "n4"}, settings) == "need n4 marked covered"
    summary = run_action("health_summary", {}, settings)
    assert "70.5 kg" in summary and "跑步 30" in summary


def test_exercise_without_duration_logs_faithfully(settings):
    # Set/rep strength work has no meaningful minute count — logging it must
    # NOT require (and so must not provoke inventing) a duration.
    store = HealthStore(settings.profile_dir)
    status, record = store.add("exercise", activity="俯卧撑两组+深蹲两组",
                               note="each 2 sets")
    assert status == "created"
    assert "duration_min" not in record  # never fabricated
    # the outcome line omits the minute suffix rather than printing a made-up one
    out = run_action("log_exercise",
                     {"activity": "卷腹", "note": "3 sets"}, settings)
    assert "exercise · 卷腹" in out and "min" not in out
    # a timed session next to a set/rep one still aggregates cleanly
    store.add("exercise", activity="跑步", duration_min=20)
    s = store.summary(7)
    assert s["exercise_sessions"] == 3 and s["exercise_minutes"] == 20
    render_summary(s)  # durationless sessions must not break rendering


def test_chat_context_and_health_logging(settings):
    HealthStore(settings.profile_dir).set_profile(height_cm=178)
    llm = FakeLLM({"reply": "记好了，跑步辛苦了！", "actions": [
        {"type": "log_exercise", "activity": "跑步", "duration_min": 30}]})
    reply = handle_message("今晚跑了30分钟", settings, llm)
    assert "## Health" in llm.prompts[0] and "height_cm 178" in llm.prompts[0]
    assert "logged h1: exercise · 跑步 30" in reply
    # no health data at all → no health section
    other = type(settings)(_env_file=None, data_dir=settings.data_dir / "other")
    assert "## Health" not in build_context(other)


def test_food_photo_flow_native_multimodal(settings, tmp_path):
    pic = tmp_path / "meal.png"
    pic.write_bytes(b"\x89PNG fake")
    settings.llm_supports_images = True
    seen = {}

    class NativeLLM(FakeLLM):
        def complete_json(self, prompt, system=None, images=None, **kw):
            seen["images"] = images
            self.prompts.append(prompt)
            return self.result

    llm = NativeLLM({"reply": "看起来是牛肉面，大约550千卡（估算）。已记录。",
                     "actions": [{"type": "log_meal", "description": "牛肉面",
                                  "calories_kcal": 550, "protein_g": 25,
                                  "time": "12:30"}]})
    reply = handle_message("这顿饭帮我记一下", settings, llm, image_paths=[str(pic)])
    assert seen["images"] == [str(pic)]
    assert "logged h1: meal · 牛肉面" in reply
    rec = HealthStore(settings.profile_dir).records()[0]
    assert rec["calories_kcal"] == 550.0 and rec["time_source"] == "stated"


def test_crosslinks_join_the_stores(settings):
    from assistant.agent.finance_store import FinanceStore
    from assistant.agent.insights import build_crosslinks

    finance = FinanceStore(settings.profile_dir)
    health = HealthStore(settings.profile_dir)
    # same event in both stores: lunch at 12:30, 45 CNY
    finance.add("expense", 45, category="food", note="面点王", time="12:30")
    health.add("meal", description="牛肉面", time="12:30", calories_kcal=550)
    # food spend on a day with no meal logged
    finance.add("expense", 88, category="food", note="晚饭", when=_day(-1))
    # health-category spend + an open need
    finance.add("expense", 120, category="health", note="维生素D")
    health.add_need("维生素D")

    links = build_crosslinks(settings)
    assert "2 food purchases (133.0 CNY) vs 1 meals logged" in links
    assert _day(-1) in links                      # spend-without-meal day flagged
    assert "h1 牛肉面 ↔ f1 45.0 CNY" in links      # date+time matched pair
    assert "health spending this month: 120.0 CNY" in links
    assert "open nutrient needs: 维生素D" in links
    # auto-time records never fabricate pairs
    finance.add("expense", 30, category="food", note="奶茶")
    health.add("meal", description="奶茶")
    assert "奶茶 ↔" not in build_crosslinks(settings)


def test_crosslinks_in_chat_context(settings):
    from assistant.agent.finance_store import FinanceStore

    FinanceStore(settings.profile_dir).add("expense", 45, category="food",
                                           note="午饭", time="12:30")
    HealthStore(settings.profile_dir).add("meal", description="牛肉面", time="12:30")
    ctx = build_context(settings)
    assert "## Cross-links" in ctx and "牛肉面 ↔ f1" in ctx
    # empty stores → no section
    other = type(settings)(_env_file=None, data_dir=settings.data_dir / "other")
    assert "## Cross-links" not in build_context(other)


def test_meal_dedup_allows_second_dish_same_sitting(settings):
    # owner correction: 燕窝 after 椒盐虾 at the same dinner time was rejected
    store = HealthStore(settings.profile_dir)
    assert store.add("meal", description="晚餐: 椒盐虾配脆炸罗勒叶",
                     time="20:00")[0] == "created"
    assert store.add("meal", description="甜品: 冰糖燕窝",
                     time="20:00")[0] == "created"        # different dish → ok
    status, existing = store.add("meal", description="椒盐虾配脆炸罗勒叶",
                                 time="20:00")            # same dish reworded
    assert status == "duplicate" and existing["id"] == "h1"


def test_summary_per_day_and_context_lists_meals(settings):
    # per-day totals are kept (not just the multi-day average) and the chat
    # context lists individual meals — so "how much did I eat yesterday" is
    # answerable. Regression: summary discarded per-day totals and the context
    # showed only aggregates, so the agent wrongly insisted meals weren't logged.
    store = HealthStore(settings.profile_dir)
    y = _day(-1)
    store.add("meal", when=y, time="08:00", description="早餐 蛋+奶",
              calories_kcal=300, protein_g=20)
    store.add("meal", when=y, time="12:30", description="午餐 牛肉面",
              calories_kcal=600, protein_g=30)
    s = store.summary()
    by_day = {d["date"]: d for d in s["by_day"]}
    assert by_day[y]["meals"] == 2
    assert by_day[y]["kcal"] == 900 and by_day[y]["protein_g"] == 50.0
    assert f"  {y}: 2 meals, 900 kcal" in render_summary(s)
    ctx = build_context(settings)
    assert "早餐 蛋+奶" in ctx and "午餐 牛肉面" in ctx  # individual meals visible


def test_health_query_by_day_kind_and_text(settings):
    store = HealthStore(settings.profile_dir)
    store.add("meal", when="2026-07-13", time="08:00", description="早餐 燕窝粥",
              calories_kcal=200, protein_g=8)
    store.add("meal", when="2026-07-13", time="12:30", description="午餐 牛肉面",
              calories_kcal=600, protein_g=30)
    store.add("meal", when="2026-07-10", time="19:00", description="晚餐 沙拉",
              calories_kcal=300, protein_g=15)
    store.add("exercise", when="2026-07-13", time="18:00", activity="跑步", duration_min=30)
    # single day (all kinds)
    assert {r["id"] for r in store.query(start="2026-07-13", end="2026-07-13")} == {"h1", "h2", "h4"}
    # by kind
    assert [r["description"] for r in
            store.query(start="2026-07-13", end="2026-07-13", kind="meal")] == ["早餐 燕窝粥", "午餐 牛肉面"]
    # text search across all dates
    assert [r["date"] for r in store.query(contains="燕窝")] == ["2026-07-13"]
    # range start excludes the older day
    assert all(r["date"] >= "2026-07-11" for r in store.query(start="2026-07-11"))


def test_query_health_action_formats_records_and_totals(settings):
    store = HealthStore(settings.profile_dir)
    store.add("meal", when="2026-07-13", time="08:00", description="早餐", calories_kcal=200, protein_g=8)
    store.add("meal", when="2026-07-13", time="12:30", description="午餐", calories_kcal=600, protein_g=30)
    out = run_action("query_health", {"date": "2026-07-13"}, settings)
    assert "2 meals" in out and "~800kcal" in out and "~38" in out
    assert "早餐" in out and "午餐" in out
    assert "no health records" in run_action("query_health", {"date": "2020-01-01"}, settings)


# ── event-day resolution in the log handlers (per-day records bug fix) ──

def test_log_meal_resolves_relative_date(settings):
    from datetime import date, timedelta

    from assistant.agent.actions.handlers import _log_meal
    from assistant.agent.health_store import HealthStore

    out = _log_meal(settings, {"description": "牛肉面", "date": "昨天", "time": "12:30"})
    assert out.startswith("logged")
    yday = (date.today() - timedelta(days=1)).isoformat()
    recs = HealthStore(settings.profile_dir).records(days=3, kind="meal")
    assert any(r["date"] == yday and r["description"] == "牛肉面" for r in recs)
    assert yday in out                     # reply echoes the resolved (yesterday) date


def test_log_meal_rejects_unparseable_date_writes_nothing(settings):
    from assistant.agent.actions.handlers import _log_meal
    from assistant.agent.actions.registry import looks_failed
    from assistant.agent.health_store import HealthStore

    store = HealthStore(settings.profile_dir)
    before = len(store.records(days=90, kind="meal"))
    out = _log_meal(settings, {"description": "牛肉面", "date": "上上个礼拜三"})
    assert "rejected" in out and looks_failed(out)          # a failure → repair round
    assert len(HealthStore(settings.profile_dir).records(days=90, kind="meal")) == before


def test_prompt_and_examples_instruct_date_resolution(settings):
    from assistant.agent.actions.registry import ACTIONS
    from assistant.agent.chat.agent import system_prompt

    assert '"date"' in ACTIONS["log_meal"].prompt_example
    assert '"date"' in ACTIONS["log_transaction"].prompt_example
    sp = system_prompt(settings)
    assert "昨天" in sp and "temporal anchor" in sp         # told to resolve relative days
