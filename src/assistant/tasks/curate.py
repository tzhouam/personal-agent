"""Dormancy curator: flip profile entries not seen within their per-section
window to `status: dormant`. Archive-only (never deletes — the flip is
recoverable and evidence is kept), following the Hermes curator invariants."""

from datetime import date, datetime

from ..profile_store import ProfileStore

# Hermes curator invariants: decay only, archive-only semantics (status flip is
# always recoverable), and manual sections are never touched (enforced upstream
# by PROTECTED_SECTIONS anyway).
_DORMANCY_DAYS = {"skills": 30, "interests": 30, "projects": 60}


def curate(store: ProfileStore) -> dict:
    """Mark active entries whose `last_seen` exceeds the section's dormancy
    window as dormant, committing once if anything changed. Returns
    {"decayed": [...]} listing what was flipped. Entries without a parseable
    `last_seen` are left alone; nothing is deleted, so the change is reversible."""
    profile = store.load()
    today = date.today()
    decayed: list[str] = []

    for section, window in _DORMANCY_DAYS.items():
        for entry in profile.get(section, []):
            if entry.get("status", "active") != "active":
                continue
            last_seen = entry.get("last_seen")
            if not last_seen:
                continue
            try:
                age = (today - datetime.strptime(str(last_seen)[:10], "%Y-%m-%d").date()).days
            except ValueError:
                continue
            if age > window:
                entry["status"] = "dormant"  # never deleted — evidence stays
                decayed.append(f"{section}: {entry.get('name') or entry.get('topic')}")

    if decayed:
        store.save(profile, f"curator: {len(decayed)} entries → dormant")
    return {"decayed": decayed}
