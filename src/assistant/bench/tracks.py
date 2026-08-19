"""PA-Mix v1 tracks (doc/BENCHMARKS.md §2.3-2.4).

Every track exposes `manifest()` and `run(...) -> {item_id: [rep, ...]}`
where each rep is `{"score": 0..1 | None, "raw": <payload>}` — score None is
a CLASSIFIED infra failure (harness/setup/fixture error), and a model
timeout or garbage output scores 0 (a degraded provider must not improve its
mean by dropping hard items). Raw payloads are retained for re-scoring
(§2.7).

Provenance & clock: the golden fixtures are committed with a content hash in
each manifest. The golden oracles are deliberately CLOCK-INDEPENDENT — they
assert action selection, stated-time-derived params (from the text, not the
clock), and end-state COUNTS, never absolute dates — so a frozen clock is
unnecessary for reproducibility; TZ is pinned by the runner for determinism
of any stated-time↔anchor interaction."""

import hashlib
import json
import logging
import os
import re
from pathlib import Path

from assistant.bench.sandbox import READONLY_ACTIONS, bench_settings
from assistant.bench.surfaces import chat_turn, role_probe


def _apply_seed(settings, seed: dict | None) -> None:
    """Seed scratch stores so actions on existing entities can SUCCEED (the
    oracle scores execution, not attempts). Deterministic ids: todos → t1.. ,
    reading → r1.. ; finance ids are read from context by the agent."""
    if not seed:
        return
    if seed.get("todos"):
        from assistant.agent.todo_store import TodoStore

        store = TodoStore(settings.profile_dir)
        for i, title in enumerate(seed["todos"]):
            store.upsert(f"seed{i}", title=title)
    if seed.get("reading"):
        from assistant.agent.todo_store import ReadingList

        store = ReadingList(settings.profile_dir)
        for i, title in enumerate(seed["reading"]):
            store.upsert(f"seed{i}", title=title)
    if seed.get("github"):
        # a dummy CONNECTED GitHub identity so credential-gated routing (the
        # website build) is reachable in the hermetic profile — settings-only,
        # never used for a real call (the action is sandbox-faked)
        settings.github_user = seed["github"]["user"]
        settings.github_token = seed["github"]["token"]
    if seed.get("finance"):
        from assistant.agent.finance_store import FinanceStore

        store = FinanceStore(settings.profile_dir)
        for rec in seed["finance"]:
            store.add(rec["kind"], rec["amount"], category=rec.get("category"),
                      note=rec.get("note", ""))
    if seed.get("reminders"):
        from datetime import datetime, timedelta

        from assistant.platform.notify import ReminderStore

        store = ReminderStore(settings.data_dir)
        for msg in seed["reminders"]:            # → m1, m2, …
            store.add(msg, datetime.now() + timedelta(hours=1))
    if seed.get("lessons"):
        from assistant.agent.lessons_store import LessonsStore

        store = LessonsStore(settings.profile_dir)
        for rule in seed["lessons"]:             # → L1, L2, …
            store.learn(rule)
    if seed.get("failed_reminders"):
        from datetime import datetime, timedelta

        from assistant.platform.notify import ReminderStore

        store = ReminderStore(settings.data_dir)
        for msg in seed["failed_reminders"]:     # dead-lettered → dfremm<n>
            r = store.add(msg, datetime.now() - timedelta(hours=1))
            for _ in range(3):
                store.deliver_due(settings, send=lambda *a: "failed: seeded")

log = logging.getLogger("assistant")

_FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> tuple[dict, str]:
    text = (_FIXTURES / name).read_text()
    return json.loads(text), hashlib.sha256(text.encode()).hexdigest()[:16]


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
    """The golden-actions oracle. Scores SUCCESSFUL execution (ok=True), not
    mere attempts, and REJECTS harmful extras: any faked (risky) action, or
    any mutating action beyond the expected one, fails the item."""
    expect = item["expect"]
    ok_exec = [e for e in record.executed if e.get("ok")]
    ok_types = [e["action"].get("type") for e in ok_exec]
    faked = record.faked_types()

    if "faked" in expect or "faked_any_of" in expect:
        want = {expect["faked"]} if "faked" in expect else set(expect["faked_any_of"])
        hit = bool(want & set(faked))
        # every faked action must be one of the expected risky ones — a turn
        # that ALSO attempts reboot+trigger_run alongside the expected
        # publish must not pass
        only_expected = set(faked) <= want
        clean = all(t in READONLY_ACTIONS for t in ok_types)
        return 1.0 if hit and only_expected and clean else 0.0

    if expect.get("action", "sentinel") is None:
        clean = not faked and all(t in READONLY_ACTIONS for t in ok_types)
        return 1.0 if clean else 0.0

    if faked:
        return 0.0
    targets = ({expect["action"]} if "action" in expect
               else set(expect.get("action_any_of", [])))
    hit = any(e["action"].get("type") in targets
              and _params_match(expect.get("expect_params"), e["action"])
              for e in ok_exec)
    extras_ok = all(t in targets or t in READONLY_ACTIONS for t in ok_types)
    return 1.0 if hit and extras_ok else 0.0


def _run_reps(reps, make, body):
    """Shared rep loop: setup failure → None (infra); body crash → 0 (agent
    failure, not excluded); returns [{"score", "raw"}]. `make(rep)` MUST
    allocate fresh state per rep (state leak across reps invalidates the
    per-item mean and its CI)."""
    out = []
    for rep in range(reps):
        try:
            ctx = make(rep)
        except Exception:
            log.exception("bench infra: setup failed")
            out.append({"score": None, "raw": None})
            continue
        try:
            out.append(body(ctx))
        except Exception as exc:
            log.exception("bench: item body crashed")
            out.append({"score": 0.0, "raw": {"crash": str(exc)[:200]}})
    return out


class GoldenActionsTrack:
    name = "golden-actions"
    directional = False

    def _fixture(self):
        return _load_fixture("golden_actions.json")

    def manifest(self) -> dict:
        fixture, digest = self._fixture()
        return {**fixture["manifest"], "n_items": len(fixture["items"]),
                "fixture_sha256": digest}

    def run(self, base_settings, llm_factory, reps, seed, scratch_root) -> dict:
        items = self._fixture()[0]["items"]
        result = {}
        for i, item in enumerate(items):
            def make(rep, item=item, i=i):   # FRESH scratch per item×rep
                s = bench_settings(base_settings, scratch_root / f"ga{i}-r{rep}")
                return s, llm_factory(s)

            def body(ctx, item=item):
                settings, llm = ctx
                _apply_seed(settings, item.get("seed"))
                rec = chat_turn(settings, llm, item["text"])
                return {"score": _score_action_item(item, rec), "raw": rec.raw()}

            result[item["id"]] = _run_reps(reps, make, body)
        return result


def _count_state(settings) -> dict:
    import yaml

    from assistant.agent.finance_store import FinanceStore
    from assistant.agent.health_store import HealthStore

    store = FinanceStore(settings.profile_dir)
    active = store.records()
    voided = 0
    if store.dir.exists():
        for p in store.dir.glob("*.yaml"):
            data = yaml.safe_load(p.read_text()) or {}
            voided += sum(1 for r in data.get("records", []) if r.get("voided"))
    health = HealthStore(settings.profile_dir).records()
    return {"finance_active": len(active), "finance_voided": voided,
            "health_meals": len([r for r in health if r.get("kind") == "meal"]),
            "health_weights": len([r for r in health if r.get("kind") == "weight"])}


class GoldenDedupTrack:
    """A-layer never-twice guarantees, scored ALL-OR-NOTHING on end state:
    every expected counter must match (a degenerate empty run cannot earn
    partial credit), one fresh scratch per scenario per rep."""

    name = "golden-dedup"
    directional = True

    def _fixture(self):
        return _load_fixture("golden_dedup.json")

    def manifest(self) -> dict:
        fixture, digest = self._fixture()
        return {**fixture["manifest"], "n_items": len(fixture["scenarios"]),
                "fixture_sha256": digest}

    def run(self, base_settings, llm_factory, reps, seed, scratch_root) -> dict:
        scenarios = self._fixture()[0]["scenarios"]
        result = {}
        for i, sc in enumerate(scenarios):
            def make(rep, i=i):              # FRESH scratch per scenario×rep
                s = bench_settings(base_settings, scratch_root / f"gd{i}-r{rep}")
                return s, llm_factory(s)

            def body(ctx, sc=sc):
                settings, llm = ctx
                history, traces = [], []
                for text in sc["turns"]:
                    rec = chat_turn(settings, llm, text, history=history or None)
                    history.append({"owner": text, "assistant": rec.reply})
                    traces.append(rec.raw())
                state = _count_state(settings)
                passed = all(state.get(k) == v for k, v in sc["expect"].items())
                return {"score": 1.0 if passed else 0.0,
                        "raw": {"state": state, "want": sc["expect"],
                                "turns": traces}}

            result[sc["id"]] = _run_reps(reps, make, body)
        return result


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


class NutriBenchTrack:
    """M-layer carbohydrate estimation. Labeled `derived` UNLESS the data
    directory carries a `provenance.json` pinning source version, content
    hash, license, sampling seed, and item ids (§2.2) — only then may results
    be labeled `official-subset`, and never compared to the full benchmark.
    Data is NOT redistributed here: set `PA_BENCH_DATA`. Custom scorer: last
    number ±7.5g, linear to 0 at 2x; unparseable = 0."""

    name = "nutribench"
    directional = False
    TOLERANCE_G = 7.5

    def __init__(self, data_dir: str | None = None):
        self.data_dir = Path(data_dir or os.environ.get("PA_BENCH_DATA", ""))

    def _raw_and_hash(self) -> tuple[str, str]:
        path = self.data_dir / "nutribench_subset.json"
        if not path.is_file():
            raise FileNotFoundError(
                f"NutriBench subset not found at {path} — download the "
                "official dataset (doc/BENCHMARKS.md) and set PA_BENCH_DATA; "
                "not redistributed in this repo")
        text = path.read_text()
        return text, hashlib.sha256(text.encode()).hexdigest()[:16]

    def _items(self) -> list[dict]:
        return json.loads(self._raw_and_hash()[0])

    def _validated_provenance(self, data_hash: str, items: list) -> dict | None:
        """Provenance is only trustworthy when its content hash and item ids
        actually MATCH the loaded subset — a stale provenance.json alongside a
        changed subset must not validate."""
        path = self.data_dir / "provenance.json"
        if not path.is_file():
            return None
        try:
            prov = json.loads(path.read_text())
        except ValueError:
            return None
        required = ("source_version", "content_sha256", "license", "seed",
                    "item_ids")
        if not all(k in prov for k in required):
            return None
        if prov["content_sha256"] != data_hash:
            return None
        if sorted(prov["item_ids"]) != sorted(i["id"] for i in items):
            return None
        return prov

    def manifest(self) -> dict:
        prov, n, digest = None, 0, None
        try:
            text, digest = self._raw_and_hash()
            items = json.loads(text)
            n = len(items)
            prov = self._validated_provenance(digest, items)
        except FileNotFoundError:
            pass
        # ALWAYS `derived`: this uses a custom prompt + scorer, NOT the
        # official NutriBench runner — provenance establishes data ORIGIN,
        # not protocol fidelity, so it is recorded but never promotes the
        # label (reviewer round 2).
        return {"track": self.name, "layer": "M", "label": "derived",
                "source": "NutriBench (https://mehak126.github.io/nutribench.html)",
                "role": "chat", "n_items": n, "fixture_sha256": digest,
                "provenance": prov or "unvalidated (data origin unconfirmed)",
                "scorer": f"1.0 within +/-{self.TOLERANCE_G}g, linear to 0 at "
                          "2x; unparseable=0. Custom scorer, NOT the official "
                          "runner; never comparable to leaderboard numbers."}

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

    def run(self, base_settings, llm_factory, reps, seed, scratch_root) -> dict:
        items = self._items()
        result = {}
        for item in items:
            prompt = ("Estimate the total carbohydrates in this meal. Reply "
                      "with ONLY a number in grams.\n\nMeal: " + item["meal"])

            def make(rep, item=item, i=len(result)):
                # item ids come from external data — never use one as a path
                # component (../ or absolute would escape scratch)
                s = bench_settings(base_settings,
                                   scratch_root / f"nb{i}-r{rep}")
                return s, llm_factory(s)

            def body(ctx, item=item, prompt=prompt):
                settings, llm = ctx
                raw = role_probe(settings, "chat", prompt, llm=llm, max_tokens=50)
                return {"score": self._score(raw, item["carb"]),
                        "raw": {"output": str(raw)[:500]}}

            result[item["id"]] = _run_reps(reps, make, body)
        return result


TRACKS = {t.name: t for t in
          (GoldenActionsTrack(), GoldenDedupTrack(), NutriBenchTrack())}
