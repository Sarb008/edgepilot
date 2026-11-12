"""M6G: offline matched-input replay and deterministic tie-break sensitivity.

Uses already-recorded M6D real-cluster traces. For each trial, all four policy
*initial* decisions are replayed against the same two-sample mixed-age snapshot.
No Kubernetes calls, no counterfactual Pods, no claims about replayed completion.

The replay is an algorithmic diagnostic, not a causal policy comparison; it
cannot reconstruct unknown future telemetry or the delayed policy's final choice.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
import json
import math
from pathlib import Path
from typing import Any, Sequence

from .kube_feasibility_analysis import EvidenceError, _read_json, load_run
from .kube_feasibility_pilot import FeasibilityConfig
from .kube_pilot import POLICIES, policy_for
from .kube_stratified_analysis import analyze
from .kube_validation import samples_from_inventory
from .models import TelemetrySample, Workload

DETAIL_FIELDS = (
    'run_index', 'block', 'position', 'observed_policy', 'perturbed_worker',
    'fresh_reference_worker', 'observed_selected_worker', 'observed_on_time',
    'replayed_policy', 'initial_decision', 'initial_selected_worker',
    'initial_wait_until_seconds_from_arrival', 'initial_selected_fresh_feasible',
    'initial_agrees_with_fresh_reference', 'initial_selected_perturbed_worker',
    'greedy_exact_top_tie_count', 'greedy_lexicographic_first',
    'greedy_reverse_lexicographic', 'greedy_reverse_fresh_feasible',
)


def _numeric(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise EvidenceError(f'{field} must be a finite JSON number')
    return float(value)


def _samples(mixed: Any) -> dict[str, TelemetrySample]:
    if not isinstance(mixed, dict) or not mixed:
        raise EvidenceError('missing mixed policy input')
    result = {}
    for name, info in mixed.items():
        if not isinstance(name, str) or not isinstance(info, dict):
            raise EvidenceError('malformed mixed policy input')
        try:
            sample = TelemetrySample(**info)
        except (TypeError, ValueError) as exc:
            raise EvidenceError(f'invalid sample {name}: {exc}') from exc
        if sample.node_id != name:
            raise EvidenceError('sample key and node_id differ')
        result[name] = sample
    if len(result) != 2:
        raise EvidenceError('expected exactly two eligible workers')
    return result


def _fresh_feasible(worker: str | None, fresh: dict[str, Any], request_m: int) -> bool | None:
    if worker is None:
        return None
    for node in fresh['node_inventory']:
        if node['name'] == worker and node.get('usable_for_model'):
            return float(node['estimated_unreserved_cpu_cores']) * 1000 + 1e-6 >= request_m
    raise EvidenceError('selected worker missing or not usable in fresh snapshot')


def _greedy_tie(workload: Workload, samples: dict[str, TelemetrySample],
                fresh: dict[str, Any], request_m: int) -> dict[str, Any]:
    eligible = [s for s in samples.values() if s.available_cpu + 1e-9 >= workload.cpu_required]
    if not eligible:
        return {'count': 0, 'lex': None, 'reverse': None, 'reverse_feasible': None}
    best = max(s.available_cpu for s in eligible)
    tied = sorted(s.node_id for s in eligible if s.available_cpu == best)
    return {'count': len(tied), 'lex': tied[0], 'reverse': tied[-1],
            'reverse_feasible': _fresh_feasible(tied[-1], fresh, request_m)}


def replay_trace(trace: dict[str, Any], config: FeasibilityConfig) -> list[dict[str, Any]]:
    """Return initial decisions ONLY; the fresh snapshot stays in evaluator code."""
    try:
        row = trace['result']
        fresh = trace['fresh_snapshot']
        samples = _samples(trace['mixed_policy_input'])
        target = row['perturbed_worker']
        if target not in samples:
            raise EvidenceError('perturbed worker absent from mixed input')
        peer = next(name for name in samples if name != target)
        target_age = _numeric(row['target_age_seconds'], 'target_age_seconds')
        peer_age = _numeric(row['peer_age_seconds'], 'peer_age_seconds')
        if min(target_age, peer_age) < 0:
            raise EvidenceError('negative telemetry age')
        # The real run recorded telemetry ages at workload arrival; reconstruct
        # that point using the source's actual monotonic measurement timestamp.
        now = samples[target].measured_at + target_age
        if not math.isclose(samples[peer].age_at(now), peer_age, abs_tol=.025):
            raise EvidenceError('peer age inconsistent with reconstructed arrival')
        if samples[target].received_at > now or samples[peer].received_at > now:
            raise EvidenceError('decision would read undelivered telemetry')
        request_m = row['probe_cpu_m']
        if type(request_m) is not int or not 500 <= request_m <= 12000:
            raise EvidenceError('invalid probe CPU request')
        deadline = _numeric(row['deadline_seconds'], 'deadline_seconds')
        if deadline != float(config.deadline_seconds):
            raise EvidenceError('row deadline differs from manifest')
        workload = Workload('m6g-replay', now, now + deadline,
                            request_m / 1000., float(config.sleep_seconds))
        ref_samples = samples_from_inventory(fresh['node_inventory'],
                    fresh['measurement_window_start_monotonic'], fresh['receipt_monotonic'])
        if not ref_samples or any(s.received_at > now for s in ref_samples.values()):
            raise EvidenceError('fresh reference not delivered by recorded arrival')
        fresh_decision = policy_for('greedy', config.stale_config().policy_config()).decide(
            workload, now, ref_samples, 0.0)
        fresh_reference = fresh_decision.node_id if fresh_decision else None
        if fresh_reference != row['reference_worker']:
            raise EvidenceError('fresh-reference replay disagrees with recorded reference')
        tie = _greedy_tie(workload, samples, fresh, request_m)
        out = []
        for name in POLICIES:
            decision = policy_for(name, config.stale_config().policy_config()).decide(
                workload, now, samples, 0.0)
            kind = ('reject' if decision is None else
                    'wait' if decision.wait_until is not None else 'select')
            node = decision.node_id if kind == 'select' else None
            wait_until = (decision.wait_until - workload.arrival_time if kind == 'wait' else None)
            if kind == 'wait' and (wait_until <= 0 or wait_until > config.stale_config().max_wait_seconds + 1e-6):
                raise EvidenceError('invalid bounded wait in replay')
            out.append({
                'observed_policy': row['policy'],
                'perturbed_worker': target,
                'fresh_reference_worker': fresh_reference,
                'observed_selected_worker': row['selected_worker'],
                'observed_on_time': row['on_time_observed'],
                'replayed_policy': name, 'initial_decision': kind,
                'initial_selected_worker': node,
                'initial_wait_until_seconds_from_arrival': wait_until,
                'initial_selected_fresh_feasible': _fresh_feasible(node, fresh, request_m),
                'initial_agrees_with_fresh_reference': (node == fresh_reference if node else None),
                'initial_selected_perturbed_worker': (node == target if node else None),
                'greedy_exact_top_tie_count': tie['count'],
                'greedy_lexicographic_first': tie['lex'],
                'greedy_reverse_lexicographic': tie['reverse'],
                'greedy_reverse_fresh_feasible': tie['reverse_feasible'],
            })
        # Actual immediate policies must agree with their deterministic
        # initial replay. Bounded deferral may refresh; initial != final is valid.
        if row['policy'] != 'bounded_deferral':
            own = next(record for record in out if record['replayed_policy'] == row['policy'])
            if own['initial_selected_worker'] != row['selected_worker']:
                raise EvidenceError('logged immediate decision differs from recorded-input replay')
        if tie['count'] and out[0]['initial_selected_worker'] != tie['lex']:
            raise EvidenceError('greedy replay contradicts exact tie ordering')
        return out
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError(f'malformed trace cannot be replayed: {exc}') from exc


def analyze_replays(directories: Sequence[Path]) -> dict[str, Any]:
    """One input per historical trial, four *nonindependent* replay decisions."""
    directories = [Path(x) for x in directories]
    if not directories or len({str(p.resolve()) for p in directories}) != len(directories):
        raise EvidenceError('must provide distinct completed runs')
    # Reuse M6F's strict run audit, balanced exposure and configuration check.
    audited = analyze(directories)
    detail: list[dict[str, Any]] = []
    for index, directory in enumerate(directories, start=1):
        trials, manifest = load_run(directory)
        try:
            cfg = FeasibilityConfig(**manifest['manifest']['config'])
        except (TypeError, ValueError) as exc:
            raise EvidenceError(f'unsupported manifest configuration: {exc}') from exc
        for trial in trials:
            trace_path = directory / 'traces' / (
                f'block{trial.block:02d}_position{trial.position:02d}_{trial.policy}.json')
            trace = _read_json(trace_path)
            for replay in replay_trace(trace, cfg):
                detail.append({'run_index': index, 'block': trial.block,
                               'position': trial.position, **replay})
    if len(detail) != audited['trial_count'] * len(POLICIES):
        raise EvidenceError('replay output incomplete')
    initial = [d for d in detail if d['replayed_policy'] == 'greedy']
    tie = [d for d in initial if d['greedy_exact_top_tie_count'] > 1]
    by_policy = []
    for policy in POLICIES:
        rows = [d for d in detail if d['replayed_policy'] == policy]
        by_policy.append({
            'policy': policy, 'recorded_input_count': len(rows),
            'initial_selects': sum(d['initial_decision'] == 'select' for d in rows),
            'initial_waits': sum(d['initial_decision'] == 'wait' for d in rows),
            'initial_refusals': sum(d['initial_decision'] == 'reject' for d in rows),
            'initial_fresh_infeasible_selections': sum(d['initial_selected_fresh_feasible'] is False for d in rows),
            'initial_fresh_reference_agreements': sum(d['initial_agrees_with_fresh_reference'] is True for d in rows),
        })
    return {
        'study': 'EdgePilot v2 M6G / retrospective common-input decision audit',
        'historical_trial_count': audited['trial_count'],
        'replay_decision_count': len(detail),
        'worker_exposure': audited['worker_exposure'],
        'greedy_exact_top_tie_inputs': len(tie),
        'greedy_tie_flip_changes_worker': sum(d['greedy_lexicographic_first'] != d['greedy_reverse_lexicographic'] for d in tie),
        'greedy_tie_flip_changes_fresh_feasibility': sum(
            d['initial_selected_fresh_feasible'] != d['greedy_reverse_fresh_feasible'] for d in tie),
        'policy_initial_summary': by_policy,
        'details': detail,
        'evidence': audited['runs'],
        'limitations': [
            'Four replays of one trace are NOT four independent cluster trials.',
            'All replay decisions see an identical previously recorded input, never future information.',
            'Fresh-reference feasibility is for audit only and is not passed to scheduling policies.',
            'Bounded deferral replay stops at its initial WAIT decision: future snapshots are not invented.',
            'Reverse lexicographic tie-break is a decision sensitivity calculation; no Pod was run.',
            'CPU feasibility uses Kubernetes requests, not CPU utilization.',
            'Recorded inputs come from sequential trial environments and are not a causal field trial.',
        ],
    }


def export_replays(report: dict[str, Any], output: Path, directories: Sequence[Path]) -> None:
    output = Path(output).resolve()
    for run in directories:
        base = Path(run).resolve()
        if output == base or output in base.parents or base in output.parents:
            raise EvidenceError('output must be separate from source evidence')
    output.mkdir(parents=True, exist_ok=False)
    summary = {k: v for k, v in report.items() if k != 'details'}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    with (output / 'replayed_decisions.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=DETAIL_FIELDS)
        writer.writeheader()
        writer.writerows(report['details'])


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', action='append', required=True, type=Path,
                   help='Complete M6D runs, including the balanced M6F run_01 folder')
    p.add_argument('--output', type=Path, default=Path('research_outputs/kubernetes/decision_audit'))
    args = p.parse_args(argv)
    result = analyze_replays(args.run)
    export_replays(result, args.output, args.run)
    print(f"Audited {result['historical_trial_count']} historical trials; "
          f"replayed {result['replay_decision_count']} initial decisions on the same inputs.")
    print(f"Exact top-capacity ties for greedy: {result['greedy_exact_top_tie_inputs']}; "
          f"reversing lexical tie-break alters fresh feasibility in "
          f"{result['greedy_tie_flip_changes_fresh_feasibility']} inputs")
    for row in result['policy_initial_summary']:
        print(f"  {row['policy']:19} select={row['initial_selects']:<3} "
              f"wait={row['initial_waits']:<3} reject={row['initial_refusals']:<3} "
              f"fresh-infeasible={row['initial_fresh_infeasible_selections']}")
    print(f'Wrote {args.output} (offline ONLY; no Kubernetes calls or counterfactual outcomes).')


if __name__ == '__main__':
    main()
