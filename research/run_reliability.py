"""Run fixed, paired synthetic reliability stress scenarios."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
import sys
from pathlib import Path

from .benchmark import export
from .figures import plot_deltas
from .reliability_study import run_reliability_study, scenarios
from .statistics import summarize_paired
from .study import Condition, policies


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description='EdgePilot reliability stress study (synthetic)')
    p.add_argument('--seeds', type=int, default=30)
    p.add_argument('--seed-start', type=int, default=2000)
    p.add_argument('--bootstrap', type=int, default=2000)
    p.add_argument('--output', type=Path, default=Path('research_outputs') / 'reliability')
    p.add_argument('--no-plots', action='store_true')
    args = p.parse_args(argv)
    if args.seeds < 2 or args.bootstrap < 1:
        p.error('seeds must be >=2 and bootstrap must be >=1')
    rows = run_reliability_study(range(args.seed_start, args.seed_start + args.seeds))
    summary = summarize_paired(rows, bootstrap_samples=args.bootstrap)
    args.output.mkdir(parents=True, exist_ok=True)
    export(rows, args.output / 'trials.csv')
    export(summary, args.output / 'paired_deltas.csv')
    if not args.no_plots:
        plot_deltas(summary, args.output / 'paired_deltas.png')
    # Machine-readable provenance. No runtime clock or nondeterministic fields.
    manifest = {
        'experiment': 'edgepilot-v2-reliability-stress-v1',
        'synthetic_only': True,
        'python': '.'.join(map(str, sys.version_info[:3])),
        'seed_start': args.seed_start,
        'seed_count': args.seeds,
        'bootstrap_samples': args.bootstrap,
        'baseline_condition': asdict(Condition('stress_baseline')),
        'scenarios': [asdict(s) for s in scenarios()],
        'policies': {name: asdict(policy) for name, policy in policies().items()},
        'bounded_deferral_placement_latency': 'set to known scenario dispatch_delay',
        'trials_sha256': hashlib.sha256((args.output / 'trials.csv').read_bytes()).hexdigest(),
        'paired_deltas_sha256': hashlib.sha256((args.output / 'paired_deltas.csv').read_bytes()).hexdigest(),
    }
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(f'Completed {len(rows)} trials: {len(scenarios())} scenarios × {args.seeds} seeds × 4 policies')
    print(f'Exported to: {args.output}')
    print('Bounded deferral vs greedy (on-time percentage points; 95% paired bootstrap intervals):')
    for r in summary:
        if r['policy'] == 'bounded_deferral':
            print(f"  {r['condition']:21s} {r['delta_percentage_points']:+6.2f} "
                  f"[{r['ci95_lower_pp']:+6.2f}, {r['ci95_upper_pp']:+6.2f}]")
    print('Synthetic sensitivity analysis only; not field-validation or a proof of superiority.')


if __name__ == '__main__':
    main()
