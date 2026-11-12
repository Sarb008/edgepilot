"""EdgePilot M6C: controlled REAL Kubernetes CPU-reservation stale-telemetry pilot.

Dry-run by default. --execute creates at most two Pod objects sequentially per
trial in a fixed kind context / fixed dedicated namespace: a harmless CPU-
reservation sleeper on one worker, then a short policy-directed probe Pod.

Fresh/old CPU-request snapshots are OBSERVED on the real cluster, with genuine
monotonic timestamps. Only the target worker's old observation is withheld
from the policy; the harness retains fresh data for diagnostic comparison.
No Pod is deliberately stressed with CPU cycles: this is *request contention*,
not a measurement of actual CPU utilization or a proof of deadline benefits.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import time
from typing import Any, Callable
from uuid import uuid4

from .kube_pilot import CONTEXT, POLICIES, PilotConfig, order_by_block, policy_for
from .kube_validation import (Kubectl, KubectlError, capture_snapshot,
                               pod_manifest, pod_observation, samples_from_inventory)
from .models import TelemetrySample, Workload

NAMESPACE = 'edgepilot-v2-validation'
TRIAL_FIELDS = (
    'block', 'position', 'policy', 'perturbed_worker', 'selected_worker',
    'fresh_reference_worker', 'selected_differs_from_fresh_reference',
    'stale_target_age_seconds', 'fresh_peer_age_seconds',
    'observed_target_request_delta_cores', 'competitor_cpu_m',
    'competitor_placed', 'telemetry_refreshes', 'intentional_wait_seconds',
    'decision_elapsed_seconds', 'pod_name', 'terminal_phase',
    'pod_actual_worker', 'placement_mismatch', 'container_exit_code',
    'end_to_end_seconds', 'kubernetes_creation_to_container_start_seconds',
    'kubernetes_container_execution_seconds', 'on_time_observed',
    'probe_cleanup_confirmed', 'competitor_cleanup_confirmed', 'error',
)


@dataclass(frozen=True)
class StaleConfig:
    context: str = CONTEXT
    namespace: str = NAMESPACE
    blocks: int = 1
    seed: int = 20261112
    deadline_seconds: float = 45.0
    probe_cpu_m: int = 50
    probe_sleep_seconds: int = 2
    competitor_cpu_m: int = 200
    competitor_hold_seconds: int = 60
    max_wait_seconds: float = 4.0
    freshness_threshold_seconds: float = .25
    telemetry_refresh_seconds: float = .75
    placement_budget_seconds: float = 20.0
    poll_interval_seconds: float = .5
    competitor_ready_timeout_seconds: float = 20.0

    def __post_init__(self):
        if self.context != CONTEXT or self.namespace != NAMESPACE:
            raise ValueError('M6C restricted to named disposable kind cluster and validation namespace')
        if type(self.blocks) is not int or not 1 <= self.blocks <= 3:
            raise ValueError('blocks must be integer 1..3 (24-Pod maximum)')
        if type(self.seed) is not int:
            raise ValueError('seed must be integer')
        if type(self.competitor_cpu_m) is not int or not 100 <= self.competitor_cpu_m <= 500:
            raise ValueError('competitor_cpu_m must be integer 100..500')
        if type(self.competitor_hold_seconds) is not int or not 45 <= self.competitor_hold_seconds <= 90:
            raise ValueError('competitor_hold_seconds must be integer 45..90')
        if (isinstance(self.competitor_ready_timeout_seconds, bool)
                or not isinstance(self.competitor_ready_timeout_seconds, (int, float))
                or not math.isfinite(self.competitor_ready_timeout_seconds)
                or not 5 <= self.competitor_ready_timeout_seconds <= 30):
            raise ValueError('competitor_ready_timeout_seconds must be 5..30')
        PilotConfig(context=self.context, namespace=self.namespace, blocks=self.blocks,
                    seed=self.seed, deadline_seconds=self.deadline_seconds,
                    cpu_m=self.probe_cpu_m, sleep_seconds=self.probe_sleep_seconds,
                    max_wait_seconds=self.max_wait_seconds,
                    freshness_threshold_seconds=self.freshness_threshold_seconds,
                    telemetry_refresh_seconds=self.telemetry_refresh_seconds,
                    placement_budget_seconds=self.placement_budget_seconds,
                    poll_interval_seconds=self.poll_interval_seconds)

    def policy_config(self) -> PilotConfig:
        return PilotConfig(context=self.context, namespace=self.namespace,
                           blocks=self.blocks, seed=self.seed,
                           deadline_seconds=self.deadline_seconds,
                           cpu_m=self.probe_cpu_m,
                           sleep_seconds=self.probe_sleep_seconds,
                           max_wait_seconds=self.max_wait_seconds,
                           freshness_threshold_seconds=self.freshness_threshold_seconds,
                           telemetry_refresh_seconds=self.telemetry_refresh_seconds,
                           placement_budget_seconds=self.placement_budget_seconds,
                           poll_interval_seconds=self.poll_interval_seconds)


def reservation_manifest(name: str, hostname: str, cpu_m: int, hold: int) -> dict[str, Any]:
    """Tiny sleeping Pod that reserves CPU requests, not CPU runtime load."""
    from .kube_validation import pod_manifest
    # Reuse validated Pod-name and hostname checks before expanding duration.
    manifest = pod_manifest(name, hostname, 50, 2)
    if type(cpu_m) is not int or not 100 <= cpu_m <= 500:
        raise ValueError('invalid reservation request')
    if type(hold) is not int or not 45 <= hold <= 90:
        raise ValueError('invalid reservation duration')
    manifest['metadata']['labels']['edgepilot-v2-role'] = 'reservation'
    container = manifest['spec']['containers'][0]
    container['command'] = ['sh', '-c', f'sleep {hold}']
    container['resources']['requests']['cpu'] = f'{cpu_m}m'
    container['resources']['limits']['cpu'] = f'{cpu_m}m'
    return manifest


def usable_workers(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    workers = [node for node in snapshot['node_inventory'] if node['usable_for_model']]
    workers.sort(key=lambda node: node['name'])
    if len(workers) != 2:
        raise KubectlError(f'M6C expects exactly 2 usable kind workers, found {len(workers)}')
    return workers


def inventory_by_name(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node['name']: node for node in snapshot['node_inventory']}


def mixed_samples(old: dict[str, Any], fresh: dict[str, Any],
                  withheld_worker: str) -> dict[str, TelemetrySample]:
    """Construct actual per-node mixed-age samples; never backdate timestamps."""
    old_nodes = inventory_by_name(old)
    fresh_nodes = inventory_by_name(fresh)
    if (old['receipt_monotonic'] > fresh['measurement_window_start_monotonic']
            or fresh['measurement_window_start_monotonic'] > fresh['receipt_monotonic']):
        raise ValueError('old and fresh snapshots are not causally ordered')
    if withheld_worker not in old_nodes or withheld_worker not in fresh_nodes:
        raise ValueError('withheld worker missing from snapshots')
    old_sample = samples_from_inventory(old['node_inventory'],
                                        old['measurement_window_start_monotonic'],
                                        old['receipt_monotonic'])
    fresh_samples = samples_from_inventory(fresh['node_inventory'],
                                           fresh['measurement_window_start_monotonic'],
                                           fresh['receipt_monotonic'])
    if withheld_worker not in old_sample or withheld_worker not in fresh_samples:
        raise ValueError('withheld worker must remain eligible')
    if set(old_sample) != set(fresh_samples):
        raise ValueError('eligible node set changed; cannot form matched mixed observation')
    # Stale target: previous actually measured CPU request. Other nodes: fresh.
    fresh_samples[withheld_worker] = old_sample[withheld_worker]
    return fresh_samples


def verified_request_change(old: dict[str, Any], fresh: dict[str, Any],
                            worker: str, reservation_m: int) -> float:
    prev = inventory_by_name(old)[worker]['requested_cpu_cores']
    now = inventory_by_name(fresh)[worker]['requested_cpu_cores']
    delta = now - prev
    if not math.isclose(delta, reservation_m / 1000., abs_tol=.015):
        raise KubectlError(f'reservation request delta {delta:.3f} cores not equal to '
                           f'controlled {reservation_m / 1000.:.3f}; abort inconclusive trial')
    return delta


def assert_empty_namespace(client: Kubectl, namespace: str):
    client.get_json('namespace', namespace)
    pods = client.get_json('pods', '-n', namespace)
    if not isinstance(pods.get('items'), list) or pods['items']:
        raise KubectlError('validation namespace must contain ZERO Pods before each trial')


def wait_for_reservation(client: Kubectl, name: str, expected_worker: str,
                         *, timeout: float, monotonic: Callable[[], float],
                         sleep: Callable[[float], None]) -> dict[str, Any]:
    start = monotonic()
    while True:
        pod = client.get_json('pod', name, '-n', NAMESPACE)
        phase = (pod.get('status') or {}).get('phase', 'Unknown')
        placed = (pod.get('spec') or {}).get('nodeName')
        if placed and placed != expected_worker:
            raise KubectlError(f'reservation mis-placed on {placed!r}')
        if phase == 'Running' and placed == expected_worker:
            return pod
        if phase in ('Succeeded', 'Failed'):
            raise KubectlError(f'reservation prematurely finished ({phase})')
        if monotonic() - start >= timeout:
            raise KubectlError('reservation Pod did not become Running before timeout')
        sleep(min(.5, max(.1, timeout - (monotonic() - start))))


def select_from_mixed(client: Kubectl, cfg: StaleConfig, name: str,
                      workload: Workload, samples: dict[str, TelemetrySample],
                      *, monotonic: Callable[[], float],
                      sleep: Callable[[float], None]) -> dict[str, Any]:
    """Act on mixed input first; bounded deferral may refresh AFTER waiting."""
    policy = policy_for(name, cfg.policy_config())
    refreshes = 0
    waited = 0.
    now = monotonic()
    if any(s.received_at > now + 1e-6 for s in samples.values()):
        raise ValueError('sample not received yet')
    initial = policy.decide(workload, now, samples, now - workload.arrival_time)
    initial_node = initial.node_id if initial and initial.node_id is not None else None
    while True:
        decision = policy.decide(workload, now, samples, now - workload.arrival_time)
        if decision is None:
            return {'selected': None, 'initial_selected': initial_node,
                    'refreshes': refreshes, 'waited': waited}
        if decision.node_id is not None:
            return {'selected': decision.node_id, 'initial_selected': initial_node,
                    'refreshes': refreshes, 'waited': waited}
        if name != 'bounded_deferral':
            raise RuntimeError('unexpected wait from immediate policy')
        remaining = decision.wait_until - now
        if remaining <= 1e-6:
            raise RuntimeError('nonprogressing bounded deferral')
        duration = min(cfg.telemetry_refresh_seconds, remaining)
        before = monotonic()
        sleep(duration)
        after = monotonic()
        if after <= before:
            raise RuntimeError('clock failed to advance')
        waited += after - before
        snapshot = capture_snapshot(client, monotonic=monotonic)
        refreshes += 1
        samples = samples_from_inventory(snapshot['node_inventory'],
                                         snapshot['measurement_window_start_monotonic'],
                                         snapshot['receipt_monotonic'])
        now = monotonic()


def observe_probe(client: Kubectl, cfg: StaleConfig, node: dict[str, Any],
                  *, workload_start: float,
                  monotonic: Callable[[], float],
                  sleep: Callable[[float], None]) -> dict[str, Any]:
    """Attempt exactly one Pod, with exact-name cleanup even on create failure."""
    pod_name = f'epv2-stale-job-{uuid4().hex[:10]}'
    manifest = pod_manifest(pod_name, node['hostname_label'],
                            cfg.probe_cpu_m, cfg.probe_sleep_seconds)
    result: dict[str, Any] = {'pod_name': pod_name, 'selected_worker': node['name'],
                              'terminal_phase': 'NotCreated',
                              'probe_cleanup_confirmed': False,
                              'on_time_observed': False}
    create_attempted = False
    try:
        create_attempted = True
        client.call('create', '-n', cfg.namespace, '-f', '-', stdin=json.dumps(manifest))
        while True:
            try:
                pod = client.get_json('pod', pod_name, '-n', cfg.namespace)
                obs = pod_observation(pod)
                result.update({
                    'terminal_phase': obs['phase'],
                    'pod_actual_worker': obs['node_name'],
                    'placement_mismatch': bool(obs['node_name'] and obs['node_name'] != node['name']),
                    'container_exit_code': obs['container_exit_code'],
                    'kubernetes_creation_to_container_start_seconds':
                        obs['kubernetes_creation_to_container_start_seconds'],
                    'kubernetes_container_execution_seconds':
                        obs['kubernetes_container_execution_seconds'],
                })
            except KubectlError as exc:
                result['poll_error'] = str(exc)
            elapsed = monotonic() - workload_start
            result['end_to_end_seconds'] = elapsed
            if result['terminal_phase'] in ('Succeeded', 'Failed'):
                result['on_time_observed'] = (
                    result['terminal_phase'] == 'Succeeded'
                    and result.get('container_exit_code') == 0
                    and not result.get('placement_mismatch', False)
                    and elapsed <= cfg.deadline_seconds)
                break
            if elapsed >= cfg.deadline_seconds:
                result['terminal_phase'] = 'TimedOutObservation'
                break
            sleep(min(cfg.poll_interval_seconds, cfg.deadline_seconds - elapsed))
    finally:
        if create_attempted:
            try:
                client.call('delete', 'pod', pod_name, '-n', cfg.namespace,
                            '--ignore-not-found=true', '--wait=true', '--timeout=15s')
                result['probe_cleanup_confirmed'] = True
            except KubectlError as exc:
                result['probe_cleanup_error'] = str(exc)
    return result


def run_stale_trial(client: Kubectl, policy: str, cfg: StaleConfig, *,
                    block: int, position: int,
                    monotonic: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep) -> tuple[dict[str, Any], dict[str, Any]]:
    if policy not in POLICIES or not 1 <= block <= cfg.blocks or not 1 <= position <= 4:
        raise ValueError('invalid trial plan')
    row: dict[str, Any] = {
        'block': block, 'position': position, 'policy': policy,
        'competitor_cpu_m': cfg.competitor_cpu_m,
        'competitor_placed': False,
        'on_time_observed': False,
        'probe_cleanup_confirmed': False, 'competitor_cleanup_confirmed': False,
        'terminal_phase': 'NotStarted',
    }
    trace: dict[str, Any] = {'block': block, 'position': position, 'policy': policy}
    competitor_name: str | None = None
    creation_attempted = False
    try:
        assert_empty_namespace(client, cfg.namespace)
        old = capture_snapshot(client, monotonic=monotonic)
        workers = usable_workers(old)
        target = workers[(block - 1) % 2]
        row['perturbed_worker'] = target['name']
        competitor_name = f'epv2-stale-res-{uuid4().hex[:10]}'
        trace['reservation_pod'] = competitor_name
        creation_attempted = True
        client.call('create', '-n', cfg.namespace, '-f', '-',
                    stdin=json.dumps(reservation_manifest(competitor_name,
                         target['hostname_label'], cfg.competitor_cpu_m,
                         cfg.competitor_hold_seconds)))
        wait_for_reservation(client, competitor_name, target['name'],
                             timeout=cfg.competitor_ready_timeout_seconds,
                             monotonic=monotonic, sleep=sleep)
        row['competitor_placed'] = True
        fresh = capture_snapshot(client, monotonic=monotonic)
        delta = verified_request_change(old, fresh, target['name'], cfg.competitor_cpu_m)
        row['observed_target_request_delta_cores'] = delta
        samples = mixed_samples(old, fresh, target['name'])
        # Truth is ONLY retained for retrospective evaluation, never passed to
        # a policy as the target's input while running on the mixed sample.
        trace['old_snapshot'] = old
        trace['fresh_snapshot'] = fresh
        trace['mixed_policy_input'] = {n: asdict(s) for n, s in samples.items()}
        now = monotonic()
        stale = samples[target['name']]
        fresh_peer = next(s for n, s in samples.items() if n != target['name'])
        row['stale_target_age_seconds'] = stale.age_at(now)
        row['fresh_peer_age_seconds'] = fresh_peer.age_at(now)
        workload = Workload(f'm6c-b{block}-{policy}', now, now + cfg.deadline_seconds,
                            cfg.probe_cpu_m / 1000., float(cfg.probe_sleep_seconds))
        reference = policy_for('greedy', cfg.policy_config()).decide(
            workload, now, samples_from_inventory(
                fresh['node_inventory'], fresh['measurement_window_start_monotonic'],
                fresh['receipt_monotonic']), 0)
        row['fresh_reference_worker'] = reference.node_id if reference else None
        chosen = select_from_mixed(client, cfg, policy, workload, samples,
                                   monotonic=monotonic, sleep=sleep)
        row['selected_worker'] = chosen['selected']
        row['telemetry_refreshes'] = chosen['refreshes']
        row['intentional_wait_seconds'] = chosen['waited']
        row['decision_elapsed_seconds'] = monotonic() - workload.arrival_time
        row['selected_differs_from_fresh_reference'] = (
            chosen['selected'] != row['fresh_reference_worker'])
        trace['decision'] = chosen
        if not chosen['selected']:
            row['terminal_phase'] = 'NoEligibleNode'
        elif monotonic() >= workload.deadline:
            row['terminal_phase'] = 'DeadlineBeforeSubmit'
        else:
            latest_inventory = inventory_by_name(fresh)
            selected = latest_inventory.get(chosen['selected'])
            if selected is None or not selected['usable_for_model']:
                raise KubectlError('selected worker absent from observed inventory')
            probe = observe_probe(client, cfg, selected,
                                  workload_start=workload.arrival_time,
                                  monotonic=monotonic, sleep=sleep)
            row.update(probe)
            trace['probe'] = probe
    except (KubectlError, ValueError, RuntimeError, KeyError) as exc:
        row['error'] = str(exc)
        row['terminal_phase'] = 'ProbeError'
    finally:
        if creation_attempted and competitor_name is not None:
            try:
                client.call('delete', 'pod', competitor_name, '-n', cfg.namespace,
                            '--ignore-not-found=true', '--wait=true', '--timeout=15s')
                row['competitor_cleanup_confirmed'] = True
            except KubectlError as exc:
                row['competitor_cleanup_error'] = str(exc)
    if 'end_to_end_seconds' not in row and 'decision_elapsed_seconds' in row:
        row['end_to_end_seconds'] = row['decision_elapsed_seconds']
    trace['result'] = row
    return row, trace


def _save_csv(path: Path, rows: list[dict[str, Any]]):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=TRIAL_FIELDS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for name in POLICIES:
        matching = [row for row in rows if row['policy'] == name]
        durations = [row['end_to_end_seconds'] for row in matching
                     if row.get('terminal_phase') == 'Succeeded'
                     and isinstance(row.get('end_to_end_seconds'), (int, float))]
        out.append({
            'policy': name,
            'trials': len(matching),
            'on_time_rate': sum(bool(r.get('on_time_observed')) for r in matching) / len(matching)
                if matching else None,
            'selection_differs_from_fresh_reference': sum(
                bool(r.get('selected_differs_from_fresh_reference')) for r in matching),
            'mean_successful_end_to_end_seconds': statistics.mean(durations) if durations else None,
            'mean_wait_seconds': statistics.mean(
                float(r.get('intentional_wait_seconds') or 0) for r in matching)
                if matching else None,
        })
    return out


def run_experiment(client: Kubectl, cfg: StaleConfig, output: Path, *,
                   monotonic: Callable[[], float] = time.monotonic,
                   sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    client.verify_context()
    assert_empty_namespace(client, cfg.namespace)
    if (output / 'manifest.json').exists() or (output / 'trials.csv').exists():
        raise FileExistsError(f'experiment output already exists: {output}; use a new --output')
    output.mkdir(parents=True, exist_ok=True)
    plan = order_by_block(cfg.blocks, cfg.seed)
    manifest = {
        'study': 'EdgePilot v2 / real Kubernetes controlled reservation and withheld-telemetry pilot',
        'observed_at_utc': datetime.now(timezone.utc).isoformat(),
        'measurement_class': 'real kind request-reservation perturbation + mixed-age telemetry; exploratory',
        'config': asdict(cfg),
        'plan': [{'block': b, 'position': p, 'policy': policy} for b, p, policy in plan],
        'limitations': [
            'All Pods run on kind nodes sharing the same host hardware',
            'Reservation Pod sleeps; this is Kubernetes CPU request contention, not CPU load',
            'The target worker old sample is intentionally withheld; the harness keeps a fresh reference',
            'Mixed worker observations are real but taken at different times, not an atomic cluster state',
            'Kubernetes default scheduler still binds Pods through nodeSelector',
            'Each policy runs sequentially; no simultaneous or causal paired trials',
            'A few blocks cannot support statistical superiority claims',
            'Any competitor or probe cleanup error aborts subsequent trials',
        ],
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    rows: list[dict[str, Any]] = []
    for block, position, policy in plan:
        row, trace = run_stale_trial(client, policy, cfg,
                                     block=block, position=position,
                                     monotonic=monotonic, sleep=sleep)
        rows.append(row)
        trace_dir = output / 'traces'
        trace_dir.mkdir(exist_ok=True)
        (trace_dir / f'block{block:02d}_position{position:02d}_{policy}.json').write_text(
            json.dumps(trace, indent=2) + '\n', encoding='utf-8')
        _save_csv(output / 'trials.csv', rows)
        (output / 'summary.json').write_text(json.dumps(summarize(rows), indent=2) + '\n',
                                              encoding='utf-8')
        print(f'Block {block}/{cfg.blocks} - {policy}: '
              f'{row["terminal_phase"]}; selected={row.get("selected_worker")}; '
              f'on-time={row.get("on_time_observed")}; '
              f'old-age={row.get("stale_target_age_seconds", 0):.2f}s')
        if (not row.get('competitor_cleanup_confirmed')
                or (row.get('pod_name') and not row.get('probe_cleanup_confirmed'))):
            raise KubectlError('Pod cleanup not confirmed; STOP and inspect namespace')
        if row.get('error'):
            raise KubectlError(f'trial failed: {row["error"]}; STOP and inspect cluster')
    return rows


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', default=CONTEXT)
    parser.add_argument('--namespace', default=NAMESPACE)
    parser.add_argument('--blocks', type=int, default=1)
    parser.add_argument('--seed', type=int, default=20261112)
    parser.add_argument('--competitor-cpu-m', type=int, default=200)
    parser.add_argument('--competitor-hold-seconds', type=int, default=60)
    parser.add_argument('--deadline-seconds', type=float, default=45.)
    parser.add_argument('--execute', action='store_true', help='Opt in to up to 24 disposable Pods')
    parser.add_argument('--output', type=Path,
                        default=Path('research_outputs/kubernetes/stale_pilot'))
    args = parser.parse_args(argv)
    cfg = StaleConfig(context=args.context, namespace=args.namespace,
                      blocks=args.blocks, seed=args.seed,
                      competitor_cpu_m=args.competitor_cpu_m,
                      competitor_hold_seconds=args.competitor_hold_seconds,
                      deadline_seconds=args.deadline_seconds)
    plan = order_by_block(cfg.blocks, cfg.seed)
    print(f'Plan: {len(plan)} policy trials; up to {len(plan) * 2} disposable Pods '
          f'(one reservation + one probe per trial)')
    print('Order: ' + ', '.join(f'B{b}:{p}' for b, _, p in plan))
    if not args.execute:
        print('DRY RUN. No Kubernetes reads/writes. Add --execute to opt in.')
        return
    client = Kubectl(cfg.context)
    client.verify_context()
    rows = run_experiment(client, cfg, args.output)
    print(f'Recorded {len(rows)} real-cluster trials in {args.output}')
    for rec in summarize(rows):
        print(f'  {rec["policy"]:19s} '
              f'on-time={rec["on_time_rate"]:.1%}; '
              f'changed-vs-fresh={rec["selection_differs_from_fresh_reference"]}')
    print('Exploratory measurements only; not a custom scheduler, causal design or CPU utilization test.')


if __name__ == '__main__':
    main()
