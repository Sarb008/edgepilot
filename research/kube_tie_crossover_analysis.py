"""M6I offline integrity audit and worker-stratified summary of prospective runs.

Reads only completed M6I evidence. It never contacts a cluster, produces
counterfactual Pod outcomes, or pretends sequential arms share one measurement.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from .kube_feasibility_pilot import request_from_old_capacity, verify_boundary
from .kube_pilot import policy_for
from .kube_tie_crossover import (
    ARMS, CrossoverConfig, FIELDS, greedy_tie_candidates, greedy_tie_select,
    plan_for,
)
from .kube_stale_pilot import inventory_by_name, usable_workers
from .models import TelemetrySample, Workload
from .kube_validation import KubectlError, samples_from_inventory


class CrossoverEvidenceError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise CrossoverEvidenceError(f'cannot read JSON evidence: {path}') from exc
    if not isinstance(data, dict):
        raise CrossoverEvidenceError(f'JSON evidence must be an object: {path}')
    return data


def _csv_value(value: Any) -> str:
    return '' if value is None else str(value)


def analyze(runs: Sequence[Path]) -> dict[str, Any]:
    if not runs:
        raise CrossoverEvidenceError('at least one run required')
    all_rows = []
    evidence = []
    for run_index, root in enumerate(runs, 1):
        root = Path(root).resolve()
        if any(root == rec['path'] for rec in evidence):
            raise CrossoverEvidenceError('same run passed more than once')
        manifest = _read_json(root / 'manifest.json')
        if manifest.get('study') != 'EdgePilot v2 M6I / prospective worker-balanced tie-break crossover':
            raise CrossoverEvidenceError('incorrect study type')
        try:
            cfg = CrossoverConfig(**manifest['config'])
            expected_plan = [
                {'repeat': rep, 'block': block, 'position': pos, 'arm': arm,
                 'perturbed_worker_slot': (block - 1) % 2}
                for rep, block, pos, arm in plan_for(cfg)
            ]
        except (TypeError, ValueError, KeyError) as exc:
            raise CrossoverEvidenceError('invalid config in evidence manifest') from exc
        if manifest.get('plan') != expected_plan:
            raise CrossoverEvidenceError('manifest plan does not match seeded protocol')
        try:
            with (root / 'trials.csv').open(newline='', encoding='utf-8') as stream:
                reader = csv.DictReader(stream)
                if tuple(reader.fieldnames or ()) != FIELDS:
                    raise CrossoverEvidenceError('trial CSV columns differ from experiment schema')
                rows = list(reader)
        except OSError as exc:
            raise CrossoverEvidenceError('cannot read trial CSV') from exc
        if len(rows) != len(expected_plan):
            raise CrossoverEvidenceError('incomplete or extra trials; do not pool partial runs')
        checksums: dict[str, str] = {}
        for index, ((rep, block, position, arm), row) in enumerate(zip(plan_for(cfg), rows)):
            if [row['repeat'], row['block'], row['position'], row['arm']] != [
                    str(rep), str(block), str(position), arm]:
                raise CrossoverEvidenceError('trials.csv order does not match manifest plan')
            trace_rel = Path('traces') / f'block{block:02d}_position{position:02d}_{arm}.json'
            trace_file = root / trace_rel
            trace = _read_json(trace_file)
            if [trace.get('repeat'), trace.get('block'), trace.get('position'), trace.get('arm')] != [
                    rep, block, position, arm]:
                raise CrossoverEvidenceError('trace metadata does not match trial plan')
            result = trace.get('result')
            if not isinstance(result, dict):
                raise CrossoverEvidenceError('trace lacks final result')
            for field in FIELDS:
                if row[field] != _csv_value(result.get(field)):
                    raise CrossoverEvidenceError(f'CSV/trace mismatch: {field} in {trace_rel}')
            if result.get('terminal_phase') in ('NotStarted', 'ProbeError') or result.get('error'):
                raise CrossoverEvidenceError('incomplete/errored trial cannot be pooled')
            if not result.get('competitor_cleanup_confirmed'):
                raise CrossoverEvidenceError('reservation cleanup not confirmed')
            if result.get('pod_name') and not result.get('probe_cleanup_confirmed'):
                raise CrossoverEvidenceError('workload cleanup not confirmed')
            if result.get('greedy_exact_top_tie') is not True:
                raise CrossoverEvidenceError('not an exact two-way greedy top tie')
            try:
                old = trace['old_snapshot']
                fresh = trace['fresh_snapshot']
                workers = usable_workers(old)
                target = workers[(block - 1) % 2]['name']
                peer = next(n['name'] for n in workers if n['name'] != target)
                cpu_m = request_from_old_capacity(inventory_by_name(old)[target],
                                                  inventory_by_name(old)[peer],
                                                  cfg.feasibility_config())
                boundary = verify_boundary(old, fresh, target, peer, cpu_m,
                                           cfg.feasibility_config())
                samples = {node: TelemetrySample(**s)
                           for node, s in trace['mixed_policy_input'].items()}
                now = samples[target].measured_at + float(result['target_age_seconds'])
                if abs(samples[peer].age_at(now) - float(result['peer_age_seconds'])) > 1e-4:
                    raise CrossoverEvidenceError('peer age inconsistent with mixed telemetry')
                workload = Workload('audit', now, now + cfg.deadline_seconds,
                                    cpu_m / 1000., float(cfg.sleep_seconds))
                candidates = greedy_tie_candidates(workload, samples)
                if len(candidates) != 2 or ';'.join(candidates) != result['greedy_tie_candidates']:
                    raise CrossoverEvidenceError('reported tie does not match policy input')
                if (result['perturbed_worker'] != target or result['probe_cpu_m'] != cpu_m
                        or any(result.get(key) != value for key, value in boundary.items())):
                    raise CrossoverEvidenceError('fresh/old resource boundary does not match result')
                if arm.startswith('greedy_'):
                    actual_greedy = greedy_tie_select(workload, samples, arm)
                    if (result['initial_action'] != 'select' or result['initial_selected_worker'] != actual_greedy
                            or result['selected_worker'] != actual_greedy or result['refreshes'] != 0
                            or float(result['wait_seconds']) != 0):
                        raise CrossoverEvidenceError('observed greedy decision differs from announced tie rule')
                if arm in ('bounded_deferral', 'age_penalized'):
                    actual = policy_for(arm, cfg.feasibility_config().stale_config().policy_config()).decide(
                        workload, now, samples, 0.0)
                    action = ('reject' if actual is None else
                              ('wait' if actual.node_id is None else 'select'))
                    node_id = actual.node_id if actual else None
                    if (result['initial_action'] != action
                            or result.get('initial_selected_worker') != node_id):
                        raise CrossoverEvidenceError('recorded initial policy action disagrees with the input')
                fresh_samples = samples_from_inventory(
                    fresh['node_inventory'], fresh['measurement_window_start_monotonic'],
                    fresh['receipt_monotonic'])
                ref = policy_for('greedy', cfg.feasibility_config().stale_config().policy_config()).decide(
                    workload, now, fresh_samples, 0.0)
                if result.get('reference_worker') != (ref.node_id if ref else None):
                    raise CrossoverEvidenceError('fresh reference does not match treatment snapshot')
                selected = result.get('selected_worker')
                if selected:
                    node = inventory_by_name(fresh)[selected]
                    feasible = node['estimated_unreserved_cpu_cores'] * 1000 >= cpu_m - 1e-6
                    if result.get('selected_feasible_in_treatment_snapshot') is not feasible:
                        raise CrossoverEvidenceError('recorded retrospective feasibility is inconsistent')
                elif result.get('selected_feasible_in_treatment_snapshot') is not None:
                    raise CrossoverEvidenceError('non-selection cannot claim placement feasibility')
            except (KeyError, TypeError, ValueError, KubectlError) as exc:
                raise CrossoverEvidenceError(f'invalid source evidence in {trace_rel}: {exc}') from exc
            all_rows.append(dict(result, run_index=run_index, source=str(root)))
            checksums[trace_rel.as_posix()] = hashlib.sha256(trace_file.read_bytes()).hexdigest()
        for filename in ('manifest.json', 'trials.csv'):
            checksums[filename] = hashlib.sha256((root / filename).read_bytes()).hexdigest()
        evidence.append({'path': root, 'sha256': checksums})

    # Pool by actual named perturbed worker, never count four policy decisions as independent.
    worker_names = sorted({r['perturbed_worker'] for r in all_rows})
    strata = []
    for worker in worker_names:
        for arm in ARMS:
            subset = [r for r in all_rows if r['perturbed_worker'] == worker and r['arm'] == arm]
            strata.append({
                'perturbed_worker': worker, 'arm': arm, 'trials': len(subset),
                'on_time': sum(r.get('on_time_observed') is True for r in subset),
                'treatment_infeasible': sum(
                    r.get('selected_feasible_in_treatment_snapshot') is False for r in subset),
                'observation_timeouts': sum(r['terminal_phase'] == 'TimedOutObservation' for r in subset),
                'initial_waits': sum(r['initial_action'] == 'wait' for r in subset),
                'total_wait_seconds': round(sum(float(r.get('wait_seconds') or 0) for r in subset), 6),
            })
    return {
        'study': 'EdgePilot v2 M6I / offline tie crossover evidence audit',
        'trial_count': len(all_rows), 'completed_runs': len(runs),
        'worker_strata': strata,
        'evidence': [{'path': str(e['path']), 'sha256': e['sha256']} for e in evidence],
        'limitations': [
            'This audit is offline: no new Kubernetes jobs, no counterfactual observed outcomes',
            'Each experiment executes sequential Pods with fresh treatment environments',
            'Observed timeouts are not proof of permanent admission failure',
            'No confidence intervals or causal/generalization claims for few blocks',
            'Treatment feasibility is retrospective CPU-request accounting, not CPU utilization',
        ],
    }


def export(report: dict[str, Any], output: Path, runs: Sequence[Path]) -> None:
    destination = Path(output).resolve()
    for run in runs:
        src = Path(run).resolve()
        if destination == src or src in destination.parents or destination in src.parents:
            raise CrossoverEvidenceError('output must be separate from evidence directories')
    if destination.exists():
        raise FileExistsError(f'refuse overwrite output path: {destination}')
    destination.mkdir(parents=True)
    body = {key: val for key, val in report.items() if key != 'worker_strata'}
    (destination / 'summary.json').write_text(json.dumps(body, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    with (destination / 'worker_strata.csv').open('w', newline='', encoding='utf-8') as stream:
        fields = ('perturbed_worker', 'arm', 'trials', 'on_time', 'treatment_infeasible',
                  'observation_timeouts', 'initial_waits', 'total_wait_seconds')
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(report['worker_strata'])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='append', type=Path, required=True)
    parser.add_argument('--output', type=Path,
                        default=Path('research_outputs/kubernetes/tie_crossover_audit_01'))
    args = parser.parse_args(argv)
    report = analyze(args.run)
    export(report, args.output, args.run)
    print(f'Audited {report["trial_count"]} completed real-cluster trial records from '
          f'{report["completed_runs"]} run(s). No Kubernetes calls.')
    for row in report['worker_strata']:
        print(f"  {row['perturbed_worker']:22} {row['arm']:19} "
              f"on-time={row['on_time']}/{row['trials']}; "
              f"treatment-infeasible={row['treatment_infeasible']}; "
              f"initial-wait={row['initial_waits']}")
    print(f'Wrote {args.output}; observational only, no causal inference.')


if __name__ == '__main__':
    main()
