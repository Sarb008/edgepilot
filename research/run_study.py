"""Run predeclared sensitivity study: python -m research.run_study --seeds 30"""
from __future__ import annotations

import argparse
from pathlib import Path
from .benchmark import export
from .figures import plot_deltas
from .statistics import summarize_paired
from .study import run_study, conditions


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='EdgePilot v2 paired sensitivity study')
    parser.add_argument('--seeds', type=int, default=30, help='Number of paired scenario seeds (default: 30)')
    parser.add_argument('--seed-start', type=int, default=1000, help='First scenario seed, for independent evaluation')
    parser.add_argument('--bootstrap', type=int, default=2000, help='Paired bootstrap samples')
    parser.add_argument('--output', type=Path, default=Path('research_outputs') / 'sensitivity')
    parser.add_argument('--no-plots', action='store_true')
    args = parser.parse_args(argv)
    if args.seeds < 2 or args.bootstrap < 1:
        parser.error('--seeds must be >= 2 and --bootstrap must be >= 1')
    seeds = range(args.seed_start, args.seed_start + args.seeds)
    trials = run_study(seeds, conditions())
    summary = summarize_paired(trials, bootstrap_samples=args.bootstrap)
    args.output.mkdir(parents=True, exist_ok=True)
    export(trials, args.output / 'trials.csv')
    export(summary, args.output / 'paired_deltas.csv')
    if not args.no_plots:
        plot_deltas(summary, args.output / 'paired_deltas.png')
    print(f'Completed {len(trials)} trials: {len(conditions())} conditions × {args.seeds} seeds × 4 policies')
    print(f'Wrote results to {args.output}')
    print('Differences vs greedy in percentage points (95% paired bootstrap intervals):')
    for row in summary:
        if row['policy'] == 'bounded_deferral':
            print(f"  {row['condition']:18s} {row['delta_percentage_points']:+6.2f} "
                  f"[{row['ci95_lower_pp']:+6.2f}, {row['ci95_upper_pp']:+6.2f}]")
    print('These are synthetic exploratory estimates, not statistical proof or Kubernetes measurements.')


if __name__ == '__main__':
    main()
