"""PA-Mix run orchestration + report card (doc/BENCHMARKS.md §2.5/§2.7).

`run_tracks` executes tracks under the network guard, writes an isolated
results run (manifest, per-item JSONL, summary), and computes paired-delta
regressions against the previous run's summary. `render_report` prints the
fixed-order per-track card — no composite number by design."""

import logging
import time

from assistant.bench import stats
from assistant.bench.results import RunStore
from assistant.bench.sandbox import bench_settings, network_guard
from assistant.bench.tracks import TRACKS
from assistant.platform.llm import LLM

log = logging.getLogger("assistant")

_DEFAULT_REPS = 3


def _llm_hosts(settings) -> frozenset[str]:
    """The only hosts a bench run may reach: every endpoint LLM_ROLES could
    route to (plus the default base URL)."""
    from urllib.parse import urlsplit

    hosts = set()
    for url in [settings.anthropic_base_url or "https://api.anthropic.com"] + [
            spec.get("base_url") for spec in (settings.llm_roles or {}).values()
            if isinstance(spec, dict)]:
        if url:
            host = urlsplit(url).hostname
            if host:
                hosts.add(host)
    return frozenset(hosts)


def run_tracks(track_names: list[str], base_settings, reps: int = _DEFAULT_REPS,
               seed: int = 0, llm_factory=None, settings_factory=None,
               results_root=None, guard_network: bool = True) -> dict:
    """Run the named tracks and persist one results run. Factories are
    injectable for tests; by default each item/scenario gets a fresh hermetic
    bench profile and a real LLM on it. Returns the summary dict."""
    unknown = [n for n in track_names if n not in TRACKS]
    if unknown:
        raise ValueError(f"unknown track(s): {unknown} — have {sorted(TRACKS)}")
    settings_factory = settings_factory or bench_settings
    llm_factory = llm_factory or (lambda s: LLM(s))
    store = RunStore(root=results_root)
    reference = store.reference_summary()

    summary: dict = {"run_id": store.run_id, "reps": reps, "seed": seed,
                     "llm_roles": dict(base_settings.llm_roles or {}),
                     "default_model": base_settings.anthropic_model,
                     "tracks": {}}
    guard = (network_guard(_llm_hosts(base_settings)) if guard_network
             else _null_context())
    with guard:
        for name in track_names:
            track = TRACKS[name]
            started = time.monotonic()
            per_item = track.run(settings_factory, llm_factory, reps, seed)
            elapsed = round(time.monotonic() - started, 1)

            means = stats.item_means(per_item)
            infra_excluded = sum(1 for scores in per_item.values()
                                 if not any(s is not None for s in scores))
            coverage = (len(means) / len(per_item)) if per_item else 0.0
            mean, lo, hi = stats.bootstrap_ci(list(means.values()), seed=seed)
            row = {"manifest": track.manifest(),
                   "score": round(mean, 4),
                   "ci": [round(lo, 4), round(hi, 4)],
                   "n_items": len(per_item), "n_scored": len(means),
                   "infra_excluded": infra_excluded,
                   "coverage": round(coverage, 3),
                   "valid": coverage >= 0.9,   # §2.5 validity floor
                   "seconds": elapsed,
                   "item_means": {k: round(v, 4) for k, v in means.items()}}
            ref_row = ((reference or {}).get("tracks") or {}).get(name)
            if ref_row and ref_row.get("valid"):
                row["delta_vs"] = reference["run_id"]
                row["delta"] = stats.paired_delta(
                    means, ref_row.get("item_means", {}), seed=seed)
            summary["tracks"][name] = row
            store.write_items(name, [
                {"item": item_id, "scores": scores}
                for item_id, scores in per_item.items()])
    store.write_manifest({name: TRACKS[name].manifest()
                          for name in track_names})
    store.write_summary(summary)
    return summary


class _null_context:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def render_report(summary: dict) -> str:
    """The fixed-order per-track card. Regressions are labeled UNCONFIRMED —
    the §2.5 policy is that an alert counts only after an immediate rerun
    reproduces it; the card cannot know that, so it never overstates."""
    lines = [f"PA-Mix {summary['run_id']}  (reps={summary['reps']}, "
             f"default model {summary.get('default_model', '?')})",
             "─" * 72]
    for name in sorted(summary.get("tracks", {})):
        row = summary["tracks"][name]
        m = row["manifest"]
        line = (f"{name:20s} {row['score']:.3f} "
                f"[{row['ci'][0]:.3f},{row['ci'][1]:.3f}] "
                f"n={row['n_scored']}/{row['n_items']}×{summary['reps']} "
                f"{m.get('layer', '?')}/{m.get('label', '?')}")
        if not row.get("valid"):
            line += "  ⚠ INVALID (<90% coverage)"
        delta = row.get("delta")
        if delta and not delta.get("directional_only"):
            sign = f"{delta['delta_mean']:+.3f}"
            if delta.get("regressed"):
                line += (f"  Δ{sign} vs {row['delta_vs']} "
                         "⚠ REGRESSED (unconfirmed — rerun to confirm)")
            else:
                line += f"  Δ{sign} vs {row['delta_vs']}"
        lines.append(line)
        if m.get("label") == "official-subset":
            lines.append(" " * 21 + "(subset — never comparable to the full "
                         "benchmark or its leaderboard)")
    return "\n".join(lines)
