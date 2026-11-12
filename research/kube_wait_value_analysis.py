"""Offline, fail-closed audit of prospective M7 wait-versus-immediate runs.

No cluster calls. No counterfactual outcome imputation. Checks traces and CSV
against the exact recorded treatment, initial observation, and expected policy
behavior. A Kubernetes timeout remains a censored observation, not proof of
permanent failure. Rejects incomplete or modified evidence.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any

from .kube_wait_value import ARMS, FIELDS, WaitValueConfig, plan_for
from .kube_feasibility_pilot import request_from_old_capacity, verify_boundary
from .kube_pilot import policy_for
from .kube_stale_pilot import inventory_by_name, usable_workers
from .kube_validation import samples_from_inventory
from .models import Workload


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline='', encoding='utf-8') as f:
        data = list(csv.DictReader(f))
    if not data:
        raise ValueError(f'empty evidence: {path}')
    return data


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def audit(run: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest_path = run / 'manifest.json'
    trials_path = run / 'trials.csv'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    cfg = WaitValueConfig(**manifest['config'])
    plan = plan_for(cfg)
    expected_plan = [{'repeat': rep, 'block': block, 'position': pos, 'arm': arm,
                      'perturbed_worker_slot': (block - 1) % 2}
                     for rep, block, pos, arm in plan]
    if manifest['plan'] != expected_plan:
        raise ValueError('manifest plan differs from deterministic predeclared plan')
    rows = _csv(trials_path)
    if len(rows) != len(plan):
        raise ValueError(f'incomplete run: {len(rows)} of {len(plan)} planned trials')
    integrity: dict[str, str] = {}
    for p in (manifest_path, trials_path):
        integrity[p.relative_to(run).as_posix()] = sha256(p.read_bytes()).hexdigest()
    audited: list[dict[str, Any]] = []
    for csvrow, (rep, block, pos, arm) in zip(rows, plan):
        if (csvrow['repeat'], csvrow['block'], csvrow['position'], csvrow['arm']) != (
            str(rep), str(block), str(pos), arm
        ):
            raise ValueError('CSV row mismatches deterministic trial order')
        trace_path = run / 'traces' / f'block{block:02d}_position{pos:02d}_{arm}.json'
        trace = json.loads(trace_path.read_text(encoding='utf-8'))
        integrity[trace_path.relative_to(run).as_posix()] = sha256(trace_path.read_bytes()).hexdigest()
        recorded = trace['result']
        for field in FIELDS:
            if csvrow[field] != _text(recorded.get(field)):
                raise ValueError(f'trace/CSV mismatch {block}/{pos}/{arm}: {field}')
        if trace['arm'] != arm or trace['block'] != block or trace['position'] != pos or trace['repeat'] != rep:
            raise ValueError('trace plan identity mismatch')
        if (recorded.get('error') or recorded.get('probe_cleanup_error') or
                recorded.get('competitor_cleanup_error')):
            raise ValueError('evidence includes failed or ambiguous trial')
        if recorded.get('competitor_cleanup_confirmed') is not True:
            raise ValueError('reservation cleanup not confirmed')
        if recorded.get('pod_name') and recorded.get('probe_cleanup_confirmed') is not True:
            raise ValueError('workload cleanup not confirmed')
        old, fresh = trace['old_snapshot'], trace['fresh_snapshot']
        if (old['receipt_monotonic'] > fresh['measurement_window_start_monotonic'] or
                fresh['measurement_window_start_monotonic'] > fresh['receipt_monotonic']):
            raise ValueError('telemetry chronology violated')
        workers = usable_workers(old)
        target = workers[(block - 1) % 2]
        peer = next(worker for worker in workers if worker['name'] != target['name'])
        if recorded['perturbed_worker'] != target['name']:
            raise ValueError('perturbed worker mismatch')
        fc = cfg.crossover_config().feasibility_config()
        expected_request = request_from_old_capacity(target, peer, fc)
        if recorded['probe_cpu_m'] != expected_request:
            raise ValueError('probe request was not predetermined from OLD observation')
        boundary = verify_boundary(old, fresh, target['name'], peer['name'], expected_request, fc)
        for field, value in boundary.items():
            if recorded.get(field) != value:
                raise ValueError(f'feasibility boundary evidence mismatch: {field}')
        initial = samples_from_inventory(old['node_inventory'],
                                         old['measurement_window_start_monotonic'],
                                         old['receipt_monotonic'])
        recorded_initial = trace['initial_policy_input']
        if {name: asdict(sample) for name, sample in initial.items()} != recorded_initial:
            raise ValueError('initial policy input not original OLD telemetry')
        if len(initial) != 2 or len({round(s.available_cpu, 9) for s in initial.values()}) != 1:
            raise ValueError('initial reported CPU capacities were not tied')
        initial_now = initial[target['name']].measured_at + recorded['initial_target_age_seconds']
        if not all(sample.received_at <= initial_now for sample in initial.values()):
            raise ValueError('initial information arrived after policy decision')
        workload = Workload('audit', initial_now, initial_now + cfg.deadline_seconds,
                            expected_request / 1000., float(cfg.sleep_seconds))
        choice = policy_for(arm, fc.stale_config().policy_config()).decide(workload, initial_now,
                                                                          initial, 0.)
        action = ('reject' if choice is None else
                  'wait' if choice.wait_until is not None else 'select')
        if action != recorded['initial_action'] or (choice.node_id if choice else None) != recorded['initial_worker']:
            raise ValueError('recorded initial decision does not replay')
        if arm == 'bounded_deferral' and (recorded['wait_seconds'] <= 0 or recorded['refreshes'] <= 0):
            raise ValueError('bounded arm did not wait for a new observation')
        if arm == 'age_penalized' and (recorded['wait_seconds'] != 0 or recorded['refreshes'] != 0):
            raise ValueError('immediate arm unexpectedly waited')
        if recorded.get('selected_worker'):
            selected = inventory_by_name(fresh).get(recorded['selected_worker'])
            if selected is None:
                raise ValueError('selected unknown worker')
            feasible = selected['estimated_unreserved_cpu_cores'] * 1000 >= expected_request - 1e-6
            if recorded['selected_feasible_in_treatment'] != feasible:
                raise ValueError('reported feasibility differs from treatment snapshot')
        if recorded.get('on_time_observed') is True and (
                recorded['terminal_phase'] != 'Succeeded' or
                recorded['container_exit_code'] != 0 or
                not recorded.get('probe_cleanup_confirmed') or
                recorded.get('selected_feasible_in_treatment') is False):
            raise ValueError('invalid on-time completion evidence')
        audited.append({
            'repeat': rep, 'block': block, 'arm': arm,
            'perturbed_worker': target['name'],
            'on_time_observed': bool(recorded['on_time_observed']),
            'selected_worker': recorded.get('selected_worker'),
            'selected_feasible_in_treatment': recorded.get('selected_feasible_in_treatment'),
            'terminal_phase': recorded['terminal_phase'],
            'initial_action': action,
            'wait_seconds': float(recorded.get('wait_seconds') or 0.),
            'refreshes': int(recorded.get('refreshes') or 0),
        })
    report = {
        'study': 'EdgePilot v2 / initial-old-telemetry real crossover evidence audit',
        'runs': 1, 'blocks': 2 * cfg.repeats, 'trials': len(audited),
        'worker_exposure': {worker: sum(r['perturbed_worker'] == worker for r in audited) // 2
                            for worker in sorted({r['perturbed_worker'] for r in audited})},
        'by_arm': {arm: {
            'trials': sum(r['arm'] == arm for r in audited),
            'on_time': sum(r['arm'] == arm and r['on_time_observed'] for r in audited),
            'observed_timeouts': sum(r['arm'] == arm and r['terminal_phase'] == 'TimedOutObservation' for r in audited),
            'total_wait_seconds': round(sum(r['wait_seconds'] for r in audited if r['arm'] == arm), 6),
        } for arm in ARMS},
        'source_sha256': integrity,
        'limitations': [
            'Sequential real-cluster trials have separately acquired snapshots.',
            'Observed timeouts are censored outcomes, not proof of persistent admission failure.',
            'Small kind-cluster outcomes do not warrant confidence intervals or causal/general claims.',
            'Pod CPU requests do not measure runtime CPU utilization.',
        ],
    }
    return audited, report


def export_audit(run: Path, output: Path) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f'output exists: {output}')
    audited, report = audit(run)
    output.mkdir(parents=True)
    with (output / 'audited_trials.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=audited[0].keys())
        writer.writeheader()
        writer.writerows(audited)
    (output / 'audit.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('research_outputs/kubernetes/wait_value_audit_01'))
    args = parser.parse_args(argv)
    report = export_audit(args.run, args.output)
    print(f'Audited {report["trials"]} real-cluster trial records / {report["blocks"]} blocks (offline).')
    for arm, item in report['by_arm'].items():
        print(f'  {arm:19} on-time={item["on_time"]}/{item["trials"]} '
              f'timeouts={item["observed_timeouts"]} waited={item["total_wait_seconds"]:.2f}s')
    print(f'Wrote {args.output}; no Kubernetes calls or causal claims.')


if __name__ == '__main__':
    main()
