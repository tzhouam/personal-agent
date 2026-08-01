"""Bench results store (doc/BENCHMARKS.md §2.7): isolated files, never
`events.db`. Layout: `bench/results/<run-id>/` under the repo root —
git-ignored, 0700, raw outputs secrets-masked, pruned past 10 runs
(summaries are what history is made of; the raw outputs exist for
re-scoring recent runs)."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from assistant.platform.config import _REPO_ROOT
from assistant.platform.secrets import mask_secrets

RESULTS_ROOT = _REPO_ROOT / "bench" / "results"
_KEEP_RUNS = 10


class RunStore:
    """One run's directory: manifest, per-track JSONL items, raw outputs,
    summary — plus reference lookup for paired-delta reporting."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root or RESULTS_ROOT)
        self.run_id = datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M%S")
        self.dir = self.root / self.run_id
        self.dir.mkdir(parents=True, exist_ok=False)
        os.chmod(self.dir, 0o700)
        if self.root == RESULTS_ROOT:
            self._ensure_gitignore()

    def _ensure_gitignore(self) -> None:
        gi = self.root.parent / ".gitignore"
        if not gi.exists() or "results/" not in gi.read_text():
            gi.parent.mkdir(parents=True, exist_ok=True)
            with open(gi, "a") as f:
                f.write("results/\n")

    def write_manifest(self, manifest: dict) -> None:
        (self.dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2))

    def write_items(self, track: str, rows: list[dict]) -> None:
        """Per-item results, one JSON object per line; raw model output is
        masked before it touches disk."""
        with open(self.dir / f"{track}.items.jsonl", "w") as f:
            for row in rows:
                if "raw" in row:
                    row = {**row, "raw": mask_secrets(str(row["raw"]))[:2000]}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def write_summary(self, summary: dict) -> None:
        (self.dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2))
        self.prune()

    def prune(self) -> None:
        runs = sorted(p for p in self.root.iterdir()
                      if p.is_dir() and p.name.startswith("run-"))
        for old in runs[:-_KEEP_RUNS]:
            for f in old.iterdir():
                f.unlink()
            old.rmdir()

    def reference_summary(self) -> dict | None:
        """The most recent PREVIOUS run's summary (the paired-delta
        reference), or None on a first run."""
        runs = sorted(p for p in self.root.iterdir()
                      if p.is_dir() and p.name.startswith("run-")
                      and p.name != self.run_id)
        for run in reversed(runs):
            summary = run / "summary.json"
            if summary.exists():
                try:
                    return json.loads(summary.read_text())
                except ValueError:
                    continue
        return None
