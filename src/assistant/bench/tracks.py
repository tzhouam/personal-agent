"""PA-Mix v1 tracks (doc/BENCHMARKS.md §2.3-2.4): the two PA-golden A-layer
tracks and the NutriBench official-subset M-layer runner.

Every track exposes `manifest()` and `run(settings_factory, llm_factory,
reps, seed) -> {item_id: [scores per rep]}` where a score is 0..1, or None
for a CLASSIFIED infra failure (harness/fixture errors — a model timeout or
garbage output scores 0; a degraded provider must not improve its mean by
dropping hard items)."""

import json
import logging
import re
from pathlib import Path

from assistant.bench.sandbox import bench_settings
from assistant.bench.surfaces import chat_turn, role_probe

log = logging.getLogger("assistant")

_FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


# ── parameter equivalence (fixture manifest's stated rules) ──────────

def _params_match(expected: dict, actual: dict) -> bool:
    for key, want in (expected or {}).items():
        got = actual.get(key)
        if isinstance(want, (int, float)):
            try:
                if abs(float(got) - float(want)) > 1e-9:
                    return False
            except (TypeError, ValueError):
                return False
        elif str(got) != str(want):
            return False
    return True


def _score_action_item(item: dict, record) -> float:
    """The golden-actions oracle: what the sandbox observed vs. the fixture's
    expectation. Multiple emitted actions are fine — the EXPECTED one must be
    among them with equivalent params; no-action items require an empty
    trace; faked expectations assert the risky action was attempted but
    (by sandbox construction) never ran."""
    expect = item["expect"]
    executed_types = [a.get("type") for a in record.executed]
    faked_types = [a.get("type") for a in record.faked]
    if "faked" in expect:
        return 1.0 if expect["faked"] in faked_types else 0.0
    if "faked_any_of" in expect:
        return 1.0 if any(t in faked_types for t in expect["faked_any_of"]) else 0.0
    if expect.get("action", "missing") is None:
        return 1.0 if not record.executed and not record.faked else 0.0
    if "action_any_of" in expect:
        return 1.0 if any(t in executed_types for t in expect["action_any_of"]) else 0.0
    for act in record.executed:
        if act.get("type") == expect["action"] and \
                _params_match(expect.get("expect_params"), act):
            return 1.0
    return 0.0


class GoldenActionsTrack:
    """A-layer: does one owner message produce the right registry action with
    the right params (or, for chit-chat, no action) through the REAL
    handle_turn — lessons injection, repair rounds and all."""

    name = "golden-actions"

    def manifest(self) -> dict:
        fixture = _load_fixture("golden_actions.json")
        return {**fixture["manifest"], "n_items": len(fixture["items"])}

    def run(self, settings_factory, llm_factory, reps: int, seed: int) -> dict:
        items = _load_fixture("golden_actions.json")["items"]
        out: dict[str, list] = {}
        for item in items:
            out[item["id"]] = []
            for _ in range(reps):
                try:
                    settings = settings_factory()
                    llm = llm_factory(settings)
                except Exception:
                    log.exception("bench infra: setup failed")
                    out[item["id"]].append(None)
                    continue
                try:
                    record = chat_turn(settings, llm, item["text"])
                except Exception:
                    # the turn surface swallowing everything would hide agent
                    # crashes; a crash here is an agent failure, scored 0
                    log.exception("bench: chat_turn crashed on %s", item["id"])
                    out[item["id"]].append(0.0)
                    continue
                out[item["id"]].append(_score_action_item(item, record))
        return out


def _count_state(settings) -> dict:
    """End-state counters the dedup scenarios assert on. `records()` already
    excludes voided rows (never-delete: they stay in the day-files), so the
    voided count reads the raw shards."""
    import yaml

    from assistant.agent.finance_store import FinanceStore
    from assistant.agent.health_store import HealthStore

    store = FinanceStore(settings.profile_dir)
    active = store.records()
    voided = 0
    if store.dir.exists():
        for p in store.dir.glob("*.yaml"):
            data = yaml.safe_load(p.read_text()) or {}
            voided += sum(1 for r in data.get("records", [])
                          if r.get("voided"))
    health = HealthStore(settings.profile_dir).records()
    return {
        "finance_active": len(active),
        "finance_voided": voided,
        "health_meals": len([r for r in health if r.get("kind") == "meal"]),
        "health_weights": len([r for r in health if r.get("kind") == "weight"]),
    }


class GoldenDedupTrack:
    """A-layer: the never-twice guarantees — multi-turn scenarios scored by
    END-STATE on the scratch stores (the WorkBench idea), one fresh scratch
    per scenario per rep."""

    name = "golden-dedup"

    def manifest(self) -> dict:
        fixture = _load_fixture("golden_dedup.json")
        return {**fixture["manifest"], "n_items": len(fixture["scenarios"])}

    def run(self, settings_factory, llm_factory, reps: int, seed: int) -> dict:
        scenarios = _load_fixture("golden_dedup.json")["scenarios"]
        out: dict[str, list] = {}
        for sc in scenarios:
            out[sc["id"]] = []
            for _ in range(reps):
                try:
                    settings = settings_factory()   # fresh scratch per scenario
                    llm = llm_factory(settings)
                except Exception:
                    log.exception("bench infra: setup failed")
                    out[sc["id"]].append(None)
                    continue
                try:
                    history: list[dict] = []
                    for turn_text in sc["turns"]:
                        record = chat_turn(settings, llm, turn_text,
                                           history=history or None)
                        history.append({"owner": turn_text,
                                        "assistant": record.reply})
                    state = _count_state(settings)
                    want = sc["expect"]
                    hits = sum(1 for k, v in want.items() if state.get(k) == v)
                    out[sc["id"]].append(hits / len(want))   # partial credit
                except Exception:
                    log.exception("bench: dedup scenario crashed: %s", sc["id"])
                    out[sc["id"]].append(0.0)
        return out


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


class NutriBenchTrack:
    """M-layer official-subset: carbohydrate estimation from natural-language
    meal descriptions, on the `chat` role's configured model. Data is NOT
    redistributed here: point `PA_BENCH_DATA` at a directory containing
    `nutribench_subset.json` (`[{id, meal, carb}]` — see doc/BENCHMARKS.md
    for the sampling manifest rules). Scoring: 1.0 within the paper-style
    ±7.5g tolerance, linear to 0.0 at 2× tolerance; unparseable output
    scores 0 (a timeout is a model failure, not infra)."""

    name = "nutribench-subset"
    TOLERANCE_G = 7.5

    def __init__(self, data_dir: str | None = None):
        import os

        self.data_dir = Path(data_dir or os.environ.get("PA_BENCH_DATA", ""))

    def _items(self) -> list[dict]:
        path = self.data_dir / "nutribench_subset.json"
        if not path.is_file():
            raise FileNotFoundError(
                f"NutriBench subset not found at {path} — download the "
                "official dataset (see doc/BENCHMARKS.md) and set "
                "PA_BENCH_DATA; the data is not redistributed in this repo")
        return json.loads(path.read_text())

    def manifest(self) -> dict:
        try:
            n = len(self._items())
        except FileNotFoundError:
            n = 0
        return {"track": self.name, "layer": "M", "label": "official-subset",
                "source": "NutriBench (https://mehak126.github.io/nutribench.html)",
                "role": "chat", "n_items": n,
                "scorer": f"1.0 within ±{self.TOLERANCE_G}g carbs, "
                          f"linear to 0 at 2x; unparseable=0",
                "note": "subset scores are NEVER comparable to full-benchmark "
                        "or leaderboard numbers"}

    def _score(self, raw: str, truth: float) -> float:
        nums = _NUM_RE.findall(str(raw))
        if not nums:
            return 0.0
        err = abs(float(nums[-1]) - float(truth))
        if err <= self.TOLERANCE_G:
            return 1.0
        if err >= 2 * self.TOLERANCE_G:
            return 0.0
        return 1.0 - (err - self.TOLERANCE_G) / self.TOLERANCE_G

    def run(self, settings_factory, llm_factory, reps: int, seed: int) -> dict:
        items = self._items()   # missing data = infra: raises before any run
        out: dict[str, list] = {}
        for item in items:
            out[item["id"]] = []
            prompt = ("Estimate the total carbohydrates in this meal. Reply "
                      "with ONLY a number in grams.\n\nMeal: " + item["meal"])
            for _ in range(reps):
                try:
                    settings = settings_factory()
                    llm = llm_factory(settings)
                except Exception:
                    log.exception("bench infra: setup failed")
                    out[item["id"]].append(None)
                    continue
                try:
                    raw = role_probe(settings, "chat", prompt, llm=llm,
                                     max_tokens=50)
                except Exception:
                    # a model/API failure scores 0 — dropping hard items must
                    # not improve the mean (§2.5)
                    log.exception("bench: nutribench call failed: %s", item["id"])
                    out[item["id"]].append(0.0)
                    continue
                out[item["id"]].append(self._score(raw, item["carb"]))
        return out


TRACKS = {t.name: t for t in
          (GoldenActionsTrack(), GoldenDedupTrack(), NutriBenchTrack())}
