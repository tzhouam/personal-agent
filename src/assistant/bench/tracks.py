"""PA-Mix v1 tracks (doc/BENCHMARKS.md §2.3-2.4).

Every track exposes `manifest()` and `run(...) -> {item_id: [rep, ...]}`
where each rep is `{"score": 0..1 | None, "raw": <payload>}` — score None is
a CLASSIFIED infra failure (harness/setup/fixture error), and a model
timeout or garbage output scores 0 (a degraded provider must not improve its
mean by dropping hard items). Raw payloads are retained for re-scoring
(§2.7).

Provenance & clock: the golden fixtures are committed with a content hash in
each manifest. Relative-time expectations are resolved from the same frozen,
configured-zone day that the prompt's temporal anchor uses, rather than
hard-coding a date that goes stale. The runner never mutates process-global TZ."""

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from assistant.agent.actions.registry import RETRIEVAL_ACTIONS
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
            when = rec.get("when", "")
            if isinstance(when, dict):
                resolved = _descriptor_date(when)
                if resolved is None:
                    raise ValueError(f"invalid finance seed date descriptor: {when!r}")
                when = resolved.isoformat()
            store.add(rec["kind"], rec["amount"], category=rec.get("category"),
                      note=rec.get("note", ""), when=when)
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


def _today() -> date:
    """Frozen local benchmark day; a seam for deterministic oracle tests."""
    from assistant.platform.timeutil import local_today

    return local_today()


def _descriptor_date(value: dict) -> date | None:
    """Resolve one fixture date descriptor against the local benchmark day."""
    if "$current_year_date" not in value:
        return None
    spec = value["$current_year_date"]
    try:
        today = _today()
        return date(today.year, int(spec["month"]), int(spec["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def _relative_value_matches(want: dict, got) -> bool:
    """Match the fixture's explicit relative date/datetime descriptors."""
    if "$current_year_date" in want:
        expected = _descriptor_date(want)
        try:
            actual = date.fromisoformat(str(got).strip())
        except ValueError:
            return False
        return expected is not None and actual == expected
    if "$relative_datetime" in want:
        spec = want["$relative_datetime"]
        try:
            actual = datetime.fromisoformat(str(got).strip().replace("Z", "+00:00"))
            expected_day = _today() + timedelta(days=int(spec["days"]))
            hour, minute = (int(part) for part in str(spec["time"]).split(":"))
        except (KeyError, TypeError, ValueError):
            return False
        if actual.tzinfo is not None:
            from assistant.platform.timeutil import local_now

            actual = actual.astimezone(local_now().tzinfo)
        return (actual.date() == expected_day and actual.hour == hour
                and actual.minute == minute and actual.second == 0)
    if "$relative_weekday" in want:
        spec = want["$relative_weekday"]
        try:
            weeks_ago = int(spec["weeks_ago"])
            weekday = int(spec["weekday"])
            actual = date.fromisoformat(str(got).strip())
        except (KeyError, TypeError, ValueError):
            return False
        monday = _today() - timedelta(days=_today().weekday())
        expected_day = monday - timedelta(weeks=weeks_ago) + timedelta(days=weekday)
        return actual == expected_day
    return got == want


def _integer_to_chinese(value: int) -> str | None:
    """Canonical common Chinese form for 0..9999 (enough for golden totals)."""
    if not 0 <= value <= 9999:
        return None
    if value == 0:
        return "零"
    digits = "零一二三四五六七八九"
    units = ((1000, "千"), (100, "百"), (10, "十"), (1, ""))
    out, pending_zero = [], False
    remainder = value
    for unit, label in units:
        digit, remainder = divmod(remainder, unit)
        if digit:
            if pending_zero and out:
                out.append("零")
            if not (unit == 10 and digit == 1 and not out):
                out.append(digits[digit])
            out.append(label)
            pending_zero = False
        elif out and remainder:
            pending_zero = True
    return "".join(out)


_TOTAL_CONTEXT = re.compile(
    r"交通|打车|地铁|支出|花(?:了|费)?|消费|费用|金额|一共|共计|合计|总计|"
    r"expense|spent|spend|cost|amount|total", re.IGNORECASE)
_CLAUSE_BREAK = re.compile(r"[。！？!?；;\n]")
_CORRECTION = re.compile(r"而是|实际(?:是|为)|正确(?:是|为)")
_NEGATIVE_BEFORE = re.compile(
    r"不是|并非|不等于|不为|不止|不到|不超过|不少于|超过|高于|低于|"
    r"少于|多于|至少|至多|最多|最少|大于|小于|大约(?:为)?\s*$|"
    r"(?<!纽)约(?:为)?\s*$|接近|"
    r"错误|不对|(?:^|\s)非(?:\s|$)|负\s*$|minus|"
    r"\bnot\b|isn['’]?t|is not|≠", re.IGNORECASE)
_NEGATIVE_AFTER = re.compile(
    r"^\s*(?:(?:元|块|人民币|港币|美元|CNY|RMB|HKD|USD)\s*)?"
    r"(?:[，,]\s*)?(?:但(?:这)?\s*)?"
    r"(?:不是|并非|不正确|错误|不对|而非|isn['’]?t|is not|is wrong|≠)",
    re.IGNORECASE)


def _chinese_integer(token: str) -> int | None:
    """Parse a common non-negative Chinese integer up to the 万 range."""
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    if not token or any(ch not in digits and ch not in units for ch in token):
        return None
    total = section = current = 0
    for ch in token:
        if ch in digits:
            current = digits[ch]
        elif ch == "万":
            section += current
            total += (section or 1) * units[ch]
            section = current = 0
        else:
            section += (current or 1) * units[ch]
            current = 0
    return total + section + current


_MONEY_VALUE = re.compile(
    r"([+-]?\d[\d,]*(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)"
    r"\s*(?:元|块|人民币|港币|美元|CNY|RMB|HKD|USD)", re.IGNORECASE)


def _later_correction_changes_total(suffix: str, wanted: float) -> bool:
    """Whether a later 'actually/rather' clause replaces the stated total.

    A non-monetary explanation ("actually two transactions") is harmless, as
    is a component breakdown whose monetary values sum to the expected total.
    A corrected monetary value with a different total supersedes the candidate.
    """
    correction = _CORRECTION.search(suffix)
    if not correction:
        return False
    values: list[float] = []
    corrected = suffix[correction.end():]
    for match in _MONEY_VALUE.finditer(corrected):
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            parsed = _chinese_integer(raw)
            if parsed is None:
                continue
            value = float(parsed)
        values.append(value)
    if not values:
        return False
    # Multiple component amounts form a total only when the explanation joins
    # them additively; unrelated/negated amounts must not cancel into a pass.
    total = (sum(values) if len(values) > 1
             and re.search(r"加|和|以及|与|\+", corrected) else values[0])
    return abs(total - wanted) > 1e-9


def _contains_expense_total(text: str, expected) -> bool:
    """Whether text affirmatively states a positive expense total.

    Presence-only matching lets contradictions ("not 96") and a net ``-96``
    masquerade as a grounded expense answer. Accept an exact positive Arabic
    or common Chinese numeral only when nearby expense/total language anchors
    its meaning and no immediately preceding negation reverses it.
    """
    try:
        wanted = float(expected)
    except (TypeError, ValueError):
        return False
    value = str(text)
    candidates: list[tuple[int, int]] = []
    for match in re.finditer(
            r"(?<![\d.])([+-]?\d[\d,]*(?:\.\d+)?)(?![\d.])", value):
        try:
            numeric = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if numeric >= 0 and abs(numeric - wanted) <= 1e-9:
            candidates.append(match.span())
    if wanted.is_integer():
        chinese = _integer_to_chinese(int(wanted))
        if chinese:
            numeral = "零一二三四五六七八九十百千万两"
            pattern = rf"(?<![{numeral}]){re.escape(chinese)}(?![{numeral}])"
            candidates.extend(match.span() for match in re.finditer(pattern, value))
    for start, end in candidates:
        prior_breaks = [m.end() for m in _CLAUSE_BREAK.finditer(value, 0, start)]
        next_break = _CLAUSE_BREAK.search(value, end)
        clause_start = prior_breaks[-1] if prior_breaks else 0
        clause_end = next_break.start() if next_break else len(value)
        clause = value[clause_start:clause_end]
        rel_start, rel_end = start - clause_start, end - clause_start
        prefix, suffix = clause[:rel_start], clause[rel_end:]
        # "not 0, but 96" can contain an earlier negation; only language after
        # the last explicit correction governs this candidate.
        prefix = _CORRECTION.split(prefix)[-1]
        if (_TOTAL_CONTEXT.search(clause)
                and not _NEGATIVE_BEFORE.search(prefix[-32:])
                and not _NEGATIVE_AFTER.search(suffix[:32])
                and not _later_correction_changes_total(suffix, wanted)):
            return True
    return False


def _params_match(expected: dict, actual: dict) -> bool:
    for key, want in (expected or {}).items():
        got = actual.get(key)
        if isinstance(want, dict):
            if not _relative_value_matches(want, got):
                return False
        elif isinstance(want, (int, float)):
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
    param_options = expect.get("expect_params_any_of")
    if not isinstance(param_options, list):
        param_options = [expect.get("expect_params")]
    hit = any(e["action"].get("type") in targets
              and any(_params_match(option, e["action"])
                      for option in param_options)
              for e in ok_exec)
    extras_ok = all(t in targets or t in READONLY_ACTIONS for t in ok_types)
    if expect.get("require_retrieval_compose"):
        # The action selecting the right store is only half the behavior under
        # test. A successful compose replaces the raw query dump; when compose
        # fails handle_turn deliberately preserves that dump behind a ✔ marker.
        retrieved = [e for e in ok_exec
                     if e["action"].get("type") in RETRIEVAL_ACTIONS
                     and e["action"].get("type") in targets]
        # Other allowed read-only actions may legitimately leave their own ✔
        # outcome in the final reply. Reject only when THIS retrieval's raw
        # outcome survived, which is handle_turn's truthful compose-failure path.
        raw_echoed = any(f"✔ {str(e.get('outcome', '')).strip()}" in record.reply
                         for e in retrieved if str(e.get("outcome", "")).strip())
        composed = bool(record.reply.strip()) and not raw_echoed
        if "expect_reply_total" in expect:
            # A generic acknowledgement is not an answer. Require the known
            # deterministic total both in the retrieved result and in the
            # composed owner-facing reply, tying the answer to real scratch data.
            wanted = expect["expect_reply_total"]
            composed = (composed and _contains_expense_total(record.reply, wanted)
                        and any(_contains_expense_total(e.get("outcome", ""), wanted)
                                for e in retrieved))
        hit = hit and bool(retrieved) and composed
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
        except Exception:
            log.exception("bench: item body crashed")
            # Raw results are durable. Exception messages may contain owner
            # data or credentials, so persist only a fixed classification.
            out.append({"score": 0.0,
                        "raw": {"crash": "item_body_exception"}})
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
                return {"score": _score_action_item(item, rec),
                        "raw": {**rec.raw(),
                                "benchmark_date": _today().isoformat()}}

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
