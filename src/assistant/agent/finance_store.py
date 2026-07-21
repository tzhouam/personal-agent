"""Personal finance ledger: income/expense records in the profile git repo.

`finance.yaml` lives next to `todos.yaml`, so every transaction is a
reviewable, revertible commit and the data never leaves the machine (the
profile repo has no remote). Records follow the never-delete idiom — a wrong
entry is *voided*, not removed — and all analysis numbers (monthly totals,
category breakdown, savings rate) are computed here in code, so the chat
LLM narrates deterministic figures instead of inventing them.

Records enter via chat (typed `log_transaction` actions — including amounts
the vision chain reads off receipt screenshots) or the `/fin` slash command.
"""

import re
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path

import yaml

from assistant.platform.locks import _path_lock, locked_transaction
from assistant.platform.timeutil import weekday_cn

CATEGORIES = ["food", "transport", "housing", "utilities", "entertainment",
              "shopping", "health", "education", "travel", "salary", "bonus",
              "investment", "transfer", "other"]

# date-encoded record id: f-YYYYMMDD-N (new); legacy ids are bare fN.
_DAY_ID_RE = re.compile(r"^f-(\d{8})-(\d+)$")


class FinanceStore:
    """Finance ledger sharded into **per-day files** `finance/YYYY-MM-DD.yaml`
    (`{records: [...]}`), keyed by each record's *event* date — so a wrong day
    is visible in its own file and dedup can never conflate two days. Legacy `fN`
    ids are preserved; new records get self-describing `f-YYYYMMDD-N` ids. Every
    mutation rewrites one day-file and commits it when the profile dir is git.
    A one-time `_ensure_migrated()` splits the old single `finance.yaml`."""

    DIRNAME = "finance"
    LEGACY = "finance.yaml"

    def __init__(self, repo_dir: Path):
        """Bind to the `finance/` day-file dir inside `repo_dir` (profile repo)."""
        self.repo_dir = Path(repo_dir)
        self.dir = self.repo_dir / self.DIRNAME
        self.legacy_path = self.repo_dir / self.LEGACY
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

    def _git(self, *args: str) -> None:
        if (self.repo_dir / ".git").exists():
            subprocess.run(["git", *args], cwd=self.repo_dir, capture_output=True)

    def _write_day(self, day: str, data: dict) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self._day_path(day)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        tmp.replace(p)  # atomic — readers never see a torn file
        return p

    def _save_day(self, day: str, data: dict, message: str) -> None:
        p = self._write_day(day, data)
        self._git("add", str(p.relative_to(self.repo_dir)))
        self._git("commit", "-q", "-m", message)

    def _ensure_migrated(self) -> None:
        """Split the legacy single `finance.yaml` into day-files, once. Marker-
        gated, crash-safe (deterministic rebuild from the immutable legacy file),
        and marker-after-validated-output so a crashed commit never suppresses a
        retry. Runs under the write lock (reentrant with the mutation methods)."""
        if self.marker_path.exists():
            return
        with _path_lock(self._lock_file):
            if self.marker_path.exists():
                return
            self.dir.mkdir(parents=True, exist_ok=True)
            if not self.legacy_path.exists():   # fresh install — nothing to migrate
                self.marker_path.write_text("no-legacy\n")
                return
            legacy = yaml.safe_load(self.legacy_path.read_text()) or {}
            records = legacy.get("records", [])
            by_day: dict[str, list] = {}
            for r in records:
                by_day.setdefault(str(r["date"]), []).append(r)
            written = 0
            for day, recs in by_day.items():      # overwrites any partial output
                self._write_day(day, {"records": recs})
                written += len(recs)
            if written != len(records):
                raise RuntimeError(
                    f"finance migration lost records: {written} != {len(records)}")
            self._git("add", self.DIRNAME)        # best-effort; "nothing to commit" is fine
            self._git("commit", "-q", "-m", "finance: migrate ledger to per-day files")
            self.marker_path.write_text("migrated\n")   # marker last, after validated output

    def _find(self, record_id: str) -> tuple[str | None, dict]:
        """`(day, day_data)` for the file holding `record_id`, or `(None, {})`.
        Date-encoded ids parse their own day; legacy `fN` ids scan day-files."""
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
                data = yaml.safe_load(p.read_text()) or {"records": []}
                if any(r.get("id") == record_id for r in data.get("records", [])):
                    return p.stem, data
        return None, {}

    @locked_transaction
    def add(self, kind: str, amount: float, category: str = "other",
            note: str = "", when: str = "", time: str = "", currency: str = "CNY",
            source: str = "chat") -> tuple[str, dict | None]:
        """Append one record → `("created", record)`. Invalid input (bad
        kind/amount/date/time) → `("invalid", None)`. A record with the same
        dedup signature — kind + amount + currency + date + time + context
        note — already present **in that day's file** → `("duplicate", existing)`
        and nothing is written. `when` defaults to today; `time` is optional HH:MM
        (e.g. off a receipt); `category` outside the known list is kept but
        lowercased."""
        self._ensure_migrated()
        kind = str(kind).strip().lower()
        if kind not in ("income", "expense"):
            return "invalid", None
        try:
            amount = round(float(amount), 2)
        except (TypeError, ValueError):
            return "invalid", None
        if amount <= 0:
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
        currency = str(currency or "CNY").upper()[:8]
        note = str(note or "")[:200]
        day_data = self._load_day(when)   # dedup within this day only
        # Dedup, two identities (stated times only — an auto-filled clock
        # time would make a forgotten-and-resent entry look unique):
        # 1. bill identity: same kind+amount+currency+date+STATED time;
        # 2. full signature incl. note, for entries without a stated time.
        signature = _signature(kind, amount, currency, when, stated, note)
        for r in day_data["records"]:
            if r.get("voided"):
                continue
            if stated and (r["type"], r["amount"], r["currency"], str(r["date"]),
                           _stated_time(r)) == (kind, amount, currency, when, stated):
                return "duplicate", r
            if _signature(r["type"], r["amount"], r["currency"], r["date"],
                          _stated_time(r), r.get("note", "")) == signature:
                return "duplicate", r
        now = datetime.now()
        record = {"id": f"f-{when.replace('-', '')}-{_next_n(day_data['records'], when)}",
                  "date": when,
                  # full YYYY-MM-DD HH:MM identity: stated time when known, else clock
                  "time": stated or now.strftime("%H:%M"),
                  "time_source": "stated" if stated else "auto",
                  "logged_at": now.strftime("%Y-%m-%d %H:%M"),
                  "type": kind, "amount": amount, "currency": currency,
                  "category": str(category or "other").strip().lower()[:30],
                  "note": note, "source": str(source or "chat")[:20]}
        day_data["records"].append(record)
        self._save_day(when, day_data,
                       f"finance: {kind} {record['amount']} {record['category']} ({record['id']})")
        return "created", record

    def to_single_file(self) -> int:
        """Reverse migration (rollback): merge all day-files back into a single
        `finance.yaml` the pre-sharding code can read *and* add() to. Reconstructs
        `next_id` above the max legacy `fN` suffix so old-code `add()` can't
        collide, removes the day dir + marker. Returns the record count. Caller
        must quiesce writers (daemon stopped) and hold the lock."""
        recs = []
        if self.dir.exists():
            for p in sorted(self.dir.glob("*.yaml")):
                recs.extend((yaml.safe_load(p.read_text()) or {}).get("records", []))
        recs.sort(key=lambda r: (str(r["date"]), str(r["id"])))
        next_id = max((int(m.group(1)) for r in recs
                       if (m := re.match(r"^f(\d+)$", str(r.get("id", ""))))),
                      default=0) + 1
        tmp = self.legacy_path.with_name(self.legacy_path.name + ".tmp")
        tmp.write_text(yaml.safe_dump({"next_id": next_id, "records": recs},
                                      sort_keys=False, allow_unicode=True))
        tmp.replace(self.legacy_path)
        shutil.rmtree(self.dir, ignore_errors=True)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "finance: revert to single-file ledger")
        return len(recs)

    def similar(self, record: dict) -> list[dict]:
        """Active records that look like possible duplicates of `record`
        despite passing dedup: same kind, amount, currency, and date but a
        different (or missing) stated time. Surfaced as a warning after
        logging, never an automatic rejection — two same-priced purchases in
        one day are legitimate."""
        return [r for r in self.records(str(record["date"])[:7])
                if r["id"] != record["id"] and not r.get("voided")
                and r["type"] == record["type"]
                and r["amount"] == record["amount"]
                and r["currency"] == record["currency"]
                and str(r["date"]) == str(record["date"])]

    @locked_transaction
    def set_category(self, record_id: str, category: str) -> str | None:
        """Recategorize an active record (owner corrections like 物业费 →
        housing). Returns the old category, or None when the id is unknown
        or voided."""
        self._ensure_migrated()
        day, data = self._find(record_id)
        if day is None:
            return None
        for r in data["records"]:
            if r["id"] == record_id and not r.get("voided"):
                old = r["category"]
                r["category"] = str(category).strip().lower()[:30]
                self._save_day(day, data,
                               f"finance: recategorize {record_id} {old}→{r['category']}")
                return old
        return None

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
                self._save_day(day, data, f"finance: void {record_id}")
                return True
        return False

    def records(self, month: str | None = None) -> list[dict]:
        """Non-voided records, oldest first; `month` filters to 'YYYY-MM' (by
        globbing only that month's day-files)."""
        self._ensure_migrated()
        pattern = f"{month}-*.yaml" if month else "*.yaml"
        out: list[dict] = []
        if self.dir.exists():
            for p in self.dir.glob(pattern):
                data = yaml.safe_load(p.read_text()) or {}
                out.extend(r for r in data.get("records", []) if not r.get("voided"))
        return sorted(out, key=lambda r: (str(r["date"]), r["id"]))

    def query(self, start: str = "", end: str = "", category: str | None = None,
              kind: str | None = None, contains: str = "", limit: int = 100) -> list[dict]:
        """On-demand retrieval behind the `query_transactions` chat action:
        non-voided records within [`start`, `end`] (YYYY-MM-DD, inclusive;
        either bound optional), optionally by `category`, `kind`
        (income|expense), and a substring over the note. Oldest first, capped —
        so the agent can look up any period, not just the current month."""
        out = self.records()
        if start:
            out = [r for r in out if str(r["date"]) >= start]
        if end:
            out = [r for r in out if str(r["date"]) <= end]
        if category:
            out = [r for r in out if r.get("category") == category]
        if kind:
            out = [r for r in out if r.get("type") == kind]
        if contains:
            needle = contains.lower()
            out = [r for r in out if needle in str(r.get("note", "")).lower()]
        return out[-limit:]

    def months(self) -> list[str]:
        """Distinct 'YYYY-MM' months with records, ascending."""
        return sorted({str(r["date"])[:7] for r in self.records()})

    def summary(self, month: str | None = None) -> dict:
        """Deterministic totals for `month` (default: current month): income,
        expense, net, savings rate, expense-by-category (descending), record
        count, and the previous recorded month's net for trend talk."""
        month = month or date.today().isoformat()[:7]
        recs = self.records(month)
        income = round(sum(r["amount"] for r in recs if r["type"] == "income"), 2)
        expense = round(sum(r["amount"] for r in recs if r["type"] == "expense"), 2)
        by_category: dict[str, float] = {}
        for r in recs:
            if r["type"] == "expense":
                by_category[r["category"]] = round(
                    by_category.get(r["category"], 0) + r["amount"], 2)
        prior = [m for m in self.months() if m < month]
        prev = self._net(prior[-1]) if prior else None
        return {"month": month, "income": income, "expense": expense,
                "net": round(income - expense, 2),
                "savings_rate": round((income - expense) / income, 3) if income else None,
                "by_category": dict(sorted(by_category.items(),
                                           key=lambda kv: -kv[1])),
                "count": len(recs), "prev_month_net": prev}

    def category_detail(self, category: str, month: str | None = None) -> dict:
        """Sub-area statistics for one expense `category` in `month`: total,
        transaction count, average and largest single transaction, top note/
        merchant groups, and a time-of-day split (from each record's time
        identity). Deterministic drill-down for the dominant categories in an
        analysis."""
        month = month or date.today().isoformat()[:7]
        recs = [r for r in self.records(month)
                if r["type"] == "expense" and r["category"] == str(category).lower()]
        if not recs:
            return {"category": category, "month": month, "count": 0}
        total = round(sum(r["amount"] for r in recs), 2)
        biggest = max(recs, key=lambda r: r["amount"])
        by_note: dict[str, dict] = {}
        for r in recs:
            key = " ".join(str(r.get("note") or "(no note)").split())[:40]
            group = by_note.setdefault(key, {"total": 0, "count": 0})
            group["total"] = round(group["total"] + r["amount"], 2)
            group["count"] += 1
        by_daypart: dict[str, dict] = {}
        for r in recs:
            part = _daypart(r.get("time", ""))
            group = by_daypart.setdefault(part, {"total": 0, "count": 0})
            group["total"] = round(group["total"] + r["amount"], 2)
            group["count"] += 1
        return {"category": category, "month": month, "total": total,
                "count": len(recs), "avg": round(total / len(recs), 2),
                "max": {"amount": biggest["amount"],
                        "note": biggest.get("note", ""),
                        "date": biggest["date"]},
                "by_note": dict(sorted(by_note.items(),
                                       key=lambda kv: -kv[1]["total"])[:5]),
                "by_daypart": dict(sorted(by_daypart.items(),
                                          key=lambda kv: -kv[1]["total"]))}

    def _net(self, month: str) -> dict:
        """Compact `{month, income, expense, net}` for one month."""
        recs = self.records(month)
        income = round(sum(r["amount"] for r in recs if r["type"] == "income"), 2)
        expense = round(sum(r["amount"] for r in recs if r["type"] == "expense"), 2)
        return {"month": month, "income": income, "expense": expense,
                "net": round(income - expense, 2)}


def _next_n(records: list[dict], day: str) -> int:
    """Next sequence number for a day-file's date-encoded ids (`f-YYYYMMDD-N`),
    allocated as the **numeric** max existing N + 1 (so 11 follows 9, never
    lexicographically). Legacy `fN` ids in the file don't match and don't count."""
    compact = day.replace("-", "")
    nums = [int(m.group(2)) for r in records
            if (m := _DAY_ID_RE.match(str(r.get("id", "")))) and m.group(1) == compact]
    return max(nums, default=0) + 1


def _stated_time(record: dict) -> str:
    """The record's explicitly-stated transaction time for dedup: '' when the
    time was auto-filled at logging. Legacy records (pre time_source) only
    stored a time when it was stated, so they default to stated."""
    if record.get("time_source", "stated") != "stated":
        return ""
    return str(record.get("time", "") or "")


def timestamp_of(record: dict) -> str:
    """Full 'YYYY-MM-DD (周X) HH:MM' display identity of a record ('' time for
    legacy records that never carried one). The weekday makes an off-by-one day
    obvious in the reply the owner sees."""
    d = str(record.get("date", ""))
    try:
        wk = f" ({weekday_cn(datetime.strptime(d, '%Y-%m-%d').date())})"
    except ValueError:
        wk = ""
    return f"{d}{wk} {record.get('time', '')}".strip()


def _signature(kind, amount, currency, when, time, note) -> tuple:
    """Dedup identity of a transaction: what/how-much/when(+time)/context.
    Whitespace-insensitive, case-insensitive on the note."""
    return (str(kind).strip().lower(), round(float(amount), 2),
            str(currency).upper().strip(), str(when).strip(),
            str(time or "").strip(), " ".join(str(note or "").lower().split()))


def render_summary(summary: dict, currency: str = "CNY",
                   store: "FinanceStore | None" = None) -> str:
    """Human/prompt rendering of `summary`: headline, per-category totals
    with share percentages, and — when `store` is given — a computed
    sub-area drill-down (top merchants, avg/max transaction, time-of-day
    split) for each category holding ≥20% of the month's spend (max 2)."""
    expense = summary["expense"] or 0
    rate = (f"{summary['savings_rate'] * 100:.0f}%" if summary["savings_rate"] is not None
            else "n/a (no income logged)")
    lines = [f"{summary['month']}: income {summary['income']} {currency}, "
             f"spend {summary['expense']} {currency}, net {summary['net']} "
             f"(savings rate {rate}, {summary['count']} records)"]
    cats = list(summary["by_category"].items())[:6]
    if cats:
        lines.append("top spend: " + ", ".join(
            f"{c} {v} ({v / expense * 100:.0f}%)" if expense else f"{c} {v}"
            for c, v in cats))
    dominant = [c for c, v in cats if expense and v / expense >= 0.2][:2]
    for category in dominant if store else []:
        detail = store.category_detail(category, summary["month"])
        if not detail.get("count"):
            continue
        notes = ", ".join(f"{note} {g['total']}×{g['count']}"
                          for note, g in detail["by_note"].items())
        parts = ", ".join(f"{part} {g['total']} ({g['count']})"
                          for part, g in detail["by_daypart"].items())
        lines.append(
            f"{category} detail: {detail['count']} txns, avg {detail['avg']}, "
            f"max {detail['max']['amount']} ({detail['max']['note']} "
            f"{detail['max']['date']})\n"
            f"  {category} top: {notes}\n"
            f"  {category} by time: {parts}")
    if summary.get("prev_month_net"):
        p = summary["prev_month_net"]
        lines.append(f"prev {p['month']}: income {p['income']}, spend {p['expense']}, net {p['net']}")
    return "\n".join(lines)


_DAYPARTS = ((5, 10, "morning"), (10, 14, "lunch"), (14, 17, "afternoon"),
             (17, 21, "dinner"))


def _daypart(time: str) -> str:
    """Bucket an HH:MM into morning/lunch/afternoon/dinner/late-night."""
    try:
        hour = int(str(time).split(":")[0])
    except (ValueError, IndexError):
        return "unknown"
    for low, high, name in _DAYPARTS:
        if low <= hour < high:
            return name
    return "late-night"
