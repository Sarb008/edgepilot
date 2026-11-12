"""Offline pooled report of predeclared age-aware vs bounded-deferral kind runs.

Runs are separate sequential live trials, NOT literal identical-state pairs.
Every raw run is passed through the existing strict evidence auditor first.
Accept only fully completed, balanced, nonduplicated, compatible runs. Preserve
all timeouts and refusals. Report descriptive counts, NOT causal effects or CIs.
"""
from __future__ import annotations

import argparse
import csv
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
from typing import Any

from .kube_wait_value import ARMS, WaitValueConfig
from .kube_wait_value_analysis import audit


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def pool(runs: tuple[Path, ...], output: Path) -> dict[str, Any]:
    if not runs:
        raise ValueError('at least one completed run is required')
    if output.exists():
        raise FileExistsError(f'output exists, refusing overwrite: {output}')
    paths = [p.resolve(strict=True) for p in runs]
    if len(set(paths)) != len(paths):
        raise ValueError('duplicate run path')
    all_blocks: list[dict[str, Any]] = []
    all_trials: list[dict[str, Any]] = []
    run_sources: list[dict[str, Any]] = []
    common_config: dict[str, Any] | None = None
    observed_seeds: set[int] = set()
    common_workers: set[str] | None = None
    for run_number, source in enumerate(paths, start=1):
        audited, report = audit(source)  # strict CSV/trace/source timestamp and cleanup checks
        manifest_file = source / 'manifest.json'
        manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
        config = WaitValueConfig(**manifest['config'])
        if config.repeats != 2:
            raise ValueError('only full two-repeat worker-and-position-balanced runs may be pooled')
        cfg = dict(manifest['config'])
        seed = cfg.pop('seed')
        if seed in observed_seeds:
            raise ValueError('duplicate randomized order seed across runs')
        observed_seeds.add(seed)
        if common_config is None:
            common_config = cfg
        elif cfg != common_config:
            raise ValueError('incompatible experimental configuration; stratify instead of pooling')
        worker_exposure = report['worker_exposure']
        if len(worker_exposure) != 2 or set(worker_exposure.values()) != {2}:
            raise ValueError('run must perturb each worker twice')
        if common_workers is None:
            common_workers = set(worker_exposure)
        elif set(worker_exposure) != common_workers:
            raise ValueError('worker identities changed across runs; refuse pooled interpretation')
        if len(audited) != 8:
            raise ValueError('expected 8 records in two-repeat run')
        by_block: dict[int, dict[str, dict[str, Any]]] = {}
        for row in audited:
            block, arm = int(row['block']), row['arm']
            arms = by_block.setdefault(block, {})
            if arm in arms:
                raise ValueError('duplicate policy in a block')
            arms[arm] = row
            all_trials.append({'run': run_number, 'seed': seed, **row})
        if set(by_block) != {1, 2, 3, 4}:
            raise ValueError('missing or extra block')
        for block, arms in sorted(by_block.items()):
            if set(arms) != set(ARMS):
                raise ValueError('incomplete paired block')
            age = arms['age_penalized']
            bounded = arms['bounded_deferral']
            if age['perturbed_worker'] != bounded['perturbed_worker']:
                raise ValueError('perturbed worker differs within paired block')
            av, bv = int(age['on_time_observed']), int(bounded['on_time_observed'])
            all_blocks.append({
                'run': run_number, 'seed': seed, 'block': block,
                'perturbed_worker': age['perturbed_worker'],
                'age_on_time': av, 'bounded_on_time': bv, 'on_time_delta': bv - av,
                'age_phase': age['terminal_phase'], 'bounded_phase': bounded['terminal_phase'],
                'age_selected': age['selected_worker'] or '',
                'bounded_selected': bounded['selected_worker'] or '',
                'age_feasible': str(age['selected_feasible_in_treatment']),
                'bounded_feasible': str(bounded['selected_feasible_in_treatment']),
                'bounded_wait_seconds': round(bounded['wait_seconds'], 6),
            })
        run_sources.append({'path': str(source), 'seed': seed,
                            'manifest_sha256': sha256(manifest_file.read_bytes()).hexdigest(),
                            'trials_sha256': report['source_sha256']['trials.csv'],
                            'sha256_file_count': len(report['source_sha256'])})
    if not all_blocks:
        raise ValueError('no complete blocks')
    workers = sorted({r['perturbed_worker'] for r in all_blocks})
    strata = []
    for worker in [*workers, 'ALL']:
        part = ([r for r in all_blocks if r['perturbed_worker'] == worker]
                if worker != 'ALL' else all_blocks)
        differences = [r['on_time_delta'] for r in part]
        strata.append({'perturbed_worker': worker, 'blocks': len(part),
                       'age_on_time': sum(r['age_on_time'] for r in part),
                       'bounded_on_time': sum(r['bounded_on_time'] for r in part),
                       'bounded_better': sum(d > 0 for d in differences),
                       'age_better': sum(d < 0 for d in differences),
                       'ties': sum(d == 0 for d in differences),
                       'descriptive_delta_percentage_points': round(100 * mean(differences), 6),
                       'mean_bounded_wait_seconds': round(mean(r['bounded_wait_seconds'] for r in part), 6),
                       'age_timeouts': sum(r['age_phase'] == 'TimedOutObservation' for r in part),
                       'bounded_timeouts': sum(r['bounded_phase'] == 'TimedOutObservation' for r in part)})
    summary = {
        'study': 'EdgePilot v2 Phase 2 / offline pooled sequential kind-crossover report',
        'runs': len(paths), 'blocks': len(all_blocks), 'trials': len(all_trials),
        'worker_exposure': {w: sum(r['perturbed_worker'] == w for r in all_blocks) for w in workers},
        'source_runs': run_sources, 'common_config_excluding_seed': common_config,
        'descriptive': strata,
        'limitations': [
            'Each block has two sequential arms with separate acquired snapshots, not identical live microstates.',
            'Blocks on one kind cluster/physical host may be temporally correlated.',
            'No causal or general-performance confidence intervals, hypothesis tests, or power claims.',
            'TimedOutObservation means not observed on time, not proof of permanent CPU admission failure.',
            'Kubernetes CPU requests are reservations, not measured utilization.',
            'The fixed data-collection plan must be reported even if outcomes are negative or incomplete.',
        ],
    }
    output.mkdir(parents=True, exist_ok=False)
    _write_csv(output / 'paired_blocks.csv', all_blocks)
    _write_csv(output / 'audited_trials.csv', all_trials)
    _write_csv(output / 'worker_strata.csv', strata)
    summary['output_sha256'] = {p.name: sha256(p.read_bytes()).hexdigest()
                                for p in sorted(output.iterdir()) if p.is_file()}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, default=Path('research_outputs/kubernetes/phase2_real_report'))
    args = parser.parse_args(argv)
    summary = pool(tuple(args.run), args.output)
    print(f'Audited and pooled {summary["trials"]} real Pod trials / {summary["blocks"]} blocks / {summary["runs"]} runs.')
    for row in summary['descriptive']:
        print(f'  {row["perturbed_worker"]:25} n={row["blocks"]:2} '
              f'age={row["age_on_time"]} bounded={row["bounded_on_time"]} '
              f'better/worse/tie={row["bounded_better"]}/{row["age_better"]}/{row["ties"]} '
              f'wait={row["mean_bounded_wait_seconds"]:.2f}s')
    print(f'Wrote {args.output}; DESCRIPTIVE ONLY, no Kubernetes calls or causality claims.')


if __name__ == '__main__':
    main()
