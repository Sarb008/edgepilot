"""M6D: opt-in, controlled Kubernetes CPU-request *feasibility* boundary pilot.

One small sleeping reservation Pod causes an otherwise admissible request to
become infeasible on the withheld worker, while a second kind worker remains
admissible. The workload request is fixed using OLD observed capacity and an
experiment-defined margin BEFORE the perturbation occurs. Policies see a
mixed-age snapshot; a fresh snapshot stays with the evaluator only.

The requested CPU can be near node capacity, but neither Pod burns CPU: both
run `sleep`. This measures Kubernetes request-accounting/admission under stale
information, NOT CPU load, end-user QoS, or natural network delay.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from typing import Any, Callable
from uuid import uuid4

from .kube_pilot import CONTEXT, POLICIES, order_by_block, policy_for
from .kube_stale_pilot import (
    NAMESPACE, StaleConfig, assert_empty_namespace, inventory_by_name,
    mixed_samples, reservation_manifest, select_from_mixed, usable_workers,
    verified_request_change, wait_for_reservation,
)
from .kube_validation import (
    Kubectl, KubectlError, capture_snapshot, pod_manifest, pod_observation,
    samples_from_inventory,
)
from .models import Workload

FIELDS = (
    'block', 'position', 'policy', 'perturbed_worker', 'probe_cpu_m',
    'selected_worker', 'reference_worker', 'selected_differs_from_reference',
    'selected_feasible_in_fresh_snapshot', 'stale_target_feasible_in_old_snapshot',
    'stale_target_feasible_in_fresh_snapshot', 'fresh_peer_feasible',
    'target_age_seconds', 'peer_age_seconds', 'pre_arrival_staleness_delay_seconds',
    'wait_seconds', 'refreshes',
    'decision_elapsed_seconds', 'deadline_seconds', 'pod_name', 'pod_actual_worker',
    'terminal_phase', 'container_exit_code', 'unschedulable_reason',
    'end_to_end_seconds', 'on_time_observed', 'probe_cleanup_confirmed',
    'competitor_cleanup_confirmed', 'error',
)


@dataclass(frozen=True)
class FeasibilityConfig:
    context: str = CONTEXT
    namespace: str = NAMESPACE
    blocks: int = 1
    seed: int = 20261112
    deadline_seconds: float = 20.
    competitor_cpu_m: int = 200
    margin_cpu_m: int = 100
    competitor_hold_seconds: int = 60
    sleep_seconds: int = 2
    poll_interval_seconds: float = .5
    min_stale_age_seconds: float = 1.0

    def __post_init__(self):
        if self.context != CONTEXT or self.namespace != NAMESPACE:
            raise ValueError('restricted to named disposable kind cluster and namespace')
        if type(self.blocks) is not int or not 1 <= self.blocks <= 2:
            raise ValueError('blocks must be integer 1..2 (16-Pod maximum)')
        if type(self.seed) is not int:
            raise ValueError('seed must be integer')
        if type(self.competitor_cpu_m) is not int or not 100 <= self.competitor_cpu_m <= 500:
            raise ValueError('competitor_cpu_m must be integer 100..500')
        if type(self.margin_cpu_m) is not int or not 50 <= self.margin_cpu_m <= 200:
            raise ValueError('margin_cpu_m must be integer 50..200')
        if self.margin_cpu_m >= self.competitor_cpu_m:
            raise ValueError('margin must be smaller than the reservation increment')
        if type(self.competitor_hold_seconds) is not int or not 45 <= self.competitor_hold_seconds <= 90:
            raise ValueError('competitor_hold_seconds must be integer 45..90')
        if type(self.sleep_seconds) is not int or not 1 <= self.sleep_seconds <= 5:
            raise ValueError('sleep_seconds must be integer 1..5')
        if (isinstance(self.deadline_seconds, bool) or not isinstance(self.deadline_seconds, (int, float))
                or not math.isfinite(self.deadline_seconds) or not 15 <= self.deadline_seconds <= 60):
            raise ValueError('deadline_seconds must be finite 15..60')
        if (isinstance(self.poll_interval_seconds, bool)
                or not isinstance(self.poll_interval_seconds, (int, float))
                or not math.isfinite(self.poll_interval_seconds)
                or not .2 <= self.poll_interval_seconds <= 2.):
            raise ValueError('poll_interval_seconds must be finite .2..2')
        if (isinstance(self.min_stale_age_seconds, bool)
                or not isinstance(self.min_stale_age_seconds, (int, float))
                or not math.isfinite(self.min_stale_age_seconds)
                or not .5 <= self.min_stale_age_seconds <= 3.):
            raise ValueError('min_stale_age_seconds must be finite .5..3')
        self.stale_config()

    def stale_config(self) -> StaleConfig:
        return StaleConfig(context=self.context, namespace=self.namespace,
                           blocks=self.blocks, seed=self.seed,
                           deadline_seconds=self.deadline_seconds,
                           competitor_cpu_m=self.competitor_cpu_m,
                           competitor_hold_seconds=self.competitor_hold_seconds,
                           probe_cpu_m=50, probe_sleep_seconds=self.sleep_seconds,
                           placement_budget_seconds=8., max_wait_seconds=3.,
                           telemetry_refresh_seconds=.75,
                           poll_interval_seconds=self.poll_interval_seconds)


def request_from_old_capacity(old_target: dict[str, Any], peer: dict[str, Any],
                              config: FeasibilityConfig) -> int:
    """Fix treatment from OLD snapshot, before competitor is created.

    Abort if we cannot fit this constructed request on the peer with >= 50m
    spare room; do not tune the size using a later 'truth' observation.
    """
    old_free = old_target['estimated_unreserved_cpu_cores']
    peer_free = peer['estimated_unreserved_cpu_cores']
    if any(type(n) not in (int, float) or not math.isfinite(n) for n in (old_free, peer_free)):
        raise KubectlError('invalid CPU capacity observation')
    request_m = math.floor(old_free * 1000 + 1e-7) - config.margin_cpu_m
    if not 500 <= request_m <= 12000:
        raise KubectlError('CPU-request boundary outside 500..12000 millicores')
    if peer_free * 1000 - request_m < 50 - 1e-6:
        raise KubectlError('peer lacks >=50m headroom for capacity-boundary workload')
    if old_free * 1000 - request_m < 50 - 1e-6:
        raise KubectlError('old target would already be inadmissible')
    return request_m


def verify_boundary(old: dict[str, Any], fresh: dict[str, Any], target: str,
                    peer: str, request_m: int, config: FeasibilityConfig) -> dict[str, Any]:
    """Retrospective feasibility only; never pass this snapshot to policies."""
    verified_request_change(old, fresh, target, config.competitor_cpu_m)
    old_target = inventory_by_name(old)[target]
    fresh_target = inventory_by_name(fresh)[target]
    fresh_peer = inventory_by_name(fresh)[peer]
    old_ok = old_target['estimated_unreserved_cpu_cores'] * 1000 >= request_m
    new_ok = fresh_target['estimated_unreserved_cpu_cores'] * 1000 >= request_m
    peer_ok = fresh_peer['estimated_unreserved_cpu_cores'] * 1000 >= request_m + 50 - 1e-6
    if not old_ok or new_ok or not peer_ok:
        raise KubectlError('fresh snapshot fails expected feasibility boundary; abort trial')
    if not fresh_target['usable_for_model'] or not fresh_peer['usable_for_model']:
        raise KubectlError('worker eligibility changed during treatment')
    return {'stale_target_feasible_in_old_snapshot': old_ok,
            'stale_target_feasible_in_fresh_snapshot': new_ok,
            'fresh_peer_feasible': peer_ok}


def high_request_probe_manifest(name: str, hostname: str, request_m: int,
                                seconds: int) -> dict[str, Any]:
    """A sleeping Pod with high CPU REQUEST; it does not spin CPU."""
    if type(request_m) is not int or not 500 <= request_m <= 12000:
        raise ValueError('request_m must be 500..12000')
    if type(seconds) is not int or not 1 <= seconds <= 5:
        raise ValueError('sleep seconds must be 1..5')
    manifest = pod_manifest(name, hostname, 50, seconds)
    manifest['metadata']['labels']['edgepilot-v2-role'] = 'feasibility-probe'
    container = manifest['spec']['containers'][0]
    container['resources']['requests']['cpu'] = f'{request_m}m'
    container['resources']['limits']['cpu'] = f'{request_m}m'
    return manifest


def _unschedulable_reason(pod: dict[str, Any]) -> str | None:
    conditions = (pod.get('status') or {}).get('conditions') or []
    for condition in conditions:
        if (condition.get('type') == 'PodScheduled' and condition.get('status') == 'False'
                and condition.get('reason') == 'Unschedulable'):
            return str(condition.get('message') or 'Unschedulable')[:500]
    return None


def run_probe(client: Kubectl, config: FeasibilityConfig, name: str, hostname: str,
              request_m: int, workload_start: float, expected_worker_name: str,
              *, monotonic: Callable[[], float], sleep: Callable[[float], None]) -> dict[str, Any]:
    """Submit one Pod and delete EXACT name even after uncertain create failure."""
    manifest = high_request_probe_manifest(name, hostname, request_m, config.sleep_seconds)
    result: dict[str, Any] = {'pod_name': name, 'terminal_phase': 'NotCreated',
                              'on_time_observed': False, 'probe_cleanup_confirmed': False}
    attempted = False
    try:
        attempted = True
        client.call('create', '-n', config.namespace, '-f', '-', stdin=json.dumps(manifest))
        while True:
            try:
                pod = client.get_json('pod', name, '-n', config.namespace)
                obs = pod_observation(pod)
                result['terminal_phase'] = obs['phase']
                result['pod_actual_worker'] = obs['node_name']
                result['container_exit_code'] = obs['container_exit_code']
                result['unschedulable_reason'] = _unschedulable_reason(pod)
            except KubectlError as exc:
                result['last_poll_error'] = str(exc)
            elapsed = monotonic() - workload_start
            result['end_to_end_seconds'] = elapsed
            phase = result['terminal_phase']
            if phase in ('Succeeded', 'Failed'):
                result['on_time_observed'] = (phase == 'Succeeded'
                                              and result.get('container_exit_code') == 0
                                              and elapsed <= config.deadline_seconds
                                              and result.get('pod_actual_worker') == expected_worker_name)
                break
            if elapsed >= config.deadline_seconds:
                result['terminal_phase'] = 'TimedOutObservation'
                break
            sleep(min(config.poll_interval_seconds, config.deadline_seconds - elapsed))
    except (KubectlError, ValueError, RuntimeError) as exc:
        result['error'] = str(exc)
        result['terminal_phase'] = 'ProbeError'
    finally:
        if attempted:
            try:
                client.call('delete', 'pod', name, '-n', config.namespace,
                            '--ignore-not-found=true', '--wait=true', '--timeout=15s')
                result['probe_cleanup_confirmed'] = True
            except KubectlError as exc:
                result['probe_cleanup_error'] = str(exc)
    return result


def run_trial(client: Kubectl, policy: str, config: FeasibilityConfig, *,
              block: int, position: int,
              monotonic: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> tuple[dict[str, Any], dict[str, Any]]:
    if policy not in POLICIES or not 1 <= block <= config.blocks or not 1 <= position <= 4:
        raise ValueError('invalid trial plan')
    row: dict[str, Any] = {'block': block, 'position': position, 'policy': policy,
                           'deadline_seconds': config.deadline_seconds,
                           'terminal_phase': 'NotStarted', 'on_time_observed': False,
                           'probe_cleanup_confirmed': False,
                           'competitor_cleanup_confirmed': False}
    trace: dict[str, Any] = {'block': block, 'position': position, 'policy': policy}
    competitor_name = f'epv2-feas-res-{uuid4().hex[:10]}'
    competitor_attempted = False
    try:
        assert_empty_namespace(client, config.namespace)
        old = capture_snapshot(client, monotonic=monotonic)
        workers = usable_workers(old)
        target = workers[(block - 1) % 2]
        peer = next(w for w in workers if w['name'] != target['name'])
        row['perturbed_worker'] = target['name']
        request_m = request_from_old_capacity(target, peer, config)
        row['probe_cpu_m'] = request_m
        trace['old_snapshot'] = old
        trace['competitor_pod'] = competitor_name
        competitor_attempted = True
        client.call('create', '-n', config.namespace, '-f', '-', stdin=json.dumps(
            reservation_manifest(competitor_name, target['hostname_label'],
                                 config.competitor_cpu_m, config.competitor_hold_seconds)))
        wait_for_reservation(client, competitor_name, target['name'],
                             timeout=20., monotonic=monotonic, sleep=sleep)
        fresh = capture_snapshot(client, monotonic=monotonic)
        trace['fresh_snapshot'] = fresh
        row.update(verify_boundary(old, fresh, target['name'], peer['name'], request_m, config))
        mixed = mixed_samples(old, fresh, target['name'])
        trace['mixed_policy_input'] = {name: asdict(sample) for name, sample in mixed.items()}
        # Construct a genuinely stale sample BEFORE the workload arrives. Never
        # falsify source timestamps: optional pre-arrival aging is real elapsed time.
        before_age = monotonic()
        shortfall = config.min_stale_age_seconds - mixed[target['name']].age_at(before_age)
        if shortfall > 0:
            sleep(shortfall)
        wait_for_reservation(client, competitor_name, target['name'], timeout=1.,
                             monotonic=monotonic, sleep=sleep)
        row['pre_arrival_staleness_delay_seconds'] = max(shortfall, 0.)
        now = monotonic()
        row['target_age_seconds'] = mixed[target['name']].age_at(now)
        row['peer_age_seconds'] = mixed[peer['name']].age_at(now)
        workload = Workload(f'm6d-b{block}-{policy}', now, now + config.deadline_seconds,
                            request_m / 1000., float(config.sleep_seconds))
        reference_samples = samples_from_inventory(fresh['node_inventory'],
                        fresh['measurement_window_start_monotonic'], fresh['receipt_monotonic'])
        reference = policy_for('greedy', config.stale_config().policy_config()).decide(
            workload, now, reference_samples, 0.)
        row['reference_worker'] = reference.node_id if reference else None
        chosen = select_from_mixed(client, config.stale_config(), policy, workload, mixed,
                                   monotonic=monotonic, sleep=sleep)
        row['selected_worker'] = chosen['selected']
        row['refreshes'] = chosen['refreshes']
        row['wait_seconds'] = chosen['waited']
        row['decision_elapsed_seconds'] = monotonic() - workload.arrival_time
        row['selected_differs_from_reference'] = chosen['selected'] != row['reference_worker']
        if chosen['selected']:
            chosen_snapshot = inventory_by_name(fresh).get(chosen['selected'])
            if not chosen_snapshot or not chosen_snapshot['usable_for_model']:
                raise KubectlError('policy selected an invalid worker')
            row['selected_feasible_in_fresh_snapshot'] = (
                chosen_snapshot['estimated_unreserved_cpu_cores'] * 1000 >= request_m - 1e-6)
            if monotonic() >= workload.deadline:
                row['terminal_phase'] = 'DeadlineBeforeSubmit'
            else:
                probe_name = f'epv2-feas-job-{uuid4().hex[:10]}'
                row['pod_name'] = probe_name
                row.update(run_probe(client, config, probe_name,
                                     chosen_snapshot['hostname_label'], request_m,
                                     workload.arrival_time, chosen['selected'], monotonic=monotonic, sleep=sleep))
        else:
            row['terminal_phase'] = 'NoEligibleNode'
    except (KubectlError, ValueError, RuntimeError, KeyError) as exc:
        row['error'] = str(exc)
        row['terminal_phase'] = 'ProbeError'
    finally:
        if competitor_attempted:
            try:
                client.call('delete', 'pod', competitor_name, '-n', config.namespace,
                            '--ignore-not-found=true', '--wait=true', '--timeout=15s')
                row['competitor_cleanup_confirmed'] = True
            except KubectlError as exc:
                row['competitor_cleanup_error'] = str(exc)
    trace['result'] = row
    return row, trace


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for name in POLICIES:
        matching = [r for r in rows if r['policy'] == name]
        out.append({'policy': name, 'trials': len(matching),
                    'on_time_rate': (sum(bool(r.get('on_time_observed')) for r in matching) / len(matching)
                                     if matching else None),
                    'fresh_reference_disagreements': sum(bool(r.get('selected_differs_from_reference'))
                                                          for r in matching),
                    'fresh_snapshot_infeasible_choices': sum(
                        r.get('selected_feasible_in_fresh_snapshot') is False for r in matching),
                    'observed_timeouts': sum(r.get('terminal_phase') == 'TimedOutObservation'
                                             for r in matching)})
    return out


def run_experiment(client: Kubectl, config: FeasibilityConfig, output: Path, *,
                   monotonic: Callable[[], float] = time.monotonic,
                   sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    client.verify_context()
    assert_empty_namespace(client, config.namespace)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f'refuse to overwrite existing evidence: {output}')
    output.mkdir(parents=True, exist_ok=True)
    plan = order_by_block(config.blocks, config.seed)
    manifest = {'study': 'EdgePilot v2 / real kind CPU-request feasibility boundary pilot',
                'created_at_utc': datetime.now(timezone.utc).isoformat(),
                'config': asdict(config),
                'plan': [{'block': b, 'position': p, 'policy': policy} for b, p, policy in plan],
                'limitations': [
                    'Constructed request bound is selected from OLD telemetry before the competitor is created',
                    'The competitor and probe execute sleep, not CPU-burning work',
                    'Large CPU requests affect Kubernetes scheduling reservations, not actual CPU usage',
                    'All kind workers share one physical host',
                    'The fresh snapshot is a retrospective reference, not an instantaneous ground truth',
                    'Imposed per-worker information delay is not measured natural network delay',
                    'Policies use default scheduler + nodeSelector; not a custom scheduler',
                    'Trials are sequential and few; cannot establish causal generalization',
                    'Before arrival, the real stale sample is aged to at least configured minimum without timestamp forgery',
                    'Observed completion time includes API polling interval',
                ]}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    rows: list[dict[str, Any]] = []
    for block, position, name in plan:
        row, trace = run_trial(client, name, config, block=block, position=position,
                               monotonic=monotonic, sleep=sleep)
        rows.append(row)
        trace_dir = output / 'traces'
        trace_dir.mkdir(exist_ok=True)
        (trace_dir / f'block{block:02d}_position{position:02d}_{name}.json').write_text(
            json.dumps(trace, indent=2) + '\n', encoding='utf-8')
        _write_csv(output / 'trials.csv', rows)
        (output / 'summary.json').write_text(json.dumps(summarize(rows), indent=2) + '\n', encoding='utf-8')
        print(f'Block {block}/{config.blocks} - {name}: {row["terminal_phase"]}; '
              f'worker={row.get("selected_worker")}; '
              f'fresh-feasible={row.get("selected_feasible_in_fresh_snapshot")}; '
              f'on-time={row["on_time_observed"]}')
        if (row.get('error') or row.get('probe_cleanup_error') or row.get('competitor_cleanup_error')
                or not row.get('competitor_cleanup_confirmed')
                or (row.get('pod_name') and not row.get('probe_cleanup_confirmed'))):
            raise KubectlError('trial or cleanup failed; STOP and inspect dedicated namespace')
        # Verify actual namespace emptiness rather than relying only on accepted deletes.
        assert_empty_namespace(client, config.namespace)
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', default=CONTEXT)
    parser.add_argument('--namespace', default=NAMESPACE)
    parser.add_argument('--blocks', type=int, default=1)
    parser.add_argument('--seed', type=int, default=20261112)
    parser.add_argument('--deadline-seconds', type=float, default=20.)
    parser.add_argument('--competitor-cpu-m', type=int, default=200)
    parser.add_argument('--margin-cpu-m', type=int, default=100)
    parser.add_argument('--execute', action='store_true', help='Explicitly opt in to at most 16 disposable Pods')
    parser.add_argument('--output', type=Path, default=Path('research_outputs/kubernetes/feasibility_pilot'))
    args = parser.parse_args(argv)
    config = FeasibilityConfig(context=args.context, namespace=args.namespace,
                               blocks=args.blocks, seed=args.seed,
                               deadline_seconds=args.deadline_seconds,
                               competitor_cpu_m=args.competitor_cpu_m,
                               margin_cpu_m=args.margin_cpu_m)
    plan = order_by_block(config.blocks, config.seed)
    print(f'Plan: {len(plan)} sequential policy trials; at most {len(plan)*2} disposable Pods')
    print('Each workload requests CPU near observed worker capacity (SLEEP, not CPU burn).')
    if not args.execute:
        print('DRY RUN. No cluster reads or writes. Add --execute to opt in.')
        return
    client = Kubectl(config.context)
    rows = run_experiment(client, config, args.output)
    print(f'Wrote {len(rows)} real-cluster trials to {args.output}')
    for summary in summarize(rows):
        print(f'  {summary["policy"]:19} on-time={summary["on_time_rate"]:.1%}; '
              f'fresh-infeasible={summary["fresh_snapshot_infeasible_choices"]}')
    print('Controlled request-feasibility validation ONLY; no CPU utilization or causal claims.')


if __name__ == '__main__':
    main()
