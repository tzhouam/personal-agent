"""Cross-links between the profile's sub-stores, computed for analysis.

The profile, finance ledger, and health log describe one person; this module
derives the deterministic joins between them so chat analyses can cite
connections instead of guessing: meal↔expense pairs matched on the shared
date + stated-time identity, food spending vs meals actually logged,
health-category spending vs the nutrient needs being tracked. Rendered as a
"## Cross-links" context block — only lines with data appear.
"""

from datetime import date

from .config import Settings


def build_crosslinks(settings: Settings) -> str:
    """Cross-domain lines for the chat context, '' when nothing links."""
    from .finance_store import FinanceStore
    from .health_store import HealthStore

    finance = FinanceStore(settings.profile_dir)
    health = HealthStore(settings.profile_dir)
    month = date.today().isoformat()[:7]
    lines: list[str] = []

    food = [r for r in finance.records(month)
            if r["type"] == "expense" and r["category"] == "food"]
    meals = health.records(days=31, kind="meal")
    meals = [m for m in meals if str(m["date"]).startswith(month)]
    if food or meals:
        spend = round(sum(r["amount"] for r in food), 2)
        food_days = {str(r["date"]) for r in food}
        meal_days = {str(m["date"]) for m in meals}
        unlogged = sorted(food_days - meal_days)
        line = (f"this month: {len(food)} food purchases ({spend} "
                f"{settings.finance_currency}) vs {len(meals)} meals logged")
        if unlogged:
            line += (f"; days with food spend but no meal logged: "
                     f"{', '.join(unlogged[-5:])}")
        lines.append(line)

    pairs = _meal_expense_pairs(meals, food)
    if pairs:
        lines.append("matched meal↔expense (same date+time): " + "; ".join(
            f"{m['id']} {m.get('description', '')} ↔ {f['id']} {f['amount']} "
            f"{f['currency']}" for m, f in pairs[-3:]))

    health_spend = [r for r in finance.records(month)
                    if r["type"] == "expense" and r["category"] == "health"]
    needs = health.open_needs()
    if health_spend or needs:
        bits = []
        if health_spend:
            bits.append(f"health spending this month: "
                        f"{round(sum(r['amount'] for r in health_spend), 2)} "
                        f"{settings.finance_currency} ({len(health_spend)} purchases)")
        if needs:
            bits.append("open nutrient needs: "
                        + ", ".join(n["item"] for n in needs))
        lines.append(" | ".join(bits))

    return "\n".join(lines)


def _meal_expense_pairs(meals: list[dict], expenses: list[dict]) -> list[tuple]:
    """(meal, expense) pairs sharing a date + stated time — the same event
    logged in both stores. Auto-filled times never match (both sides must
    have stated the time)."""
    def stated(record):
        if record.get("time_source", "stated") != "stated":
            return ""
        return str(record.get("time", "") or "")

    by_key = {(str(e["date"]), stated(e)): e for e in expenses if stated(e)}
    return [(m, by_key[(str(m["date"]), stated(m))]) for m in meals
            if stated(m) and (str(m["date"]), stated(m)) in by_key]
