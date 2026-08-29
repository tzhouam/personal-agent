"""Durable per-route health for research feeds.

Only opaque route fingerprints, counters, and timestamps are persisted.  The
source URL/config and exception text never enter this file: feed URLs can carry
private tokens, and this deployment-shared state must be safe for every tenant
to reuse.  A small flock-protected JSON transaction makes the cooldown and its
half-open probe claim atomic across worker processes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

from assistant.platform.locks import _path_lock

_STATE_VERSION = 1
_FAILURE_THRESHOLD = 3
_MAX_FAILURE_THRESHOLD = 100
_DEFAULT_COOLDOWN_HOURS = 72.0
_MAX_COOLDOWN_HOURS = 24.0 * 365
_MAX_STATE_COUNTER = (1 << 63) - 2
# Feed HTTP operations have short component timeouts, but redirects and a slow
# streaming body can span more than one component timeout.  A long lease still
# recovers automatically after a crashed probe without admitting two ordinary
# concurrent half-open requests.
_PROBE_LEASE = timedelta(minutes=15)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    return _as_utc(parsed)


def _nonnegative_int(value) -> int:
    """Return one bounded state counter; corrupt/extreme values fail open."""
    if isinstance(value, bool):
        return 0
    try:
        if isinstance(value, float) and (
                not math.isfinite(value) or not value.is_integer()):
            return 0
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if 0 <= parsed <= _MAX_STATE_COUNTER else 0


def _bounded_cooldown_hours(value) -> float:
    """Normalize policy input without allowing NaN/inf or timedelta overflow."""
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_COOLDOWN_HOURS
    if not math.isfinite(parsed):
        return _DEFAULT_COOLDOWN_HOURS
    return min(_MAX_COOLDOWN_HOURS, max(0.0, parsed))


def _bounded_failure_threshold(value) -> int:
    """Normalize the internal policy component included in route identity."""
    if isinstance(value, bool):
        return _FAILURE_THRESHOLD
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return _FAILURE_THRESHOLD
    return min(_MAX_FAILURE_THRESHOLD, max(1, parsed))


def source_fingerprint(source: Mapping, *,
                       cooldown_hours: float = _DEFAULT_COOLDOWN_HOURS,
                       failure_threshold: int = _FAILURE_THRESHOLD) -> str:
    """Opaque identity for the complete immutable source route/config.

    Canonical JSON means key order does not affect identity.  Any URL or config
    change intentionally produces a new key and therefore bypasses an old
    quarantine immediately. Cooldown and failure-threshold policy are part of
    the identity so tenants/configurations with different policies cannot
    mutate one another's deployment-shared state.
    """
    cooldown = timedelta(hours=_bounded_cooldown_hours(cooldown_hours))
    canonical = json.dumps(
        {
            "policy": {
                "cooldown_seconds": cooldown.total_seconds(),
                "failure_threshold": _bounded_failure_threshold(failure_threshold),
            },
            "source": dict(source),
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(b"personal-agent/feed-route/v1\0" + canonical).hexdigest()


@dataclass(frozen=True)
class FeedAttempt:
    """One source's decision for this run.

    ``fetch`` and ``probe`` are the only modes that permit an HTTP attempt;
    ``cooling`` and ``skipped`` describe why no request should be made.
    """

    fingerprint: str
    mode: str
    token: str = ""
    retry_at: str = ""
    generation: int = 0

    @property
    def should_fetch(self) -> bool:
        return self.mode in ("fetch", "probe")


class FeedHealthStore:
    """Atomic persistent circuit health rooted in deployment-shared data.

    Cooldown ``0`` is a temporary, stateless bypass: it neither consults nor
    clears an existing policy-scoped state entry. Re-enabling the same positive
    policy therefore resumes that prior entry rather than silently resetting it.
    """

    def __init__(self, shared_dir: Path, cooldown_hours: float = 72,
                 now: Callable[[], datetime] | None = None,
                 failure_threshold: int = _FAILURE_THRESHOLD):
        self.path = Path(shared_dir) / "research-feed-health.json"
        self.lock_path = Path(shared_dir) / "research-feed-health.lock"
        self.cooldown_hours = _bounded_cooldown_hours(cooldown_hours)
        self.cooldown = timedelta(hours=self.cooldown_hours)
        self.failure_threshold = _bounded_failure_threshold(failure_threshold)
        self._now = now or _utc_now

    @property
    def enabled(self) -> bool:
        return self.cooldown.total_seconds() > 0

    def claim(self, source: Mapping) -> FeedAttempt:
        """Return this run's action and atomically claim an expired probe.

        State I/O is deliberately fail-open: an unwritable/corrupt optional
        health cache must not turn a research digest into a hard failure.
        """
        fingerprint = source_fingerprint(
            source,
            cooldown_hours=self.cooldown_hours,
            failure_threshold=self.failure_threshold,
        )
        if not self.enabled:
            return FeedAttempt(fingerprint, "fetch")
        try:
            with _path_lock(self.lock_path):
                state = self._load()
                entry = state["sources"].get(fingerprint, {})
                failures = _nonnegative_int(entry.get("consecutive_failures", 0))
                if failures < self.failure_threshold:
                    generation = _nonnegative_int(entry.get("generation", 0)) + 1
                    state["sources"][fingerprint] = {
                        "consecutive_failures": failures,
                        "generation": generation,
                    }
                    self._write(state)
                    return FeedAttempt(
                        fingerprint, "fetch", generation=generation)

                now = _as_utc(self._now())
                cooldown_until = _parse_time(entry.get("cooldown_until"))
                if cooldown_until is not None and now < cooldown_until:
                    return FeedAttempt(
                        fingerprint, "cooling", retry_at=cooldown_until.isoformat())

                lease_until = _parse_time(entry.get("probe_lease_until"))
                if lease_until is not None and now < lease_until:
                    return FeedAttempt(
                        fingerprint, "skipped", retry_at=lease_until.isoformat())

                token = uuid.uuid4().hex
                generation = _nonnegative_int(entry.get("generation", 0)) + 1
                entry.update({
                    "consecutive_failures": failures,
                    "generation": generation,
                    "probe_token": token,
                    "probe_lease_until": (now + _PROBE_LEASE).isoformat(),
                })
                state["sources"][fingerprint] = entry
                self._write(state)
                return FeedAttempt(
                    fingerprint, "probe", token=token, generation=generation)
        except (OSError, TypeError, ValueError, OverflowError):
            return FeedAttempt(fingerprint, "fetch")

    def record_success(self, attempt: FeedAttempt) -> None:
        """Close the route and clear failures while retaining its generation.

        The zero-failure tombstone is intentional: deleting the row would let a
        later claim restart at generation 1, so a very late generation-1 result
        from before the success could become current again.
        """
        if not self.enabled:
            return
        try:
            with _path_lock(self.lock_path):
                state = self._load()
                entry = state["sources"].get(attempt.fingerprint)
                if not entry:
                    return
                if _nonnegative_int(entry.get("generation")) != attempt.generation:
                    return  # an older ordinary request finished out of order
                if attempt.mode == "probe" and entry.get("probe_token") != attempt.token:
                    return  # a stale lease holder must not close newer health
                state["sources"][attempt.fingerprint] = {
                    "consecutive_failures": 0,
                    "generation": attempt.generation,
                }
                self._write(state)
        except (OSError, TypeError, ValueError, OverflowError):
            return

    def record_failure(self, attempt: FeedAttempt) -> None:
        """Increment failures and enter cooldown at the configured threshold."""
        if not self.enabled:
            return
        try:
            with _path_lock(self.lock_path):
                state = self._load()
                entry = state["sources"].get(attempt.fingerprint)
                if not entry or \
                        _nonnegative_int(entry.get("generation")) != attempt.generation:
                    return  # a newer claim/result owns the route now
                if attempt.mode == "probe" and entry.get("probe_token") != attempt.token:
                    return  # the lease expired and a newer probe owns the row
                failures = _nonnegative_int(entry.get("consecutive_failures", 0)) + 1
                now = _as_utc(self._now())
                updated = {
                    "consecutive_failures": failures,
                    "generation": attempt.generation,
                    "last_failure_at": now.isoformat(),
                }
                if failures >= self.failure_threshold:
                    updated["cooldown_until"] = (now + self.cooldown).isoformat()
                state["sources"][attempt.fingerprint] = updated
                self._write(state)
        except (OSError, TypeError, ValueError, OverflowError):
            return

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeError):
            return {"version": _STATE_VERSION, "sources": {}}
        if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
            return {"version": _STATE_VERSION, "sources": {}}
        # Unknown fields at every level are discarded so old/corrupt state can
        # never become a side channel for arbitrary content on the next write.
        sources = {}
        for fingerprint, entry in data["sources"].items():
            if (isinstance(fingerprint, str) and len(fingerprint) == 64
                    and all(ch in "0123456789abcdef" for ch in fingerprint)
                    and isinstance(entry, dict)):
                clean = {
                    "consecutive_failures": _nonnegative_int(
                        entry.get("consecutive_failures")),
                    "generation": _nonnegative_int(entry.get("generation")),
                }
                for key in ("last_failure_at", "cooldown_until",
                            "probe_lease_until"):
                    timestamp = _parse_time(entry.get(key))
                    if timestamp is not None:
                        clean[key] = timestamp.isoformat()
                token = entry.get("probe_token")
                if (isinstance(token, str) and len(token) == 32
                        and all(ch in "0123456789abcdef" for ch in token)):
                    clean["probe_token"] = token
                sources[fingerprint] = clean
        return {"version": _STATE_VERSION, "sources": sources}

    def _write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        payload = json.dumps(
            {"version": _STATE_VERSION, "sources": state["sources"]},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fd = -1
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
