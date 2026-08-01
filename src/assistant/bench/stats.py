"""Bench statistics (doc/BENCHMARKS.md §2.5).

The resampling unit is the ITEM: repetitions are averaged per item first
(N×n outputs treated as independent would pseudoreplicate), then a bootstrap
CI is taken over item means. Regression alerting uses a PAIRED bootstrap
over per-item deltas against the reference run — never overlapping marginal
CIs, never fixed point-drops. Deterministic via a caller-supplied seed."""

import random
import statistics


def item_means(per_item_scores: dict[str, list]) -> dict[str, float]:
    """Average each item's per-rep scores. Accepts either raw floats/Nones or
    `{"score": ...}` reps. `None` reps (classified infra failures) are
    dropped per item; an item with no valid rep at all is excluded (counted
    by the caller via `rep_accounting`)."""
    out = {}
    for item_id, reps in per_item_scores.items():
        valid = [_score(r) for r in reps if _score(r) is not None]
        if valid:
            out[item_id] = sum(valid) / len(valid)
    return out


def _score(rep):
    return rep["score"] if isinstance(rep, dict) else rep


def rep_accounting(per_item_scores: dict[str, list]) -> dict:
    """Rep-level counts so partial-rep infra failures can't hide: total reps,
    valid reps, infra (None) reps, and items with fewer valid reps than
    requested."""
    total = valid = infra = 0
    partial_items = []
    for item_id, reps in per_item_scores.items():
        v = sum(1 for r in reps if _score(r) is not None)
        total += len(reps)
        valid += v
        infra += len(reps) - v
        if 0 < v < len(reps):
            partial_items.append(item_id)
    return {"reps_total": total, "reps_valid": valid, "reps_infra": infra,
            "items_with_partial_reps": partial_items}


def bootstrap_ci(values: list[float], seed: int = 0, iters: int = 2000,
                 alpha: float = 0.05) -> tuple[float, float, float]:
    """(mean, lo, hi): percentile bootstrap CI over item means."""
    if not values:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    mean = statistics.fmean(values)
    n = len(values)
    means = sorted(statistics.fmean(rng.choices(values, k=n))
                   for _ in range(iters))
    lo = means[int((alpha / 2) * iters)]
    hi = means[min(int((1 - alpha / 2) * iters), iters - 1)]
    return mean, lo, hi


def paired_delta(current: dict[str, float], reference: dict[str, float],
                 seed: int = 0, iters: int = 2000,
                 alpha: float = 0.05) -> dict:
    """Paired bootstrap over per-item deltas on the COMMON item set.
    Returns {n_common, delta_mean, lo, hi, regressed} — `regressed` iff the
    delta CI lies entirely below zero. The caller applies the rerun-confirm
    policy before alerting."""
    common = sorted(set(current) & set(reference))
    if len(common) < 5:
        return {"n_common": len(common), "delta_mean": 0.0, "lo": 0.0,
                "hi": 0.0, "regressed": False, "directional_only": True}
    deltas = [current[i] - reference[i] for i in common]
    mean, lo, hi = bootstrap_ci(deltas, seed=seed, iters=iters, alpha=alpha)
    return {"n_common": len(common), "delta_mean": mean, "lo": lo, "hi": hi,
            "regressed": hi < 0.0, "directional_only": False}
