"""PA-Mix run orchestration + report card (doc/BENCHMARKS.md §2.5/§2.7).

`run_tracks` runs tracks under the network guard on hermetic scratch
profiles, writes an isolated results run, and computes paired-delta
regressions ONLY against a comparable reference (same fixture hash, same
route fingerprint) — a changed fixture/model can't masquerade as a
regression. A directional track (too few items) never alerts. Regressions
are labeled unconfirmed pending an immediate rerun (§2.5)."""

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from assistant.bench import stats
from assistant.bench.results import RunStore
from assistant.bench.sandbox import bench_settings, network_guard, route_fingerprint
from assistant.bench.tracks import TRACKS
from assistant.platform.llm import LLM

log = logging.getLogger("assistant")

_MIN_REPS = 3
_MIN_ITEMS_TO_ALERT = 10


def _llm_hosts(settings) -> frozenset[str]:
    from urllib.parse import urlsplit

    hosts = set()
    urls = [settings.anthropic_base_url or "https://api.anthropic.com"]
    for spec in (settings.llm_roles or {}).values():
        if isinstance(spec, dict) and spec.get("base_url"):
            urls.append(spec["base_url"])
    mix = settings.llm_mixture or {}
    for spec in mix.get("members", []):
        if isinstance(spec, dict) and spec.get("base_url"):
            urls.append(spec["base_url"])
    agg = mix.get("aggregator")
    if isinstance(agg, dict) and agg.get("base_url"):
        urls.append(agg["base_url"])
    for url in urls:
        host = urlsplit(url).hostname
        if host:
            hosts.add(host)
    return frozenset(hosts)


def _comparable(cur_row: dict, cur_fp: dict, ref_row: dict,
                ref_fp: dict) -> bool:
    """A paired delta is meaningful only when BOTH runs are valid, both
    fixture hashes are present AND equal (a None==None match is not a match —
    it would let a hashless/changed subset compare), and the full route
    fingerprints match. Otherwise a changed item set or model would surface
    as a spurious 'regression'."""
    cur_hash = cur_row.get("manifest", {}).get("fixture_sha256")
    ref_hash = ref_row.get("manifest", {}).get("fixture_sha256")
    return bool(cur_row.get("valid") and ref_row.get("valid")
                and cur_hash and ref_hash and cur_hash == ref_hash
                and ref_fp == cur_fp)


def run_tracks(track_names: list[str], base_settings, reps: int = _MIN_REPS,
               seed: int = 0, llm_factory=None, results_root=None,
               guard_network: bool = True) -> dict:
    unknown = [n for n in track_names if n not in TRACKS]
    if unknown:
        raise ValueError(f"unknown track(s): {unknown} — have {sorted(TRACKS)}")
    llm_factory = llm_factory or (lambda s: LLM(s))
    fingerprint = route_fingerprint(base_settings)
    store = RunStore(root=results_root)
    reference = store.reference_summary()
    ref_fp = (reference or {}).get("route_fingerprint")

    summary: dict = {"run_id": store.run_id, "reps": reps, "seed": seed,
                     "route_fingerprint": fingerprint, "tracks": {}}
    scratch_base = Path(tempfile.mkdtemp(prefix="pa-bench-run-"))
    guard = (network_guard(_llm_hosts(base_settings)) if guard_network
             else _null_context())
    prev_tz = os.environ.get("TZ")
    os.environ["TZ"] = getattr(base_settings, "tz", "") or "Asia/Shanghai"
    try:
        time.tzset()
    except AttributeError:
        pass
    try:
        with guard:
            for name in track_names:
                track = TRACKS[name]
                scratch = scratch_base / name
                started = time.monotonic()
                per_item = track.run(base_settings, llm_factory, reps, seed,
                                     scratch)
                elapsed = round(time.monotonic() - started, 1)

                means = stats.item_means(per_item)
                acct = stats.rep_accounting(per_item)
                coverage = (len(means) / len(per_item)) if per_item else 0.0
                mean, lo, hi = stats.bootstrap_ci(list(means.values()), seed=seed)
                manifest = track.manifest()
                directional = (getattr(track, "directional", False)
                               or len(per_item) < _MIN_ITEMS_TO_ALERT
                               or reps < _MIN_REPS)
                row = {"manifest": manifest, "score": round(mean, 4),
                       "ci": [round(lo, 4), round(hi, 4)],
                       "n_items": len(per_item), "n_scored": len(means),
                       "coverage": round(coverage, 3),
                       "valid": coverage >= 0.9,
                       "directional": directional,
                       "reps": acct, "seconds": elapsed,
                       "item_means": {k: round(v, 4) for k, v in means.items()}}
                ref_row = ((reference or {}).get("tracks") or {}).get(name)
                if (ref_row and not directional
                        and _comparable(row, fingerprint, ref_row,
                                        ref_fp or {})):
                    d = stats.paired_delta(means, ref_row.get("item_means", {}),
                                           seed=seed)
                    if not d.get("directional_only"):
                        row["delta_vs"] = reference["run_id"]
                        row["delta"] = d
                summary["tracks"][name] = row
                store.write_items(name, per_item)
        store.write_manifest({n: TRACKS[n].manifest() for n in track_names})
        store.write_summary(summary)
    finally:
        shutil.rmtree(scratch_base, ignore_errors=True)
        if prev_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev_tz
        try:
            time.tzset()
        except AttributeError:
            pass
    return summary


class _null_context:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def render_report(summary: dict) -> str:
    fp = summary.get("route_fingerprint", {})
    lines = [f"PA-Mix {summary['run_id']}  (reps={summary['reps']}, "
             f"model {fp.get('default_model', '?')})",
             "─" * 72]
    for name in sorted(summary.get("tracks", {})):
        row = summary["tracks"][name]
        m = row["manifest"]
        tag = "directional" if row.get("directional") else "alerting"
        line = (f"{name:16s} {row['score']:.3f} "
                f"[{row['ci'][0]:.3f},{row['ci'][1]:.3f}] "
                f"n={row['n_scored']}/{row['n_items']}×{summary['reps']} "
                f"{m.get('layer', '?')}/{m.get('label', '?')} {tag}")
        if not row.get("valid"):
            line += "  ⚠ INVALID (<90% coverage)"
        if row["reps"]["reps_infra"]:
            line += f"  ({row['reps']['reps_infra']} infra reps)"
        delta = row.get("delta")
        if delta:
            sign = f"{delta['delta_mean']:+.3f}"
            line += f"  Δ{sign} vs {row['delta_vs'][:16]}"
            if delta.get("regressed"):
                line += "  ⚠ REGRESSED (unconfirmed — rerun to confirm)"
        lines.append(line)
        if m.get("label") == "official-subset":
            lines.append(" " * 17 + "(subset — never comparable to the full "
                         "benchmark or its leaderboard)")
        elif m.get("label") == "derived":
            lines.append(" " * 17 + "(derived/custom — not the source "
                         "benchmark's score)")
    return "\n".join(lines)
