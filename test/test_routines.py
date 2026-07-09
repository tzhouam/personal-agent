from datetime import datetime

from assistant import routines as routines_mod
from assistant.actions import run_action
from assistant.routines import RoutineStore, check_condition, fire_due, parse_days

MON_9 = datetime(2026, 7, 6, 9, 0)   # Monday
SAT_9 = datetime(2026, 7, 11, 10, 30)  # Saturday, past the weekend routine's time


def test_parse_days():
    assert parse_days("workdays") == {0, 1, 2, 3, 4}
    assert parse_days("weekends") == {5, 6}
    assert parse_days("daily") == set(range(7))
    assert parse_days("") == set(range(7))
    assert parse_days("mon,wed,fri") == {0, 2, 4}
    assert parse_days("Tuesday") == {1}
    assert parse_days("someday") is None


def test_store_due_and_once_per_day(settings):
    store = RoutineStore(settings.data_dir)
    assert store.add("t", "8:99") is None and store.add("t", "08:00", days="blah") is None
    work = store.add("check CI", "08:30", days="workdays")
    store.add("sleep in", "10:00", days="weekends")
    store.add("later today", "23:00", days="daily")

    due = store.due(MON_9)  # Monday 09:00: workday routine past 08:30 fires
    assert [r["id"] for r in due] == [work["id"]]
    store.mark_checked(work["id"], MON_9.date())
    assert store.due(MON_9) == []                       # once per day
    assert [r["days"] for r in store.due(SAT_9)] == ["weekends"]
    assert store.cancel(work["id"]) and not store.cancel(work["id"])
    assert work["id"] not in [r["id"] for r in store.active()]


def test_condition_gate(settings, monkeypatch):
    assert check_condition(settings, "") == (True, "")  # unconditional
    monkeypatch.setattr("assistant.search.web_search",
                        lambda q, max_results=6, settings=None: [
                            {"title": "Rainstorm warning", "url": "u",
                             "snippet": "red rainstorm alert issued"}])

    class FakeLLM:
        def __init__(self, settings):
            pass

        def complete_json(self, prompt, system=None, **kw):
            assert "weather alert" in prompt and "Rainstorm" in prompt
            return {"holds": True, "why": "red rainstorm alert active"}

    monkeypatch.setattr("assistant.llm.LLM", FakeLLM)
    holds, why = check_condition(settings, "there is a weather alert in Shenzhen")
    assert holds and "rainstorm" in why.lower()
    # judge failure → conservative false
    monkeypatch.setattr(FakeLLM, "complete_json",
                        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("api")))
    holds, why = check_condition(settings, "anything")
    assert not holds and "check failed" in why


def test_fire_due_gates_and_sends(settings, monkeypatch):
    store = RoutineStore(settings.data_dir)
    gated = store.add("warn me about the weather", "08:00", days="daily",
                      condition="weather alert in Shenzhen")
    plain = store.add("send me my top todos", "08:00", days="daily")

    monkeypatch.setattr(routines_mod, "check_condition",
                        lambda s, c: (False, "no alert") if c else (True, ""))
    sent, handled = [], []
    monkeypatch.setattr("assistant.chat.agent.handle_message",
                        lambda task, s, llm=None, history=None:
                        handled.append(task) or f"reply to: {task}")
    monkeypatch.setattr("assistant.notify.send_wechat",
                        lambda s, text: sent.append(text) or "sent")

    outcomes = fire_due(settings, now=MON_9)
    assert {o["id"]: o["fired"] for o in outcomes} == {gated["id"]: False,
                                                       plain["id"]: True}
    # the task is framed as execute-now so the chat agent doesn't plan_task it
    assert len(handled) == 1
    assert "execute the task immediately" in handled[0]
    assert "Do NOT use plan_task" in handled[0]
    assert handled[0].endswith("send me my top todos")
    assert len(sent) == 1 and sent[0].startswith(f"🔁 [{plain['id']}] reply to:")
    # both marked checked — nothing due again today, even the gated one
    assert fire_due(settings, now=MON_9) == []


def test_routine_actions(settings):
    result = run_action("create_routine",
                        {"task": "check CI status", "time": "08:30",
                         "days": "workdays", "condition": ""}, settings)
    assert result.startswith("routine rt1 created — workdays at 08:30")
    result = run_action("create_routine",
                        {"task": "warn me", "time": "07:45",
                         "condition": "weather alert in Shenzhen"}, settings)
    assert "only when: weather alert in Shenzhen" in result
    listing = run_action("list_routines", {}, settings)
    assert "[rt1] workdays 08:30" in listing and "(if: weather alert" in listing
    assert run_action("cancel_routine", {"id": "rt1"}, settings) == "routine rt1 cancelled"
    assert "couldn't create routine" in run_action(
        "create_routine", {"task": "x", "time": "noon"}, settings)
