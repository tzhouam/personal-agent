"""Bench results store (doc/BENCHMARKS.md §2.7): isolated files, never
`events.db`. Layout: `bench/results/<run-id>/` under the repo root —
git-ignored (root .gitignore, committed, not created at runtime), 0700, raw
outputs secrets-masked and retained for re-scoring, pruned past 10 runs
EXCEPT summaries, which are archived permanently."""

import json
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from assistant.platform.config import _REPO_ROOT
from assistant.platform.secrets import mask_secrets

RESULTS_ROOT = _REPO_ROOT / "bench" / "results"
_KEEP_RUNS = 10


class _UnsafeResultPayload(TypeError):
    """A benchmark result crossed the documented JSON-only boundary."""


def _validate_json_value(value, active: set[int] | None = None) -> None:
    """Validate the strict retained-raw schema without coercing user objects.

    Calling ``str``/``repr``/``asdict`` on an unknown value can execute custom
    code or persist hidden fields and exception messages. Producers must
    project exact supported contracts before this boundary; the writer accepts
    only finite JSON scalars, string-keyed plain dictionaries, and plain lists.
    """
    kind = type(value)
    if value is None or kind in (bool, int):
        return
    if kind is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise _UnsafeResultPayload from None
        return
    if kind is float:
        if math.isfinite(value):
            return
        raise _UnsafeResultPayload
    if kind not in (dict, list):
        raise _UnsafeResultPayload

    active = active if active is not None else set()
    identity = id(value)
    if identity in active:
        raise _UnsafeResultPayload
    active.add(identity)
    try:
        if kind is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise _UnsafeResultPayload
                _validate_json_value(key, active)
                _validate_json_value(item, active)
        else:
            for item in value:
                _validate_json_value(item, active)
    finally:
        active.remove(identity)


def _masked_json(value, location: str) -> str:
    """Return deterministic masked JSON or one privacy-safe structural error."""
    try:
        _validate_json_value(value)
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError(
            f"benchmark result is not JSON-compatible at {location}") from None
    return mask_secrets(encoded)


class RunStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or RESULTS_ROOT)
        # run id: UTC second + short uuid so rapid/concurrent runs never
        # collide
        self.run_id = (datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M%S")
                       + "-" + uuid.uuid4().hex[:6])
        self.dir = self.root / self.run_id
        self.dir.mkdir(parents=True, exist_ok=False)
        os.chmod(self.dir, 0o700)
        (self.root / "summaries").mkdir(exist_ok=True)

    def write_manifest(self, manifest: dict) -> None:
        (self.dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2))

    def write_items(self, track: str, per_item: dict) -> None:
        """Per-item results incl. RAW model output/traces (retained for
        re-scoring), masked before disk.

        The complete JSONL is validated and assembled before an atomic replace,
        so one invalid late rep cannot leave a plausible-looking partial run.
        """
        if type(per_item) is not dict:
            raise ValueError(
                "benchmark result is not JSON-compatible at track payload")
        lines: list[str] = []
        for row_index, (item_id, reps) in enumerate(per_item.items()):
            if type(reps) is not list:
                raise ValueError(
                    f"benchmark result is not JSON-compatible at row {row_index}")
            safe_reps = []
            for rep_index, result in enumerate(reps):
                if type(result) is not dict:
                    raise ValueError(
                        "benchmark result is not JSON-compatible at "
                        f"row {row_index} rep {rep_index}")
                payload = {
                    "score": result.get("score"),
                    "raw": result.get("raw"),
                }
                # Validate each rep first so the error identifies structural
                # position without ever formatting the offending value or an
                # external fixture id.
                _masked_json(payload, f"row {row_index} rep {rep_index}")
                safe_reps.append(payload)
            row = {"item": item_id, "reps": safe_reps}
            lines.append(_masked_json(row, f"row {row_index}") + "\n")

        target = self.dir / f"{track}.items.jsonl"
        tmp = target.with_name(f".{target.name}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                os.chmod(tmp, 0o600)
                handle.writelines(lines)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)

    def write_summary(self, summary: dict) -> None:
        text = json.dumps(summary, ensure_ascii=False, indent=2)
        (self.dir / "summary.json").write_text(text)
        # a permanent copy so pruning heavy runs never loses history
        (self.root / "summaries" / f"{self.run_id}.json").write_text(text)
        self.prune()

    def prune(self) -> None:
        """Delete the heavy files of runs past the keep window — never the
        archived summaries."""
        runs = sorted(p for p in self.root.iterdir()
                      if p.is_dir() and p.name.startswith("run-"))
        for old in runs[:-_KEEP_RUNS]:
            for f in old.iterdir():
                f.unlink()
            old.rmdir()

    def reference_summary(self) -> dict | None:
        """The most recent PREVIOUS run's archived summary (the paired-delta
        reference candidate), or None."""
        archive = self.root / "summaries"
        if not archive.exists():
            return None
        others = sorted(p for p in archive.glob("run-*.json")
                        if p.stem != self.run_id)
        for path in reversed(others):
            try:
                return json.loads(path.read_text())
            except ValueError:
                continue
        return None
