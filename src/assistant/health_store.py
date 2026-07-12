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

import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

RECORD_KINDS = ("meal", "exercise", "weight")


class HealthStore:
    """`health.yaml`: `{profile, next_id, needs, records}`. Every mutation
    rewrites the file and commits it when the profile dir is a git repo."""

    FILENAME = "health.yaml"

    def __init__(self, repo_dir: Path):
        """Bind to `health.yaml` inside `repo_dir` (the profile git repo)."""
        self.repo_dir = repo_dir
        self.path = repo_dir / self.FILENAME

    def load(self) -> dict:
        """Parsed store, or an empty scaffold when missing/empty."""
        if not self.path.exists():
            return {"profile": {}, "next_id": 1, "needs": [], "records": []}
        data = yaml.safe_load(self.path.read_text()) or {}
        for key, default in (("profile", {}), ("next_id", 1),
                             ("needs", []), ("records", [])):
            data.setdefault(key, default)
        return data

    def _save(self, data: dict, message: str) -> None:
        """Write back and git-commit (best-effort) so history is auditable."""
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        if (self.repo_dir / ".git").exists():
            subprocess.run(["git", "add", self.FILENAME], cwd=self.repo_dir,
                           capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.repo_dir,
                           capture_output=True)

    # ── static profile ───────────────────────────────────────────────
    def set_profile(self, **fields) -> dict:
        """Update the static body profile (sex, birth_year, height_cm) with
        the validated subset of `fields`; unknown/invalid ones are ignored.
        Returns the stored profile."""
        data = self.load()
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
        self._save(data, "health: profile update")
        return profile

    # ── records ──────────────────────────────────────────────────────
    def add(self, kind: str, when: str = "", time: str = "",
            source: str = "chat", **fields) -> tuple[str, dict | None]:
        """Append one `meal`/`exercise`/`weight` record →
        `("created", record)`, `("invalid", None)`, or `("duplicate",
        existing)`. Dedup identity: same kind + date + stated time (one meal
        at 12:30 is one meal, however described — the finance bill-identity
        idea); weight additionally matches on the same kg for timeless
        re-sends. Numeric fields are validated per kind; a meal keeps its
        free-text `description` plus optional calories/macros estimates."""
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
            duration = _number(fields.get("duration_min"), 1, 1440)
            if not body["activity"] or duration is None:
                return "invalid", None
            body["duration_min"] = duration
        else:  # weight
            kg = _number(fields.get("weight_kg"), 20, 400)
            if kg is None:
                return "invalid", None
            body["weight_kg"] = kg
        note = str(fields.get("note") or "")[:200]

        data = self.load()
        for r in data["records"]:
            if r.get("voided") or r["kind"] != kind or str(r["date"]) != when:
                continue
            if stated and _stated_time(r) == stated:
                return "duplicate", r
            if kind == "weight" and not stated and not _stated_time(r) \
                    and r.get("weight_kg") == body["weight_kg"]:
                return "duplicate", r
        now = datetime.now()
        record = {"id": f"h{data['next_id']}", "kind": kind, "date": when,
                  "time": stated or now.strftime("%H:%M"),
                  "time_source": "stated" if stated else "auto",
                  "logged_at": now.strftime("%Y-%m-%d %H:%M"),
                  **body, "note": note, "source": str(source or "chat")[:20]}
        data["next_id"] += 1
        data["records"].append(record)
        label = body.get("description") or body.get("activity") or body.get("weight_kg")
        self._save(data, f"health: {kind} {label} ({record['id']})")
        return "created", record

    def void(self, record_id: str) -> bool:
        """Mark a record voided (never delete). True if one was voided."""
        data = self.load()
        for r in data["records"]:
            if r["id"] == record_id and not r.get("voided"):
                r["voided"] = True
                self._save(data, f"health: void {record_id}")
                return True
        return False

    def records(self, days: int | None = None,
                kind: str | None = None) -> list[dict]:
        """Non-voided records, oldest first; `days` limits to a trailing
        window and `kind` to one record type."""
        cutoff = (date.today() - timedelta(days=days)).isoformat() if days else ""
        out = [r for r in self.load()["records"]
               if not r.get("voided") and str(r["date"]) >= cutoff
               and (kind is None or r["kind"] == kind)]
        return sorted(out, key=lambda r: (str(r["date"]), r["id"]))

    # ── needs (nutrients / ingredients wanted) ───────────────────────
    def add_need(self, item: str, why: str = "") -> dict | None:
        """Track a nutrient/ingredient the owner wants covered; None when the
        item is empty or already open."""
        item = str(item or "").strip()[:80]
        if not item:
            return None
        data = self.load()
        if any(n["item"].lower() == item.lower() and not n.get("done")
               for n in data["needs"]):
            return None
        need = {"id": f"n{data['next_id']}", "item": item,
                "why": str(why or "")[:160], "since": date.today().isoformat()}
        data["next_id"] += 1
        data["needs"].append(need)
        self._save(data, f"health: need {item} ({need['id']})")
        return need

    def done_need(self, need_id: str) -> bool:
        """Mark need `need_id` covered. True if one was open."""
        data = self.load()
        for n in data["needs"]:
            if n["id"] == need_id and not n.get("done"):
                n["done"] = True
                self._save(data, f"health: need done {need_id}")
                return True
        return False

    def open_needs(self) -> list[dict]:
        """Needs not yet marked covered."""
        return [n for n in self.load()["needs"] if not n.get("done")]

    # ── analysis (deterministic) ─────────────────────────────────────
    def summary(self, days: int = 7) -> dict:
        """Computed health picture for the trailing `days` window: body facts
        (+age/BMI derived), latest weight and delta across the window,
        exercise totals by activity, meal counts and daily calorie/protein
        averages over the days that have data, and open needs."""
        profile = dict(self.load()["profile"])
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
        for r in exercise:
            by_activity[r["activity"]] = round(
                by_activity.get(r["activity"], 0) + r["duration_min"], 1)
        meals = [r for r in window if r["kind"] == "meal"]
        kcal_days: dict[str, float] = {}
        protein_days: dict[str, float] = {}
        for r in meals:
            if r.get("calories_kcal") is not None:
                kcal_days[r["date"]] = kcal_days.get(r["date"], 0) + r["calories_kcal"]
            if r.get("protein_g") is not None:
                protein_days[r["date"]] = protein_days.get(r["date"], 0) + r["protein_g"]
        return {"days": days, "profile": profile,
                "latest_weight": latest and {"kg": latest["weight_kg"],
                                             "date": latest["date"]},
                "weight_delta": delta,
                "exercise_sessions": len(exercise),
                "exercise_minutes": round(sum(r["duration_min"] for r in exercise), 1),
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
    if summary["needs"]:
        lines.append("wants covered: " + "; ".join(
            f"[{n['id']}] {n['item']}" + (f" ({n['why']})" if n.get("why") else "")
            for n in summary["needs"]))
    return "\n".join(lines)


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
