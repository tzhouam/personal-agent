"""Health subprofile: body facts, meals, exercise, and nutrient needs in the
profile git repo.

`health.yaml` lives next to `finance.yaml` — versioned, local-only, and
never-delete (wrong entries are *voided*). It holds a small static profile
(sex, birth year, height), append-only time-series records (`meal`,
`exercise`, `weight` — each with the same stated-or-auto time identity the
finance ledger uses), and a list of nutrients/ingredients the owner wants
covered ("needs").

Meals and body numbers enter via chat: spoken ("午饭吃了牛肉面"), or read off
a photo — a meal, a nutrition label, a body-scale screen — by the multimodal
main LLM, which estimates calories/protein when they aren't printed. All
analysis numbers (BMI, weight trend, exercise totals, daily calorie/protein
averages) are computed here in code so health advice cites real figures.
"""

import re
import shutil
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from assistant.platform.locks import _path_lock, locked_transaction

RECORD_KINDS = ("meal", "exercise", "weight")

# date-encoded record id: h-YYYYMMDD-N (new); legacy record ids are bare hN;
# need ids are nN (allocated from the meta file's next_need_id).
_DAY_ID_RE = re.compile(r"^h-(\d{8})-(\d+)$")


class HealthStore:
    """Health log sharded into **per-day files** `health/YYYY-MM-DD.yaml`
    (`{records: [...]}`) keyed by each record's event date, with the static body
    profile + nutrient needs in a non-daily `health/profile.yaml`
    (`{profile, needs, next_need_id}`). Legacy `hN` record ids and `nN` need ids
    are preserved; new records get self-describing `h-YYYYMMDD-N` ids. A one-time
    `_ensure_migrated()` splits the old single `health.yaml`."""

    DIRNAME = "health"
    LEGACY = "health.yaml"

    def __init__(self, repo_dir: Path):
        """Bind to the `health/` day-file dir inside `repo_dir` (profile repo)."""
        self.repo_dir = Path(repo_dir)
        self.dir = self.repo_dir / self.DIRNAME
        self.legacy_path = self.repo_dir / self.LEGACY
        self.meta_path = self.dir / "profile.yaml"
        self.marker_path = self.dir / ".migrated"
        self._lock_file = self.repo_dir.parent / "write.lock"

    # ── per-day file plumbing ────────────────────────────────────────
    def _day_path(self, day: str) -> Path:
        return self.dir / f"{day}.yaml"

    def _load_day(self, day: str) -> dict:
        p = self._day_path(day)
        if not p.exists():
            return {"records": []}
        return yaml.safe_load(p.read_text()) or {"records": []}

    def _load_meta(self) -> dict:
        if not self.meta_path.exists():
            return {"profile": {}, "needs": [], "next_need_id": 1}
        data = yaml.safe_load(self.meta_path.read_text()) or {}
        for key, default in (("profile", {}), ("needs", []), ("next_need_id", 1)):
            data.setdefault(key, default)
        return data

    def _git(self, *args: str) -> None:
        if (self.repo_dir / ".git").exists():
            subprocess.run(["git", *args], cwd=self.repo_dir, capture_output=True)

    def _write_yaml(self, path: Path, data: dict) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        tmp.replace(path)  # atomic — readers never see a torn file
        return path

    def _save_day(self, day: str, data: dict, message: str) -> None:
        p = self._write_yaml(self._day_path(day), data)
        self._git("add", str(p.relative_to(self.repo_dir)))
        self._git("commit", "-q", "-m", message)

    def _save_meta(self, data: dict, message: str) -> None:
        p = self._write_yaml(self.meta_path, data)
        self._git("add", str(p.relative_to(self.repo_dir)))
        self._git("commit", "-q", "-m", message)

    def _ensure_migrated(self) -> None:
        """Split the legacy single `health.yaml` into day-files + `profile.yaml`,
        once. Marker-gated, crash-safe (deterministic rebuild), marker-after-
        validated-output. The legacy shared `next_id` becomes `next_need_id` (new
        needs continue the sequence; records switch to date-encoded ids)."""
        if self.marker_path.exists():
            return
        with _path_lock(self._lock_file):
            if self.marker_path.exists():
                return
            self.dir.mkdir(parents=True, exist_ok=True)
            if not self.legacy_path.exists():
                self.marker_path.write_text("no-legacy\n")
                return
            legacy = yaml.safe_load(self.legacy_path.read_text()) or {}
            records = legacy.get("records", [])
            by_day: dict[str, list] = {}
            for r in records:
                by_day.setdefault(str(r["date"]), []).append(r)
            written = 0
            for day, recs in by_day.items():
                self._write_yaml(self._day_path(day), {"records": recs})
                written += len(recs)
            self._write_yaml(self.meta_path, {
                "profile": legacy.get("profile", {}), "needs": legacy.get("needs", []),
                "next_need_id": legacy.get("next_id", 1)})
            if written != len(records):
                raise RuntimeError(
                    f"health migration lost records: {written} != {len(records)}")
            self._git("add", self.DIRNAME)
            self._git("commit", "-q", "-m", "health: migrate log to per-day files")
            self.marker_path.write_text("migrated\n")

    def _find(self, record_id: str) -> tuple[str | None, dict]:
        """`(day, day_data)` holding `record_id`, or `(None, {})`. Date-encoded
        ids parse their day; legacy `hN` ids scan day-files."""
        m = _DAY_ID_RE.match(str(record_id))
        if m:
            c = m.group(1)
            day = f"{c[:4]}-{c[4:6]}-{c[6:8]}"
            data = self._load_day(day)
            if any(r.get("id") == record_id for r in data["records"]):
                return day, data
            return None, {}
        if self.dir.exists():
            for p in sorted(self.dir.glob("*.yaml")):
                if p.name == "profile.yaml":
                    continue
                data = yaml.safe_load(p.read_text()) or {"records": []}
                if any(r.get("id") == record_id for r in data.get("records", [])):
                    return p.stem, data
        return None, {}

    def profile(self) -> dict:
        """The static body profile dict (sex/birth_year/height_cm)."""
        self._ensure_migrated()
        return dict(self._load_meta()["profile"])

    def to_single_file(self) -> int:
        """Reverse migration (rollback): merge day-files + `profile.yaml` back
        into a single `health.yaml`. Reconstructs the shared `next_id` above the
        max legacy `hN` AND `nN` suffix so old-code `add()`/`add_need()` can't
        collide. Returns the record count. Caller quiesces writers + holds lock."""
        recs = []
        if self.dir.exists():
            for p in sorted(self.dir.glob("*.yaml")):
                if p.name == "profile.yaml":
                    continue
                recs.extend((yaml.safe_load(p.read_text()) or {}).get("records", []))
        recs.sort(key=lambda r: (str(r["date"]), str(r["id"])))
        meta = self._load_meta()
        suffixes = [int(m.group(1)) for r in recs
                    if (m := re.match(r"^h(\d+)$", str(r.get("id", ""))))]
        suffixes += [int(m.group(1)) for n in meta["needs"]
                     if (m := re.match(r"^n(\d+)$", str(n.get("id", ""))))]
        next_id = max(suffixes, default=0) + 1
        self._write_yaml(self.legacy_path, {
            "profile": meta["profile"], "next_id": next_id,
            "needs": meta["needs"], "records": recs})
        shutil.rmtree(self.dir, ignore_errors=True)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "health: revert to single-file log")
        return len(recs)

    # ── static profile ───────────────────────────────────────────────
    @locked_transaction
    def set_profile(self, **fields) -> dict:
        """Update the static body profile (sex, birth_year, height_cm) with
        the validated subset of `fields`; unknown/invalid ones are ignored.
        Returns the stored profile."""
        self._ensure_migrated()
        data = self._load_meta()
        profile = data["profile"]
        sex = str(fields.get("sex") or "").strip().lower()
        if sex in ("male", "female", "m", "f", "男", "女"):
            profile["sex"] = {"m": "male", "男": "male",
                              "f": "female", "女": "female"}.get(sex, sex)
        for key, low, high in (("birth_year", 1900, date.today().year),
                               ("height_cm", 80, 260)):
            try:
                value = float(fields.get(key))
                if low <= value <= high:
                    profile[key] = int(value) if key == "birth_year" else round(value, 1)
            except (TypeError, ValueError):
                pass
        self._save_meta(data, "health: profile update")
        return profile

    # ── records ──────────────────────────────────────────────────────
    @locked_transaction
    def add(self, kind: str, when: str = "", time: str = "",
            source: str = "chat", **fields) -> tuple[str, dict | None]:
        """Append one `meal`/`exercise`/`weight` record →
        `("created", record)`, `("invalid", None)`, or `("duplicate",
        existing)`. Dedup identity: same kind + date + stated time (one meal
        at 12:30 is one meal, however described — the finance bill-identity
        idea); weight additionally matches on the same kg for timeless
        re-sends. Numeric fields are validated per kind; a meal keeps its
        free-text `description` plus optional calories/macros estimates."""
        self._ensure_migrated()
        kind = str(kind).strip().lower()
        if kind not in RECORD_KINDS:
            return "invalid", None
        when = str(when or date.today().isoformat()).strip()
        try:
            datetime.strptime(when, "%Y-%m-%d")
        except ValueError:
            return "invalid", None
        stated = str(time or "").strip()
        if stated:
            try:
                stated = datetime.strptime(stated, "%H:%M").strftime("%H:%M")
            except ValueError:
                return "invalid", None

        body: dict = {}
        if kind == "meal":
            body["description"] = str(fields.get("description") or "").strip()[:200]
            if not body["description"]:
                return "invalid", None
            for key in ("calories_kcal", "protein_g", "carbs_g", "fat_g"):
                value = _number(fields.get(key), 0, 6000)
                if value is not None:
                    body[key] = value
        elif kind == "exercise":
            body["activity"] = str(fields.get("activity") or "").strip()[:80]
            if not body["activity"]:
                return "invalid", None
            # duration is OPTIONAL: set/rep strength work (pushups × sets,
            # squats × sets) has no meaningful minute count, so requiring one
            # forced the model to invent a time (owner correction 2026-07-15:
            # don't 自行估算时长). Store it only when a valid value is given;
            # drop an out-of-range value like a bad macro estimate rather than
            # rejecting the whole record.
            duration = _number(fields.get("duration_min"), 1, 1440)
            if duration is not None:
                body["duration_min"] = duration
        else:  # weight
            kg = _number(fields.get("weight_kg"), 20, 400)
            if kg is None:
                return "invalid", None
            body["weight_kg"] = kg
        note = str(fields.get("note") or "")[:200]

        day_data = self._load_day(when)   # dedup within this day only
        for r in day_data["records"]:
            if r.get("voided") or r["kind"] != kind or str(r["date"]) != when:
                continue
            if stated and _stated_time(r) == stated:
                # meals: one sitting can hold several dishes — only a
                # similarly-described item at the same time is a duplicate
                # (owner correction 2026-07-13: 燕窝 after 椒盐虾 at 20:00
                # was wrongly rejected)
                if kind == "meal" and not _similar_text(
                        r.get("description", ""), body.get("description", "")):
                    continue
                return "duplicate", r
            if kind == "weight" and not stated and not _stated_time(r) \
                    and r.get("weight_kg") == body["weight_kg"]:
                return "duplicate", r
        now = datetime.now()
        record = {"id": f"h-{when.replace('-', '')}-{_next_n(day_data['records'], when)}",
                  "kind": kind, "date": when,
                  "time": stated or now.strftime("%H:%M"),
                  "time_source": "stated" if stated else "auto",
                  "logged_at": now.strftime("%Y-%m-%d %H:%M"),
                  **body, "note": note, "source": str(source or "chat")[:20]}
        day_data["records"].append(record)
        label = body.get("description") or body.get("activity") or body.get("weight_kg")
        self._save_day(when, day_data, f"health: {kind} {label} ({record['id']})")
        return "created", record

    @locked_transaction
    def void(self, record_id: str) -> bool:
        """Mark a record voided (never delete). True if one was voided."""
        self._ensure_migrated()
        day, data = self._find(record_id)
        if day is None:
            return False
        for r in data["records"]:
            if r["id"] == record_id and not r.get("voided"):
                r["voided"] = True
                self._save_day(day, data, f"health: void {record_id}")
                return True
        return False

    def records(self, days: int | None = None,
                kind: str | None = None) -> list[dict]:
        """Non-voided records, oldest first; `days` limits to a trailing
        window (system-local today) and `kind` to one record type. Reads only
        the day-files whose filename date is in-window."""
        self._ensure_migrated()
        cutoff = (date.today() - timedelta(days=days)).isoformat() if days else ""
        out: list[dict] = []
        if self.dir.exists():
            for p in self.dir.glob("*.yaml"):
                if p.name == "profile.yaml" or p.stem < cutoff:
                    continue
                data = yaml.safe_load(p.read_text()) or {}
                out.extend(r for r in data.get("records", [])
                           if not r.get("voided") and (kind is None or r["kind"] == kind))
        return sorted(out, key=lambda r: (str(r["date"]), r["id"]))

    def query(self, start: str = "", end: str = "", kind: str | None = None,
              contains: str = "", limit: int = 80) -> list[dict]:
        """On-demand retrieval behind the `query_health` chat action: non-voided
        records within [`start`, `end`] (YYYY-MM-DD, inclusive; either bound
        optional), optionally one `kind` and a case-insensitive substring over
        description/activity/note. Oldest first, capped at `limit` — so the
        agent can look up any day or period, not just the context snapshot."""
        out = self.records(kind=kind)
        if start:
            out = [r for r in out if str(r["date"]) >= start]
        if end:
            out = [r for r in out if str(r["date"]) <= end]
        if contains:
            needle = contains.lower()
            out = [r for r in out if needle in
                   f"{r.get('description', '')} {r.get('activity', '')} "
                   f"{r.get('note', '')}".lower()]
        return out[-limit:]

    # ── needs (nutrients / ingredients wanted) ───────────────────────
    @locked_transaction
    def add_need(self, item: str, why: str = "") -> dict | None:
        """Track a nutrient/ingredient the owner wants covered; None when the
        item is empty or already open."""
        self._ensure_migrated()
        item = str(item or "").strip()[:80]
        if not item:
            return None
        data = self._load_meta()
        if any(n["item"].lower() == item.lower() and not n.get("done")
               for n in data["needs"]):
            return None
        need = {"id": f"n{data['next_need_id']}", "item": item,
                "why": str(why or "")[:160], "since": date.today().isoformat()}
        data["next_need_id"] += 1
        data["needs"].append(need)
        self._save_meta(data, f"health: need {item} ({need['id']})")
        return need

    @locked_transaction
    def done_need(self, need_id: str) -> bool:
        """Mark need `need_id` covered. True if one was open."""
        self._ensure_migrated()
        data = self._load_meta()
        for n in data["needs"]:
            if n["id"] == need_id and not n.get("done"):
                n["done"] = True
                self._save_meta(data, f"health: need done {need_id}")
                return True
        return False

    def open_needs(self) -> list[dict]:
        """Needs not yet marked covered."""
        self._ensure_migrated()
        return [n for n in self._load_meta()["needs"] if not n.get("done")]

    # ── analysis (deterministic) ─────────────────────────────────────
    def summary(self, days: int = 7) -> dict:
        """Computed health picture for the trailing `days` window: body facts
        (+age/BMI derived), latest weight and delta across the window,
        exercise totals by activity, meal counts and daily calorie/protein
        averages over the days that have data, and open needs."""
        profile = self.profile()
        if profile.get("birth_year"):
            profile["age"] = date.today().year - int(profile["birth_year"])
        weights = self.records(kind="weight")
        latest = weights[-1] if weights else None
        if latest and profile.get("height_cm"):
            meters = profile["height_cm"] / 100
            profile["bmi"] = round(latest["weight_kg"] / meters ** 2, 1)
        window = self.records(days=days)
        weight_window = [r for r in window if r["kind"] == "weight"]
        delta = (round(weight_window[-1]["weight_kg"] - weight_window[0]["weight_kg"], 1)
                 if len(weight_window) >= 2 else None)
        exercise = [r for r in window if r["kind"] == "exercise"]
        by_activity: dict[str, float] = {}
        for r in exercise:  # durationless (set/rep) sessions contribute 0 min
            by_activity[r["activity"]] = round(
                by_activity.get(r["activity"], 0) + (r.get("duration_min") or 0), 1)
        meals = [r for r in window if r["kind"] == "meal"]
        kcal_days: dict[str, float] = {}
        protein_days: dict[str, float] = {}
        meal_days: dict[str, int] = {}
        for r in meals:
            meal_days[r["date"]] = meal_days.get(r["date"], 0) + 1
            if r.get("calories_kcal") is not None:
                kcal_days[r["date"]] = kcal_days.get(r["date"], 0) + r["calories_kcal"]
            if r.get("protein_g") is not None:
                protein_days[r["date"]] = protein_days.get(r["date"], 0) + r["protein_g"]
        # per-day totals (most recent first) — so "how many calories yesterday"
        # is answerable directly, not just from the multi-day average.
        by_day = [{"date": d, "meals": meal_days[d],
                   "kcal": round(kcal_days.get(d, 0)) or None,
                   "protein_g": round(protein_days.get(d, 0), 1) or None}
                  for d in sorted(meal_days, reverse=True)]
        return {"days": days, "profile": profile, "by_day": by_day,
                "latest_weight": latest and {"kg": latest["weight_kg"],
                                             "date": latest["date"]},
                "weight_delta": delta,
                "exercise_sessions": len(exercise),
                "exercise_minutes": round(
                    sum(r.get("duration_min") or 0 for r in exercise), 1),
                "exercise_by_activity": dict(sorted(by_activity.items(),
                                                    key=lambda kv: -kv[1])),
                "meals_logged": len(meals),
                "avg_daily_kcal": (round(sum(kcal_days.values()) / len(kcal_days))
                                   if kcal_days else None),
                "avg_daily_protein_g": (round(sum(protein_days.values())
                                              / len(protein_days), 1)
                                        if protein_days else None),
                "needs": self.open_needs()}


def render_summary(summary: dict) -> str:
    """Compact human/prompt rendering of `summary`."""
    p = summary["profile"]
    facts = ", ".join(f"{k} {p[k]}" for k in ("sex", "age", "height_cm", "bmi")
                      if p.get(k)) or "(no body profile yet)"
    lines = [f"profile: {facts}"]
    if summary["latest_weight"]:
        line = (f"weight: {summary['latest_weight']['kg']} kg "
                f"(on {summary['latest_weight']['date']})")
        if summary["weight_delta"] is not None:
            line += f", {summary['weight_delta']:+} kg over {summary['days']}d"
        lines.append(line)
    lines.append(f"exercise last {summary['days']}d: "
                 f"{summary['exercise_sessions']} sessions, "
                 f"{summary['exercise_minutes']} min"
                 + (" — " + ", ".join(f"{a} {m}min" for a, m in
                                      summary["exercise_by_activity"].items())
                    if summary["exercise_by_activity"] else ""))
    meal_bits = [f"{summary['meals_logged']} meals logged"]
    if summary["avg_daily_kcal"] is not None:
        meal_bits.append(f"~{summary['avg_daily_kcal']} kcal/day")
    if summary["avg_daily_protein_g"] is not None:
        meal_bits.append(f"~{summary['avg_daily_protein_g']} g protein/day")
    lines.append(f"food last {summary['days']}d: " + ", ".join(meal_bits))
    for day in summary.get("by_day", [])[:7]:  # per-day totals, recent first
        bits = [f"{day['meals']} meals"]
        if day["kcal"] is not None:
            bits.append(f"{day['kcal']} kcal")
        if day["protein_g"] is not None:
            bits.append(f"{day['protein_g']} g protein")
        lines.append(f"  {day['date']}: " + ", ".join(bits))
    if summary["needs"]:
        lines.append("wants covered: " + "; ".join(
            f"[{n['id']}] {n['item']}" + (f" ({n['why']})" if n.get("why") else "")
            for n in summary["needs"]))
    return "\n".join(lines)


def _next_n(records: list[dict], day: str) -> int:
    """Next numeric sequence for a day-file's date-encoded ids (`h-YYYYMMDD-N`);
    legacy `hN` ids in the file don't match and don't count."""
    compact = day.replace("-", "")
    nums = [int(m.group(2)) for r in records
            if (m := _DAY_ID_RE.match(str(r.get("id", "")))) and m.group(1) == compact]
    return max(nums, default=0) + 1


def _similar_text(a: str, b: str) -> bool:
    """Loose same-dish check: normalized containment either way."""
    na, nb = " ".join(str(a).lower().split()), " ".join(str(b).lower().split())
    return bool(na and nb) and (na in nb or nb in na)


def _stated_time(record: dict) -> str:
    """Explicitly-stated time for dedup ('' when auto-filled)."""
    if record.get("time_source", "stated") != "stated":
        return ""
    return str(record.get("time", "") or "")


def _number(value, low: float, high: float) -> float | None:
    """`value` as a float within [low, high], else None."""
    try:
        number = round(float(value), 1)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None
