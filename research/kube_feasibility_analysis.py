"""Offline, read-only audit of EdgePilot v2 Kubernetes feasibility pilot evidence.

Never calls kubectl. Verifies that CSV rows match the originally written per-trial
traces and recorded plan, checks cleanup, and describes policy outcomes. A
successful bounded-deferral decision is NOT by itself a causal treatment effect:
trials are sequential, and the "fresh" inventory is an API-based reference.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, replace
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics
from typing import Any, Sequence

from .kube_pilot import POLICIES

FIELDS = ('policy', 'trials', 'on_time', 'on_time_rate', 'succeeded',
          'no_eligible_node', 'timed_out_observation', 'confirmed_insufficient_cpu',
          'observed_timeout_without_cpu_evidence', 'other_outcomes',
          'fresh_infeasible_selections', 'fresh_reference_disagreements',
          'median_success_seconds')


class EvidenceError(ValueError):
    """Input cannot be trusted enough to produce an audited summary."""


@dataclass(frozen=True)
class Trial:
    run: str
    run_id: int
    block: int
    position: int
    policy: str
    phase: str
    on_time: bool
    selected_worker: str | None
    reference_worker: str | None
    selected_feasible_fresh: bool | None
    reference_disagreement: bool
    cpu_unschedulable_evidence: bool
    unschedulable_reason: str | None
    elapsed_seconds: float | None
    probe_cpu_m: int
    perturbed_worker: str


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f'cannot read valid JSON {path}: {exc}') from exc
    if not isinstance(data, dict):
        raise EvidenceError(f'JSON root must be an object: {path}')
    return data


def _required_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise EvidenceError(f'{name}: expected integer')
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvidenceError(f'{name}: expected integer') from exc
    if str(result) != str(value) or result < minimum:
        raise EvidenceError(f'{name}: invalid integer {value!r}')
    return result


def _bool(value: Any, name: str, *, optional: bool = False) -> bool | None:
    if type(value) is bool:
        return value
    if type(value) is str and value in ('True', 'False'):
        return value == 'True'
    if optional and (value == '' or value is None):
        return None
    raise EvidenceError(f'{name}: invalid boolean {value!r}')


def _nonnegative_float(value: Any, name: str, *, optional: bool = False) -> float | None:
    if optional and (value is None or value == ''):
        return None
    try:
        val = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise EvidenceError(f'{name}: expected finite number') from exc
    if not math.isfinite(val) or val < 0:
        raise EvidenceError(f'{name}: expected finite nonnegative number')
    return val


def _same_repr(csv_value: Any, json_value: Any) -> bool:
    return str(csv_value if csv_value is not None else '') == str(
        json_value if json_value is not None else '')


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _manifest_and_rows(directory: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    manifest = _read_json(directory / 'manifest.json')
    config = manifest.get('config')
    if not isinstance(config, dict) or config.get('context') != 'kind-edgepilot-v2' \
            or config.get('namespace') != 'edgepilot-v2-validation':
        raise EvidenceError('unsupported cluster or namespace in manifest')
    blocks = _required_int(config.get('blocks'), 'manifest.blocks', minimum=1)
    if blocks > 2:
        raise EvidenceError('unexpected number of blocks in M6D manifest')
    plan = manifest.get('plan')
    if not isinstance(plan, list) or len(plan) != blocks * len(POLICIES):
        raise EvidenceError('manifest plan incomplete')
    for block in range(1, blocks + 1):
        per_block = [v for v in plan if isinstance(v, dict) and v.get('block') == block]
        if {v.get('policy') for v in per_block} != set(POLICIES) or \
                {v.get('position') for v in per_block} != set(range(1, len(POLICIES) + 1)) or \
                len(per_block) != len(POLICIES):
            raise EvidenceError('manifest has duplicate or missing policy/position')
    path = directory / 'trials.csv'
    try:
        with path.open(newline='', encoding='utf-8') as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise EvidenceError('trials.csv has invalid header')
            required = {'block', 'position', 'policy', 'terminal_phase', 'on_time_observed',
                        'competitor_cleanup_confirmed', 'probe_cleanup_confirmed',
                        'selected_worker', 'reference_worker', 'probe_cpu_m',
                        'stale_target_feasible_in_old_snapshot',
                        'stale_target_feasible_in_fresh_snapshot', 'fresh_peer_feasible',
                        'selected_differs_from_reference',
                        'selected_feasible_in_fresh_snapshot', 'perturbed_worker'}
            if not required.issubset(reader.fieldnames):
                raise EvidenceError('missing required CSV columns')
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise EvidenceError(f'cannot read trials.csv: {exc}') from exc
    if len(rows) != len(plan):
        raise EvidenceError('CSV count does not match plan; possible interrupted experiment')
    for idx, (row, expected) in enumerate(zip(rows, plan, strict=True)):
        if row.get('policy') != expected['policy'] or \
                _required_int(row.get('block'), 'row.block', minimum=1) != expected['block'] or \
                _required_int(row.get('position'), 'row.position', minimum=1) != expected['position']:
            raise EvidenceError(f'row {idx+1} violates recorded trial order')
    return manifest, rows


def _audit_snapshot_boundary(trace: dict[str, Any], target: str, reference: str,
                             selected: str | None, request_m: int,
                             selected_feasible: bool | None) -> None:
    """Recompute feasibility flags independently from the timestamped traces.

    Inventory is still a non-atomic Kubernetes API approximation, NOT truth.
    """
    old = trace.get('old_snapshot')
    fresh = trace.get('fresh_snapshot')
    mixed = trace.get('mixed_policy_input')
    if not isinstance(old, dict) or not isinstance(fresh, dict) or not isinstance(mixed, dict):
        raise EvidenceError('missing old/fresh snapshots or policy input')
    for name, snap in [('old', old), ('fresh', fresh)]:
        begin = _nonnegative_float(snap.get('measurement_window_start_monotonic'),
                                   f'{name}.measurement')
        received = _nonnegative_float(snap.get('receipt_monotonic'), f'{name}.receipt')
        if begin > received:
            raise EvidenceError('snapshot receipt precedes measurement')
    if old['receipt_monotonic'] > fresh['measurement_window_start_monotonic']:
        raise EvidenceError('snapshot causal order violated')
    def inventory(snapshot: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
        values = snapshot.get('node_inventory')
        if not isinstance(values, list) or not values:
            raise EvidenceError(f'{label} snapshot has no inventory')
        found: dict[str, dict[str, Any]] = {}
        for node in values:
            if not isinstance(node, dict) or not isinstance(node.get('name'), str) or node['name'] in found:
                raise EvidenceError(f'{label} snapshot has invalid/duplicate nodes')
            found[node['name']] = node
        return found
    previous = inventory(old, 'old')
    current = inventory(fresh, 'fresh')
    if target not in previous or target not in current or reference not in current:
        raise EvidenceError('missing target or reference node in snapshots')
    if len(mixed) != sum(bool(n.get('usable_for_model')) for n in current.values()):
        raise EvidenceError('mixed input has unexpected eligible nodes')
    if target not in mixed or reference not in mixed:
        raise EvidenceError('mixed snapshot does not contain target and reference')
    if not math.isclose(float(mixed[target].get('measured_at', -1)),
                        float(old['measurement_window_start_monotonic']), abs_tol=1e-6):
        raise EvidenceError('withheld worker received a fabricated timestamp')
    if target != reference and not math.isclose(float(mixed[reference].get('measured_at', -1)),
                                                float(fresh['measurement_window_start_monotonic']), abs_tol=1e-6):
        raise EvidenceError('peer did not use the recorded fresh measurement')
    def cpu_remaining(node: dict[str, Any]) -> float:
        return _nonnegative_float(node.get('estimated_unreserved_cpu_cores'), 'available CPU') * 1000
    if cpu_remaining(previous[target]) < request_m - 1e-6:
        raise EvidenceError('old snapshot does not support stale-feasible claim')
    if cpu_remaining(current[target]) >= request_m - 1e-6:
        raise EvidenceError('fresh target is still feasible; no boundary')
    if cpu_remaining(current[reference]) < request_m - 1e-6:
        raise EvidenceError('fresh reference worker is not feasible')
    if selected:
        if selected not in current:
            raise EvidenceError('selected worker missing in fresh inventory')
        actually_feasible = cpu_remaining(current[selected]) >= request_m - 1e-6
        if actually_feasible != selected_feasible:
            raise EvidenceError('logged feasibility flag disagrees with recorded snapshot')


def load_run(directory: Path) -> tuple[list[Trial], dict[str, Any]]:
    """Strict complete-run audit. Does NOT modify inputs, outputs or cluster."""
    directory = Path(directory)
    if not directory.is_dir():
        raise EvidenceError(f'run directory does not exist: {directory}')
    manifest, rows = _manifest_and_rows(directory)
    trials: list[Trial] = []
    hashes = {'manifest.json': _hash(directory / 'manifest.json'),
              'trials.csv': _hash(directory / 'trials.csv')}
    for row in rows:
        block = _required_int(row['block'], 'block', minimum=1)
        position = _required_int(row['position'], 'position', minimum=1)
        policy = row['policy']
        rel = Path('traces') / f'block{block:02d}_position{position:02d}_{policy}.json'
        trace = _read_json(directory / rel)
        original = trace.get('result')
        if not isinstance(original, dict):
            raise EvidenceError(f'trace missing result: {rel}')
        if trace.get('block') != block or trace.get('position') != position or trace.get('policy') != policy:
            raise EvidenceError(f'trace identity mismatch: {rel}')
        required_result = {'block', 'position', 'policy', 'terminal_phase',
                           'on_time_observed', 'competitor_cleanup_confirmed',
                           'selected_differs_from_reference', 'probe_cpu_m',
                           'stale_target_feasible_in_old_snapshot',
                           'stale_target_feasible_in_fresh_snapshot', 'fresh_peer_feasible'}
        if not required_result.issubset(original):
            raise EvidenceError(f'trace result missing required fields: {rel}')
        for field in row:
            if field in original and not _same_repr(row[field], original[field]):
                raise EvidenceError(f'CSV/trace mismatch for {field!r} in {rel}')
        if original.get('error') or original.get('probe_cleanup_error') or original.get('competitor_cleanup_error'):
            raise EvidenceError(f'run contains execution or cleanup error: {rel}')
        if _bool(row['competitor_cleanup_confirmed'], 'competitor_cleanup_confirmed') is not True:
            raise EvidenceError(f'competitor cleanup not confirmed: {rel}')
        if row.get('pod_name') and _bool(row['probe_cleanup_confirmed'], 'probe_cleanup_confirmed') is not True:
            raise EvidenceError(f'probe cleanup not confirmed: {rel}')
        if _bool(row['stale_target_feasible_in_old_snapshot'], 'old boundary') is not True or \
                _bool(row['stale_target_feasible_in_fresh_snapshot'], 'fresh target') is not False or \
                _bool(row['fresh_peer_feasible'], 'fresh peer') is not True:
            raise EvidenceError(f'feasibility intervention not verified: {rel}')
        cpu_m = _required_int(row.get('probe_cpu_m'), 'probe_cpu_m', minimum=500)
        if cpu_m > 12000:
            raise EvidenceError('CPU request exceeds safety bound')
        phase = row['terminal_phase']
        if phase not in {'Succeeded', 'Failed', 'TimedOutObservation',
                         'NoEligibleNode', 'DeadlineBeforeSubmit', 'ProbeError'}:
            raise EvidenceError(f'unknown terminal phase: {phase}')
        on_time = _bool(row['on_time_observed'], 'on_time_observed')
        selected = row['selected_worker'] or None
        reference = row['reference_worker'] or None
        if not reference:
            raise EvidenceError(f'no fresh-reference selection: {rel}')
        if _bool(row['selected_differs_from_reference'], 'reference disagreement') != (selected != reference):
            raise EvidenceError(f'inconsistent reference comparison: {rel}')
        fresh_feasible = _bool(row['selected_feasible_in_fresh_snapshot'],
                               'selected_feasible_in_fresh_snapshot', optional=True)
        if (selected is None) != (fresh_feasible is None):
            raise EvidenceError(f'feasibility flag inconsistent with node selection: {rel}')
        if on_time and (phase != 'Succeeded' or not selected or not fresh_feasible or
                        row.get('container_exit_code') != '0'):
            raise EvidenceError(f'on-time success inconsistent with trace: {rel}')
        _audit_snapshot_boundary(trace, row['perturbed_worker'], reference, selected,
                                 cpu_m, fresh_feasible)
        if not selected and phase not in ('NoEligibleNode', 'DeadlineBeforeSubmit', 'ProbeError'):
            raise EvidenceError(f'no worker selected but attempted terminal phase: {rel}')
        if phase == 'Succeeded' and not selected:
            raise EvidenceError(f'completed workload without selected node: {rel}')
        duration = _nonnegative_float(row.get('end_to_end_seconds'),
                                      'end_to_end_seconds', optional=True)
        deadline = _nonnegative_float(row.get('deadline_seconds'), 'deadline_seconds')
        if deadline <= 0 or (on_time and (duration is None or duration > deadline + 1e-6)):
            raise EvidenceError(f'observed success violates recorded deadline: {rel}')
        reason = row.get('unschedulable_reason') or None
        if reason and not selected:
            raise EvidenceError(f'unschedulable condition without created probe: {rel}')
        hashes[rel.as_posix()] = _hash(directory / rel)
        trials.append(Trial(run=directory.name, run_id=0, block=block, position=position,
                            policy=policy, phase=phase, on_time=bool(on_time),
                            selected_worker=selected, reference_worker=reference,
                            selected_feasible_fresh=fresh_feasible,
                            reference_disagreement=selected != reference,
                            cpu_unschedulable_evidence=bool(reason and 'insufficient cpu' in reason.lower()),
                            unschedulable_reason=reason, elapsed_seconds=duration,
                            probe_cpu_m=cpu_m, perturbed_worker=row['perturbed_worker']))
    return trials, {'path': str(directory.resolve()),
                    'manifest': {'config': manifest['config'], 'plan': manifest['plan']},
                    'sha256': hashes}


def audit_runs(directories: Sequence[Path]) -> dict[str, Any]:
    if not directories:
        raise EvidenceError('at least one --run is required')
    canonical = [str(Path(p).resolve()) for p in directories]
    if len(set(canonical)) != len(canonical):
        raise EvidenceError('duplicate run path')
    all_trials: list[Trial] = []
    run_audits: list[dict[str, Any]] = []
    for run_id, directory in enumerate(directories, 1):
        trials, audit = load_run(Path(directory))
        all_trials.extend(replace(t, run_id=run_id) for t in trials)
        run_audits.append(audit)
    summary = []
    for policy in POLICIES:
        selected = [t for t in all_trials if t.policy == policy]
        success_durations = [t.elapsed_seconds for t in selected
                             if t.on_time and t.elapsed_seconds is not None]
        timeouts = [t for t in selected if t.phase == 'TimedOutObservation']
        successes = sum(t.phase == 'Succeeded' for t in selected)
        refusal = sum(t.phase == 'NoEligibleNode' for t in selected)
        summary.append(dict(zip(FIELDS, (
            policy, len(selected), sum(t.on_time for t in selected),
            sum(t.on_time for t in selected) / len(selected) if selected else None,
            successes, refusal, len(timeouts),
            sum(t.cpu_unschedulable_evidence for t in selected),
            sum(not t.cpu_unschedulable_evidence for t in timeouts),
            len(selected) - successes - refusal - len(timeouts),
            sum(t.selected_feasible_fresh is False for t in selected),
            sum(t.reference_disagreement for t in selected),
            statistics.median(success_durations) if success_durations else None,
        ))))
    by_block = {}
    for run_id, run in enumerate(directories, start=1):
        per_run = [t for t in all_trials if t.run_id == run_id]
        # Avoid collisions when two paths share the same basename: index of path
        # preserves the run's block identity.
        bnum = _required_int(run_audits[run_id-1]['manifest']['config']['blocks'], 'blocks', minimum=1)
        for block in range(1, bnum + 1):
            per_block = [t for t in per_run if t.block == block]
            if len(per_block) != len(POLICIES):
                raise EvidenceError('incomplete within-block outcomes')
            by_policy = {t.policy: t for t in per_block}
            by_block[f'run{run_id}:block{block}'] = {
                'run_id': run_id, 'block': block,
                'perturbed_worker': per_block[0].perturbed_worker,
                'on_time': {p: int(by_policy[p].on_time) for p in POLICIES},
                'delta_on_time_vs_greedy': {
                    p: int(by_policy[p].on_time) - int(by_policy['greedy'].on_time)
                    for p in POLICIES if p != 'greedy'
                },
            }
    configs = [r['manifest']['config'] for r in run_audits]
    comparable_keys = ('deadline_seconds', 'competitor_cpu_m', 'margin_cpu_m',
                       'competitor_hold_seconds', 'sleep_seconds')
    matching = all(all(c.get(k) == configs[0].get(k) for k in comparable_keys)
                   for c in configs)
    count_targets = {name: sum(1 for b in by_block.values() if b['perturbed_worker'] == name)
                     for name in sorted({b['perturbed_worker'] for b in by_block.values()})}
    return {'study': 'EdgePilot v2 / M6E offline evidence audit',
            'run_count': len(directories), 'block_count': len(by_block),
            'trial_count': len(all_trials), 'policy_summaries': summary,
            'by_block': by_block, 'perturbation_target_blocks': count_targets,
            'comparable_configurations': matching, 'runs': run_audits,
            'limits': [
                'This is a read-only retrospective audit, not a cluster experiment.',
                'Trials are sequential; block comparisons are not independent controlled counterfactuals.',
                'Fresh-reference capacity is non-atomic API request accounting, not perfect node ground truth.',
                'TimedOutObservation does not prove Insufficient cpu without a recorded unschedulable reason.',
                'NoEligibleNode represents deliberate admission refusal, not a Kubernetes Pod failure.',
                'Pod requests near capacity do not imply high actual CPU utilization.',
                'Do not infer causal effects or compute an inferential confidence interval from these few trials.',
                'On-time observed successes use client polling; terminal timing is interval-censored.',
                'Runs with differing workload/deadline configurations must be analyzed separately.',
            ]}


def write_audit(report: dict[str, Any], destination: Path, runs: Sequence[Path]) -> None:
    """Write two report artifacts only after full validation; never overwrite."""
    destination = Path(destination)
    out = destination.resolve()
    for path in runs:
        inp = Path(path).resolve()
        if out == inp or inp in out.parents or out in inp.parents:
            raise EvidenceError('report output must be separate from input evidence')
    if destination.exists():
        raise FileExistsError(f'refuse to overwrite existing output {destination}')
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'audit.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    with (destination / 'policy_summary.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(report['policy_summaries'])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path, action='append',
                        help='M6D experiment output directory; repeat to combine runs')
    parser.add_argument('--output', type=Path,
                        default=Path('research_outputs/kubernetes/feasibility_audit'))
    args = parser.parse_args(argv)
    result = audit_runs(args.run)
    write_audit(result, args.output, args.run)
    print(f'Audited {result["trial_count"]} real-cluster trial records '
          f'from {result["run_count"]} complete runs / {result["block_count"]} blocks.')
    print('Policy               on-time   rejected  timeouts  fresh-infeasible  recorded Insufficient cpu')
    for r in result['policy_summaries']:
        print(f'{r["policy"]:20} {r["on_time"]}/{r["trials"]:<6} '
              f'{r["no_eligible_node"]:<9} {r["timed_out_observation"]:<9} '
              f'{r["fresh_infeasible_selections"]:<17} '
              f'{r["confirmed_insufficient_cpu"]}')
    print(f'Perturbed workers (blocks): {result["perturbation_target_blocks"]}')
    if not result['comparable_configurations']:
        print('WARNING: differing experimental settings; do NOT pool for comparative claims.')
    print(f'Wrote offline audit: {args.output}')
    print('NO causal inference; timed-out observation is not proof of CPU admission failure.')


if __name__ == '__main__':
    main()
