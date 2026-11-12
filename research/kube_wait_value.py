"""Prospective opt-in pilot: BOTH workers' telemetry initially withheld.

This differs from M6I: that pilot gave the immediate age-penalized arm a fresh
peer measurement. Here both arms receive an old, equal-age snapshot from
before a controlled 200m request change. The bounded arm can request actual
subsequent telemetry; the immediate arm cannot. This isolates the information
available after waiting in a tightly scoped single-host kind pilot, while
retaining the real cost of waiting.

IMPORTANT: distinct sequential Kubernetes trials do NOT share identical physical
microstates, and results do NOT establish causal superiority on real clusters.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import time
from typing import Any, Callable
from uuid import uuid4

from .kube_tie_crossover import CrossoverConfig, greedy_tie_candidates
from .kube_feasibility_pilot import request_from_old_capacity, run_probe, verify_boundary
from .kube_pilot import CONTEXT, policy_for
from .kube_stale_pilot import (
    NAMESPACE, assert_empty_namespace, inventory_by_name, reservation_manifest,
    select_from_mixed, usable_workers, wait_for_reservation,
)
from .kube_validation import Kubectl, KubectlError, capture_snapshot, samples_from_inventory
from .models import Workload

ARMS = ('age_penalized', 'bounded_deferral')
FIELDS = (
    'repeat', 'block', 'position', 'arm', 'perturbed_worker',
    'probe_cpu_m', 'reference_worker', 'initial_action', 'initial_worker',
    'initial_equal_measurement_age', 'initial_reported_cpu_tie',
    'initial_target_age_seconds', 'selected_worker', 'selected_feasible_in_treatment',
    'wait_seconds', 'refreshes', 'decision_elapsed_seconds', 'deadline_seconds',
    'terminal_phase', 'unschedulable_reason', 'pod_name', 'pod_actual_worker',
    'container_exit_code', 'end_to_end_seconds', 'on_time_observed',
    'probe_cleanup_confirmed', 'competitor_cleanup_confirmed', 'error',
)


@dataclass(frozen=True)
class WaitValueConfig:
    context: str = CONTEXT
    namespace: str = NAMESPACE
    repeats: int = 1
    seed: int = 20261116
    deadline_seconds: float = 20.0
    competitor_cpu_m: int = 200
    margin_cpu_m: int = 100
    competitor_hold_seconds: int = 60
    sleep_seconds: int = 2
    poll_interval_seconds: float = .5
    min_stale_age_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.context != CONTEXT or self.namespace != NAMESPACE:
            raise ValueError('restricted to the designated disposable kind cluster/namespace')
        if type(self.repeats) is not int or not 1 <= self.repeats <= 2:
            raise ValueError('repeats must be 1..2 (16 Pod maximum)')
        if type(self.seed) is not int:
            raise ValueError('seed must be an integer')
        self.crossover_config()

    def crossover_config(self) -> CrossoverConfig:
        return CrossoverConfig(context=self.context, namespace=self.namespace,
                               repeats=self.repeats, seed=self.seed,
                               deadline_seconds=self.deadline_seconds,
                               competitor_cpu_m=self.competitor_cpu_m,
                               margin_cpu_m=self.margin_cpu_m,
                               competitor_hold_seconds=self.competitor_hold_seconds,
                               sleep_seconds=self.sleep_seconds,
                               poll_interval_seconds=self.poll_interval_seconds,
                               min_stale_age_seconds=self.min_stale_age_seconds)


def plan_for(cfg: WaitValueConfig) -> list[tuple[int, int, int, str]]:
    """Each worker treated once per repeat; arm positions balance if repeats=2."""
    base = list(ARMS)
    random.Random(cfg.seed).shuffle(base)
    plan = []
    for i in range(2 * cfg.repeats):
        repeat, block = i // 2 + 1, i + 1
        # Swap arm order across worker and repeat; within each worker's
        # exposure, repeats=2 puts each arm in both positions exactly once.
        swap = (i // 2 + i % 2) % 2
        ordered = base[swap:] + base[:swap]
        for position, arm in enumerate(ordered, start=1):
            plan.append((repeat, block, position, arm))
    return plan


def common_old_samples(old: dict[str, Any], required_cpu_m: int, now: float,
                       workload: Workload):
    """Fail closed unless initial CPU reports AND source ages are exactly tied."""
    samples = samples_from_inventory(old['node_inventory'],
                                     old['measurement_window_start_monotonic'],
                                     old['receipt_monotonic'])
    if len(samples) != 2:
        raise KubectlError('expected exactly two eligible worker samples')
    if any(s.received_at > now or s.measured_at > now for s in samples.values()):
        raise KubectlError('future telemetry in input')
    tie = greedy_tie_candidates(workload, samples)
    if len(tie) != 2:
        raise KubectlError('old measured capacities are not exactly tied; refusing unmatched trial')
    ages = [sample.age_at(now) for sample in samples.values()]
    if not math.isclose(ages[0], ages[1], rel_tol=0, abs_tol=1e-7):
        raise KubectlError('source ages differ; could confound immediate decision')
    if any(s.available_cpu * 1000 < required_cpu_m - 1e-6 for s in samples.values()):
        raise KubectlError('candidate is not initially eligible')
    return samples


def run_trial(client: Kubectl, arm: str, config: WaitValueConfig, *,
              repeat: int, block: int, position: int,
              monotonic: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> tuple[dict[str, Any], dict[str, Any]]:
    if (arm not in ARMS or (repeat, block, position, arm) not in plan_for(config)):
        raise ValueError('invalid trial plan')
    fc = config.crossover_config().feasibility_config()
    row: dict[str, Any] = {'repeat': repeat, 'block': block, 'position': position,
                           'arm': arm, 'deadline_seconds': config.deadline_seconds,
                           'terminal_phase': 'NotStarted', 'on_time_observed': False,
                           'probe_cleanup_confirmed': False, 'competitor_cleanup_confirmed': False}
    trace: dict[str, Any] = {'repeat': repeat, 'block': block, 'position': position, 'arm': arm}
    reservation_name = f'epv2-wait-res-{uuid4().hex[:10]}'
    attempted = False
    try:
        assert_empty_namespace(client, config.namespace)
        old = capture_snapshot(client, monotonic=monotonic)
        workers = usable_workers(old)
        target = workers[(block - 1) % 2]
        peer = next(worker for worker in workers if worker['name'] != target['name'])
        req = request_from_old_capacity(target, peer, fc)
        row['perturbed_worker'] = target['name']
        row['probe_cpu_m'] = req
        trace['old_snapshot'] = old
        trace['reservation_pod'] = reservation_name
        attempted = True
        client.call('create', '-n', config.namespace, '-f', '-', stdin=json.dumps(
            reservation_manifest(reservation_name, target['hostname_label'],
                                 config.competitor_cpu_m, config.competitor_hold_seconds)))
        wait_for_reservation(client, reservation_name, target['name'],
                             timeout=20., monotonic=monotonic, sleep=sleep)
        fresh = capture_snapshot(client, monotonic=monotonic)
        trace['fresh_snapshot'] = fresh
        row.update(verify_boundary(old, fresh, target['name'], peer['name'], req, fc))
        now = monotonic()
        sample_at_old = samples_from_inventory(old['node_inventory'],
                              old['measurement_window_start_monotonic'],
                              old['receipt_monotonic'])
        if target['name'] not in sample_at_old:
            raise KubectlError('target missing in original observation')
        shortfall = config.min_stale_age_seconds - sample_at_old[target['name']].age_at(now)
        if shortfall > 0:
            sleep(shortfall)
        wait_for_reservation(client, reservation_name, target['name'],
                             timeout=1., monotonic=monotonic, sleep=sleep)
        now = monotonic()
        workload = Workload(f'waitvalue-b{block}-{arm}', now, now + config.deadline_seconds,
                            req / 1000., float(config.sleep_seconds))
        old_samples = common_old_samples(old, req, now, workload)
        trace['initial_policy_input'] = {n: asdict(s) for n, s in old_samples.items()}
        row['initial_target_age_seconds'] = old_samples[target['name']].age_at(now)
        row['initial_equal_measurement_age'] = True
        row['initial_reported_cpu_tie'] = True
        fresh_samples = samples_from_inventory(fresh['node_inventory'],
                                  fresh['measurement_window_start_monotonic'],
                                  fresh['receipt_monotonic'])
        reference = policy_for('greedy', fc.stale_config().policy_config()).decide(
            workload, now, fresh_samples, 0.)
        row['reference_worker'] = reference.node_id if reference else None
        initial = policy_for(arm, fc.stale_config().policy_config()).decide(
            workload, now, old_samples, 0.)
        row['initial_action'] = ('reject' if initial is None else
                                 'wait' if initial.wait_until is not None else 'select')
        row['initial_worker'] = initial.node_id if initial else None
        if arm == 'age_penalized' and row['initial_action'] != 'select':
            raise KubectlError('age-penalized arm did not initially select')
        if arm == 'bounded_deferral' and row['initial_action'] != 'wait':
            raise KubectlError('bounded deferral failed to wait on stale common input')
        # The fresh snapshot above is NOT passed to this function; it must
        # obtain a NEW observation only after deliberate waiting.
        chosen = select_from_mixed(client, fc.stale_config(), arm,
                                   workload, old_samples, monotonic=monotonic, sleep=sleep)
        row['selected_worker'] = chosen['selected']
        row['wait_seconds'] = chosen['waited']
        row['refreshes'] = chosen['refreshes']
        row['decision_elapsed_seconds'] = monotonic() - workload.arrival_time
        if chosen['selected']:
            worker = inventory_by_name(fresh).get(chosen['selected'])
            if not worker or not worker['usable_for_model']:
                raise KubectlError('selected worker ineligible in treatment snapshot')
            row['selected_feasible_in_treatment'] = (worker['estimated_unreserved_cpu_cores'] * 1000 >= req - 1e-6)
            if monotonic() >= workload.deadline:
                row['terminal_phase'] = 'DeadlineBeforeSubmit'
            else:
                pod_name = f'epv2-wait-job-{uuid4().hex[:10]}'
                row['pod_name'] = pod_name
                row.update(run_probe(client, fc, pod_name, worker['hostname_label'],
                                     req, workload.arrival_time, chosen['selected'],
                                     monotonic=monotonic, sleep=sleep))
        else:
            row['terminal_phase'] = 'NoEligibleNode'
    except (KubectlError, ValueError, RuntimeError, KeyError) as exc:
        row['error'] = str(exc)
        row['terminal_phase'] = 'ProbeError'
    finally:
        if attempted:
            try:
                client.call('delete', 'pod', reservation_name, '-n', config.namespace,
                            '--ignore-not-found=true', '--wait=true', '--timeout=15s')
                row['competitor_cleanup_confirmed'] = True
            except KubectlError as exc:
                row['competitor_cleanup_error'] = str(exc)
    trace['result'] = row
    return row, trace


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for arm in ARMS:
        matches = [r for r in rows if r['arm'] == arm]
        result.append({'arm': arm, 'trials': len(matches),
                       'on_time': sum(r.get('on_time_observed') is True for r in matches),
                       'observed_timeouts': sum(r.get('terminal_phase') == 'TimedOutObservation' for r in matches),
                       'treatment_infeasible': sum(r.get('selected_feasible_in_treatment') is False for r in matches),
                       'total_wait_seconds': round(sum(float(r.get('wait_seconds') or 0) for r in matches), 6)})
    return result


def run_experiment(client: Kubectl, config: WaitValueConfig, output: Path, *,
                   monotonic: Callable[[], float] = time.monotonic,
                   sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    client.verify_context()
    assert_empty_namespace(client, config.namespace)
    if output.exists():
        raise FileExistsError(f'existing output; refusing overwrite: {output}')
    plan = plan_for(config)
    output.mkdir(parents=True)
    (output / 'manifest.json').write_text(json.dumps({
        'study': 'EdgePilot v2 / two-arm real Kubernetes delayed-information crossover',
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'config': asdict(config),
        'plan': [{'repeat': rep, 'block': b, 'position': pos, 'arm': arm,
                  'perturbed_worker_slot': (b - 1) % 2} for rep, b, pos, arm in plan],
        'limitations': [
            'Arms share pre-perturbation *protocol*, not literally the same live cluster state.',
            'Each initial input contains only the earlier real telemetry snapshot for BOTH workers.',
            'The reference snapshot is evaluator-only and never passed to selection.',
            'Bounded deferral must fetch a later, new snapshot after real elapsed waiting.',
            'Observed timeouts are not proof of persistent Kubernetes admission failure.',
            'Kubernetes CPU requests are not measured CPU utilization.',
            'Sleeping Pods and two Docker-backed workers sharing one physical host limit external validity.',
            'Default kube-scheduler plus nodeSelector is used; this is NOT a custom scheduler installation.',
            'Sequential trials do not establish a randomized parallel causal experiment.',
            'Small pilots must not be reported as statistically significant superiority.',
        ],
    }, indent=2) + '\n', encoding='utf-8')
    rows: list[dict[str, Any]] = []
    for repeat, block, position, arm in plan:
        row, trace = run_trial(client, arm, config, repeat=repeat, block=block,
                               position=position, monotonic=monotonic, sleep=sleep)
        rows.append(row)
        traces = output / 'traces'
        traces.mkdir(exist_ok=True)
        (traces / f'block{block:02d}_position{position:02d}_{arm}.json').write_text(
            json.dumps(trace, indent=2) + '\n', encoding='utf-8')
        with (output / 'trials.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
        (output / 'summary.json').write_text(json.dumps(summarize(rows), indent=2) + '\n', encoding='utf-8')
        print(f'B{block} #{position} {arm}: {row["terminal_phase"]}; '
              f'worker={row.get("selected_worker")}; on-time={row["on_time_observed"]}; '
              f'wait={float(row.get("wait_seconds") or 0):.2f}s')
        if (row.get('error') or row.get('probe_cleanup_error') or row.get('competitor_cleanup_error')
                or not row['competitor_cleanup_confirmed']
                or (row.get('pod_name') and not row['probe_cleanup_confirmed'])):
            raise KubectlError('trial/cleanup failure; STOP and inspect namespace')
        assert_empty_namespace(client, config.namespace)
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--seed', type=int, default=20261116)
    parser.add_argument('--deadline-seconds', type=float, default=20.)
    parser.add_argument('--output', type=Path, default=Path('research_outputs/kubernetes/wait_value_01'))
    parser.add_argument('--execute', action='store_true', help='explicit opt-in to up to 16 disposable Pods')
    args = parser.parse_args(argv)
    cfg = WaitValueConfig(repeats=args.repeats, seed=args.seed,
                          deadline_seconds=args.deadline_seconds)
    plan = plan_for(cfg)
    print(f'Plan: {len(plan)} trials, {2*cfg.repeats} worker-balanced blocks, '
          f'at most {2*len(plan)} disposable Pods.')
    for rep, block, pos, arm in plan:
        print(f'  R{rep} B{block} #{pos} {arm}; perturb worker slot {(block - 1) % 2}')
    if not args.execute:
        print('DRY RUN. No Kubernetes calls, Pods, or output files.')
        return
    rows = run_experiment(Kubectl(cfg.context), cfg, args.output)
    print(f'Completed {len(rows)} real Kubernetes trials in {args.output}')
    for record in summarize(rows):
        print(f'  {record["arm"]:19} on-time={record["on_time"]}/{record["trials"]} '
              f'fresh-infeasible={record["treatment_infeasible"]} '
              f'waiting={record["total_wait_seconds"]:.2f}s')
    print('EXPLORATORY only: sequential single-host kind Pods, not causal evidence of general superiority.')


if __name__ == '__main__':
    main()
