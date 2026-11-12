"""M6F: strictly OFFLINE balanced, worker-stratified descriptive evidence report.

Never contacts a cluster or changes input data. In particular, this report
cannot establish policy causality because Kubernetes trials were sequential.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Sequence

from .kube_feasibility_analysis import EvidenceError, audit_runs, load_run
from .kube_pilot import POLICIES

STRATUM_FIELDS = (
    'perturbed_worker', 'policy', 'trials', 'on_time', 'on_time_rate',
    'refusals', 'observation_timeouts', 'recorded_insufficient_cpu',
    'fresh_infeasible_selections', 'selected_perturbed_worker',
    'delta_on_time_vs_greedy_count', 'delta_on_time_vs_greedy_pp',
)
PAIR_FIELDS = ('run_id', 'block', 'perturbed_worker', 'policy',
               'policy_on_time', 'greedy_on_time', 'delta_on_time',
               'selected_worker', 'fresh_reference_worker')


def analyze(directories: Sequence[Path]) -> dict[str, Any]:
    """Require two worker strata with exactly equal full-block exposure."""
    audited = audit_runs(directories)  # rejects partial/inconsistent runs
    if not audited['comparable_configurations']:
        raise EvidenceError('different workload parameters: analyze separately')
    all_trials = []
    for run_id, run in enumerate(directories, 1):
        trials, _ = load_run(Path(run))
        all_trials.extend((run_id, trial) for trial in trials)
    blocks = audited['by_block']
    counts = audited['perturbation_target_blocks']
    if len(counts) != 2 or len(set(counts.values())) != 1:
        raise EvidenceError(f'balanced two-worker exposure required; found {counts}. '
                            'Do not pool the unbalanced initial one-block pilot.')
    if any(t.perturbed_worker not in counts for _, t in all_trials):
        raise EvidenceError('worker mismatch between audited trace and manifest')
    if len(all_trials) != audited['block_count'] * len(POLICIES):
        raise EvidenceError('incomplete policy blocks')

    strata = []
    pairs = []
    for worker in sorted(counts):
        for policy in POLICIES:
            chosen = [(run_id, t) for run_id, t in all_trials
                      if t.perturbed_worker == worker and t.policy == policy]
            if len(chosen) != counts[worker]:
                raise EvidenceError('incomplete worker-policy stratum')
            delta = 0
            for run_id, t in chosen:
                key = f'run{run_id}:block{t.block}'
                reference = blocks[key]
                if reference['perturbed_worker'] != worker:
                    raise EvidenceError('trace disagrees with audited block worker')
                greedy = reference['on_time']['greedy']
                d = int(t.on_time) - greedy
                delta += d
                pairs.append({
                    'run_id': run_id, 'block': t.block,
                    'perturbed_worker': worker, 'policy': policy,
                    'policy_on_time': int(t.on_time), 'greedy_on_time': greedy,
                    'delta_on_time': d, 'selected_worker': t.selected_worker or '',
                    'fresh_reference_worker': t.reference_worker or '',
                })
            successes = sum(t.on_time for _, t in chosen)
            strata.append({
                'perturbed_worker': worker, 'policy': policy,
                'trials': len(chosen), 'on_time': successes,
                'on_time_rate': successes / len(chosen),
                'refusals': sum(t.phase == 'NoEligibleNode' for _, t in chosen),
                'observation_timeouts': sum(t.phase == 'TimedOutObservation' for _, t in chosen),
                'recorded_insufficient_cpu': sum(t.cpu_unschedulable_evidence for _, t in chosen),
                'fresh_infeasible_selections': sum(t.selected_feasible_fresh is False for _, t in chosen),
                'selected_perturbed_worker': sum(t.selected_worker == worker for _, t in chosen),
                'delta_on_time_vs_greedy_count': delta,
                'delta_on_time_vs_greedy_pp': 100 * delta / len(chosen),
            })
    return {
        'study': 'EdgePilot v2 / M6F balanced two-worker descriptive analysis',
        'run_count': audited['run_count'], 'block_count': audited['block_count'],
        'trial_count': audited['trial_count'], 'worker_exposure': counts,
        'runs': audited['runs'], 'strata': strata, 'block_pairs': pairs,
        'limitations': [
            'Worker balance removes only one asymmetry; it does not eliminate sequential-trial confounding.',
            'CPU requests are Kubernetes scheduling reservations, not CPU utilization.',
            'Each trial has a separately created competitor Pod; these are not simultaneous counterfactuals.',
            'Admission refusal and timeouts are distinct from failed or completed Pods.',
            'Insufficient cpu evidence requires a recorded Kubernetes unschedulable reason.',
            'A fresh reference snapshot is non-atomic API request accounting, not ground truth.',
            'Node name ordering and greedy tie-breaking may systematically bias choices.',
            'These differences are descriptive, not inferential p-values or causal effect estimates.',
        ],
    }


def export(report: dict[str, Any], output: Path, inputs: Sequence[Path]) -> None:
    output = Path(output).resolve()
    for path in inputs:
        resolved = Path(path).resolve()
        if output == resolved or output in resolved.parents or resolved in output.parents:
            raise EvidenceError('report directory must be separate from experiment evidence')
    output.mkdir(parents=True, exist_ok=False)  # never overwrite
    (output / 'summary.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n',
                                         encoding='utf-8')
    for file, fields, rows in ((
            'worker_strata.csv', STRATUM_FIELDS, report['strata']),
            ('block_pairs.csv', PAIR_FIELDS, report['block_pairs'])):
        with (output / file).open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', type=Path, action='append', required=True,
                   help='Completed M6D run, repeat for balanced runs')
    p.add_argument('--output', type=Path,
                   default=Path('research_outputs/kubernetes/balanced_stratified_report'))
    a = p.parse_args(argv)
    report = analyze(a.run)
    export(report, a.output, a.run)
    print(f"Audited {report['trial_count']} policy trials / {report['block_count']} "
          f"blocks; worker exposure {report['worker_exposure']}")
    for row in report['strata']:
        print(f"  {row['perturbed_worker']}: {row['policy']:19} "
              f"on-time={row['on_time']}/{row['trials']}, "
              f"delta-vs-greedy={row['delta_on_time_vs_greedy_pp']:+.1f} pp")
    print(f'Wrote {a.output} (offline, descriptive; no causal claims)')


if __name__ == '__main__':
    main()
