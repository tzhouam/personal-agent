"""Bench results store (doc/BENCHMARKS.md §2.7): isolated files, never
`events.db`. Layout: `bench/results/<run-id>/` under the repo root —
git-ignored (root .gitignore, committed, not created at runtime), 0700, raw
outputs secrets-masked and retained for re-scoring, pruned past 10 runs
EXCEPT summaries, which are archived permanently."""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from assistant.platform.config import _REPO_ROOT
from assistant.platform.secrets import mask_secrets

RESULTS_ROOT = _REPO_ROOT / "bench" / "results"
_KEEP_RUNS = 10


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
        re-scoring), masked before disk."""
        with open(self.dir / f"{track}.items.jsonl", "w") as f:
            for item_id, reps in per_item.items():
                safe = [{"score": r.get("score") if isinstance(r, dict) else r,
                         "raw": json.loads(mask_secrets(
                             json.dumps(r.get("raw"), ensure_ascii=False)))
                         if isinstance(r, dict) and r.get("raw") is not None
                         else None}
                        for r in reps]
                f.write(json.dumps({"item": item_id, "reps": safe},
                                   ensure_ascii=False) + "\n")

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
