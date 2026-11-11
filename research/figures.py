"""Reproducible figures from already calculated study summaries."""
from __future__ import annotations
from pathlib import Path


def plot_deltas(summary: list[dict], path: str | Path) -> None:
    if not summary:
        raise ValueError('summary cannot be empty')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    conditions = sorted({row['condition'] for row in summary})
    policies = sorted({row['policy'] for row in summary if row['policy'] != row['reference']})
    by_pair = {(row['condition'], row['policy']): row for row in summary}
    fig, ax = plt.subplots(figsize=(max(9, len(conditions) * 1.5), 5.6))
    x = list(range(len(conditions)))
    for index, policy in enumerate(policies):
        points = [by_pair[(c, policy)] for c in conditions]
        offsets = [v + (index - (len(policies) - 1) / 2) * 0.15 for v in x]
        means = [r['delta_percentage_points'] for r in points]
        lower = [max(0.0, v - r['ci95_lower_pp']) for v, r in zip(means, points)]
        upper = [max(0.0, r['ci95_upper_pp'] - v) for v, r in zip(means, points)]
        ax.errorbar(offsets, means, yerr=[lower, upper], fmt='o', capsize=3, label=policy)
    ax.axhline(0, linestyle='--', linewidth=1, alpha=0.6)
    ax.set_xticks(x, [c.replace('_', '\n') for c in conditions])
    ax.set_xlabel('Synthetic scenario condition')
    ax.set_ylabel('On-time rate difference vs greedy (percentage points)')
    ax.set_title('Paired policy comparisons by scenario (95% bootstrap intervals)')
    ax.legend(loc='best')
    fig.tight_layout()
    fig.savefig(destination, dpi=180)
    plt.close(fig)
