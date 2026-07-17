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

import subprocess
from datetime import date, datetime
from pathlib import Path

import yaml

from .locks import locked_transaction

CATEGORIES = ["food", "transport", "housing", "utilities", "entertainment",
              "shopping", "health", "education", "travel", "salary", "bonus",
              "investment", "transfer", "other"]


class FinanceStore:
    """`finance.yaml` ledger: `{next_id, records: [...]}`. Every mutation
    rewrites the file and commits it when the profile dir is a git repo."""

    FILENAME = "finance.yaml"

    def __init__(self, repo_dir: Path):
        """Bind to `finance.yaml` inside `repo_dir` (the profile git repo)."""
        self.repo_dir = repo_dir
        self.path = repo_dir / self.FILENAME
        self._lock_file = repo_dir.parent / "write.lock"

    def load(self) -> dict:
        """Parsed store, or an empty scaffold when missing/empty."""
        if not self.path.exists():
            return {"next_id": 1, "records": []}
        return yaml.safe_load(self.path.read_text()) or {"next_id": 1, "records": []}

    def _save(self, data: dict, message: str) -> None:
        """Write back and git-commit (best-effort) so the ledger is auditable."""
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        tmp.replace(self.path)  # atomic — readers never see a torn file
        if (self.repo_dir / ".git").exists():
            subprocess.run(["git", "add", self.FILENAME], cwd=self.repo_dir,
                           capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.repo_dir,
                           capture_output=True)

    @locked_transaction
    def add(self, kind: str, amount: float, category: str = "other",
            note: str = "", when: str = "", time: str = "", currency: str = "CNY",
            source: str = "chat") -> tuple[str, dict | None]:
        """Append one record → `("created", record)`. Invalid input (bad
        kind/amount/date/time) → `("invalid", None)`. A record with the same
        dedup signature — kind + amount + currency + date + time + context
        note — already present → `("duplicate", existing)` and nothing is
        written, so a receipt screenshot sent twice (or NL + receipt for the
        same payment) can't double-log. `when` defaults to today; `time` is
        optional HH:MM (e.g. off a receipt); `category` outside the known
        list is kept but normalized to lowercase."""
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
        data = self.load()
        # Dedup, two identities (stated times only — an auto-filled clock
        # time would make a forgotten-and-resent entry look unique):
        # 1. bill identity: same kind+amount+currency+date+STATED time is the
        #    same transaction regardless of note wording — a receipt image of
        #    an already-recorded payment must not double-log even when the
        #    merchant is written differently;
        # 2. full signature incl. note, for entries without a stated time.
        signature = _signature(kind, amount, currency, when, stated, note)
        for r in data["records"]:
            if r.get("voided"):
                continue
            if stated and (r["type"], r["amount"], r["currency"], str(r["date"]),
                           _stated_time(r)) == (kind, amount, currency, when, stated):
                return "duplicate", r
            if _signature(r["type"], r["amount"], r["currency"], r["date"],
                          _stated_time(r), r.get("note", "")) == signature:
                return "duplicate", r
        now = datetime.now()
        record = {"id": f"f{data['next_id']}", "date": when,
                  # every record carries a full YYYY-MM-DD HH:MM identity:
                  # the stated transaction time when known (receipt/owner),
                  # else the logging clock time
                  "time": stated or now.strftime("%H:%M"),
                  "time_source": "stated" if stated else "auto",
                  "logged_at": now.strftime("%Y-%m-%d %H:%M"),
                  "type": kind, "amount": amount, "currency": currency,
                  "category": str(category or "other").strip().lower()[:30],
                  "note": note, "source": str(source or "chat")[:20]}
        data["next_id"] += 1
        data["records"].append(record)
        self._save(data, f"finance: {kind} {record['amount']} {record['category']} ({record['id']})")
        return "created", record

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
        data = self.load()
        for r in data["records"]:
            if r["id"] == record_id and not r.get("voided"):
                old = r["category"]
                r["category"] = str(category).strip().lower()[:30]
                self._save(data, f"finance: recategorize {record_id} {old}→{r['category']}")
                return old
        return None

    @locked_transaction
    def void(self, record_id: str) -> bool:
        """Mark a record voided (never delete). True if one was voided."""
        data = self.load()
        for r in data["records"]:
            if r["id"] == record_id and not r.get("voided"):
                r["voided"] = True
                self._save(data, f"finance: void {record_id}")
                return True
        return False

    def records(self, month: str | None = None) -> list[dict]:
        """Non-voided records, oldest first; `month` filters to 'YYYY-MM'."""
        out = [r for r in self.load()["records"] if not r.get("voided")]
        if month:
            out = [r for r in out if str(r["date"]).startswith(month)]
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


def _stated_time(record: dict) -> str:
    """The record's explicitly-stated transaction time for dedup: '' when the
    time was auto-filled at logging. Legacy records (pre time_source) only
    stored a time when it was stated, so they default to stated."""
    if record.get("time_source", "stated") != "stated":
        return ""
    return str(record.get("time", "") or "")


def timestamp_of(record: dict) -> str:
    """Full 'YYYY-MM-DD HH:MM' display identity of a record ('' time for
    legacy records that never carried one)."""
    return f"{record['date']} {record.get('time', '')}".strip()


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
