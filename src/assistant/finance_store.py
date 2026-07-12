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

    def load(self) -> dict:
        """Parsed store, or an empty scaffold when missing/empty."""
        if not self.path.exists():
            return {"next_id": 1, "records": []}
        return yaml.safe_load(self.path.read_text()) or {"next_id": 1, "records": []}

    def _save(self, data: dict, message: str) -> None:
        """Write back and git-commit (best-effort) so the ledger is auditable."""
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        if (self.repo_dir / ".git").exists():
            subprocess.run(["git", "add", self.FILENAME], cwd=self.repo_dir,
                           capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.repo_dir,
                           capture_output=True)

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
        # Dedup uses the STATED time only: an auto-filled clock time would
        # make the same forgotten-and-resent entry look unique every minute.
        signature = _signature(kind, amount, currency, when, stated, note)
        for r in data["records"]:
            if not r.get("voided") and _signature(
                    r["type"], r["amount"], r["currency"], r["date"],
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


def render_summary(summary: dict, currency: str = "CNY") -> str:
    """One-paragraph human rendering of `summary` for chat/slash output."""
    cats = ", ".join(f"{c} {v}" for c, v in list(summary["by_category"].items())[:6])
    rate = (f"{summary['savings_rate'] * 100:.0f}%" if summary["savings_rate"] is not None
            else "n/a (no income logged)")
    lines = [f"{summary['month']}: income {summary['income']} {currency}, "
             f"spend {summary['expense']} {currency}, net {summary['net']} "
             f"(savings rate {rate}, {summary['count']} records)"]
    if cats:
        lines.append(f"top spend: {cats}")
    if summary.get("prev_month_net"):
        p = summary["prev_month_net"]
        lines.append(f"prev {p['month']}: income {p['income']}, spend {p['expense']}, net {p['net']}")
    return "\n".join(lines)
