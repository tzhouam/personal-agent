"""Durable, deployment-wide quarantine for unusable LLM routes.

Authentication and permission failures survive process restarts so every
request does not rediscover a broken credential or model.  The database stores
only a canonical endpoint, a credential-keyed HMAC digest, scope, model id, and
timestamp; credentials and provider payloads never cross the persistence
boundary.
"""

import hashlib
import hmac
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


_DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com"
_FINGERPRINT_DOMAIN = b"personal-agent/llm-route-health/v1"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS route_quarantine (
    base_url       TEXT NOT NULL,
    credential_fp  TEXT NOT NULL,
    scope          TEXT NOT NULL CHECK(scope IN ('prov', 'model')),
    model          TEXT NOT NULL DEFAULT '',
    created_ts     TEXT NOT NULL,
    PRIMARY KEY (base_url, credential_fp, scope, model)
);
"""


def canonical_base_url(value: str | None) -> str:
    """Return an opaque stable endpoint identity without route secrets.

    Only normalized scheme/host/port remain readable. Userinfo, path, query,
    and fragment are represented by one digest because compatible endpoints
    put credentials in all four locations. Changing any such component selects
    a fresh health identity without persisting its bytes. Trailing path slashes
    remain canonical-equivalent. Malformed values are represented only by a
    digest for the same reason.
    """
    raw = str(value or _DEFAULT_ANTHROPIC_URL).strip()
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError("base URL needs a scheme and host")
        scheme = parsed.scheme.lower()
        host = parsed.hostname.lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
        if port is not None and not (
                (scheme == "http" and port == 80)
                or (scheme == "https" and port == 443)):
            host = f"{host}:{port}"
        userinfo = parsed.netloc.rsplit("@", 1)[0] if "@" in parsed.netloc else ""
        path = parsed.path.rstrip("/")
        sensitive = "\0".join((userinfo, path, parsed.query, parsed.fragment))
        route_fp = hashlib.sha256(sensitive.encode()).hexdigest()
        return f"{scheme}://{host}|route_sha256={route_fp}"
    except (TypeError, ValueError):
        digest = hashlib.sha256(raw.encode()).hexdigest()
        return f"opaque-sha256:{digest}"


def credential_fingerprint(value: str | None) -> str:
    """Return a stable HMAC identity while keeping the credential off disk.

    API credentials are high-entropy HMAC keys.  Empty credentials use an
    explicit marker instead of sharing the digest of an empty secret, making a
    missing-configuration quarantine distinguishable during inspection.
    """
    credential = str(value or "")
    if not credential:
        return "missing-credential"
    return hmac.new(credential.encode(), _FINGERPRINT_DOMAIN,
                    hashlib.sha256).hexdigest()


def route_scopes(base_url: str | None, api_key: str | None,
                 model: str) -> tuple[tuple, tuple]:
    """Return provider and model quarantine keys for one resolved route."""
    url = canonical_base_url(base_url)
    fingerprint = credential_fingerprint(api_key)
    return (("prov", url, fingerprint),
            ("model", url, fingerprint, str(model)))


class RouteHealthStore:
    """Small process-safe SQLite quarantine shared by the deployment."""

    def __init__(self, shared_dir: Path):
        """Create/open ``shared/llm_health.db`` with restrictive permissions."""
        self.path = Path(shared_dir) / "llm_health.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @contextmanager
    def _conn(self):
        """Yield one short-lived connection safe across threads/processes."""
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def quarantine(self, scopes: tuple, scope: str) -> None:
        """Persist a provider- or model-scoped permanent quarantine."""
        key = scopes[0] if scope == "prov" else scopes[1]
        model = "" if scope == "prov" else key[3]
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO route_quarantine "
                "(base_url, credential_fp, scope, model, created_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (key[1], key[2], scope, model,
                 datetime.now(timezone.utc).isoformat()))

    def quarantine_scope(self, scopes: tuple) -> str | None:
        """Return ``prov``/``model`` when this exact route is quarantined."""
        provider, model = scopes
        with self._conn() as conn:
            provider_row = conn.execute(
                "SELECT 1 FROM route_quarantine WHERE base_url=? "
                "AND credential_fp=? AND scope='prov' AND model=''",
                (provider[1], provider[2])).fetchone()
            if provider_row:
                return "prov"
            model_row = conn.execute(
                "SELECT 1 FROM route_quarantine WHERE base_url=? "
                "AND credential_fp=? AND scope='model' AND model=?",
                (model[1], model[2], model[3])).fetchone()
            return "model" if model_row else None

    def clear_route(self, scopes: tuple) -> None:
        """Clear only quarantines disproved by a successful forced probe.

        A working model disproves provider/credential authentication failure
        and that model's own permission failure.  Other model-scoped rows on
        the same provider remain quarantined.
        """
        provider, model = scopes
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM route_quarantine WHERE base_url=? "
                "AND credential_fp=? AND (scope='prov' OR "
                "(scope='model' AND model=?))",
                (provider[1], provider[2], model[3]))
