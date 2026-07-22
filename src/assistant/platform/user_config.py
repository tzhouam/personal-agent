"""Write a tenant's `users/<uid>/config.env`.

The self-serve abilities (`connect_github`, `build_website`) let a user set their
own `PERSONAL_ENV_FIELDS` from chat — `Settings.for_user` re-reads this file per
request, so an upsert here is visible on the tenant's next turn/job. Upserts in
place (order + comments preserved), atomic, mode 0600 (it holds credentials).
"""

from pathlib import Path


def update_user_config(udir: Path, updates: dict) -> None:
    """Upsert each `KEY=value` in `<udir>/config.env`, rewriting an existing
    key in place and appending any that are new. Values are written verbatim
    (callers pass already-validated tokens/repo names — no secret ever reaches a
    log from here). The file is (re)created 0600 via an atomic temp+replace."""
    cfg = Path(udir) / "config.env"
    lines = cfg.read_text().splitlines() if cfg.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    for key, val in remaining.items():
        out.append(f"{key}={val}")
    cfg.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.with_name(cfg.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n")
    tmp.chmod(0o600)
    tmp.replace(cfg)
