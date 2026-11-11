"""Seed-paired nonparametric bootstrap summaries; descriptive uncertainty only."""
from __future__ import annotations

import math
import random
from statistics import fmean
from typing import Sequence


def paired_bootstrap(values: Sequence[float], samples: int = 2000,
                     confidence: float = 0.95, seed: int = 2026) -> tuple[float, float, float]:
    """Return (sample mean, lower, upper) for paired per-seed differences.

    Resampling units are *scenario seeds*, NOT jobs or policies within a seed.
    Percentile intervals are descriptive and do not correct multiple testing.
    """
    if not values or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
        raise ValueError('values must be a nonempty finite sample')
    if isinstance(samples, bool) or not isinstance(samples, int) or samples < 1:
        raise ValueError('samples must be a positive integer')
    if not 0 < confidence < 1:
        raise ValueError('confidence must be between zero and one')
    if len(values) == 1:
        value = float(values[0])
        return value, value, value
    rng = random.Random(seed)
    n = len(values)
    draws = sorted(fmean(values[rng.randrange(n)] for _ in range(n)) for _ in range(samples))
    alpha = (1 - confidence) / 2

    def quantile(p: float) -> float:
        position = p * (len(draws) - 1)
        lower = math.floor(position)
        upper = math.ceil(position)
        return draws[lower] + (draws[upper] - draws[lower]) * (position - lower)

    return fmean(values), quantile(alpha), quantile(1 - alpha)


def summarize_paired(rows: list[dict], reference: str = 'greedy',
                     metric: str = 'on_time_rate', bootstrap_samples: int = 2000,
                     bootstrap_seed: int = 2026) -> list[dict]:
    """Produce one policy-vs-reference summary per condition in percentage points.

    Incomplete pairs and duplicate trials are rejected instead of silently
    changing the evaluated population across policies.
    """
    if not rows:
        raise ValueError('cannot summarize an empty study')
    grouped: dict[str, dict[str, dict[int, float]]] = {}
    for row in rows:
        condition, policy, seed = row['condition'], row['policy'], row['seed']
        value = row[metric]
        if not math.isfinite(value):
            raise ValueError('nonfinite metric')
        by_policy = grouped.setdefault(condition, {}).setdefault(policy, {})
        if seed in by_policy:
            raise ValueError(f'duplicate trial for {condition}, {policy}, seed {seed}')
        by_policy[seed] = value
    output: list[dict] = []
    for condition in sorted(grouped):
        by_policy = grouped[condition]
        if reference not in by_policy:
            raise ValueError(f'missing reference policy in condition {condition}')
        reference_values = by_policy[reference]
        seed_ids = sorted(reference_values)
        for policy in sorted(by_policy):
            values = by_policy[policy]
            if set(values) != set(reference_values):
                raise ValueError(f'unpaired seeds for {condition}: {policy}')
            differences_pp = [100 * (values[s] - reference_values[s]) for s in seed_ids]
            mean, low, high = paired_bootstrap(differences_pp, bootstrap_samples, seed=bootstrap_seed)
            output.append({
                'condition': condition,
                'policy': policy,
                'reference': reference,
                'seeds': len(seed_ids),
                'mean_on_time_rate': fmean(values[s] for s in seed_ids),
                'reference_mean_on_time_rate': fmean(reference_values[s] for s in seed_ids),
                'delta_percentage_points': mean,
                'ci95_lower_pp': low,
                'ci95_upper_pp': high,
            })
    return output
