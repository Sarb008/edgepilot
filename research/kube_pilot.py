"""Explicitly opt-in, sequential *real Kubernetes* policy pilot.

Controlled workload + per-block policy rotation, but NOT a causal or truly
simultaneous paired experiment. Times measured on the client are monotonic.
Kubernetes timestamp fields are recorded as independent diagnostics.

Safety boundaries: context locked to the disposable kind cluster, namespace
validated, at most 20 Pods sequentially, CPU requests bounded, exact Pod name
cleanup attempted even after uncertain create outcomes. Default is a dry run.
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
import statistics
import time
from typing import Any, Callable
from uuid import uuid4

from .baselines import AgePenalized, UncertaintyAware
from .kube_validation import (
    Kubectl, KubectlError, _NAMESPACE, capture_snapshot, pod_manifest,
    pod_observation, samples_from_inventory,
)
from .models import Workload
from .policies import BoundedDeferral, Greedy

POLICIES = ('greedy', 'age_penalized', 'bounded_deferral', 'uncertainty_aware')
CONTEXT = 'kind-edgepilot-v2'
FIELDS = (
    'block', 'order', 'policy', 'pod_name', 'node_selected', 'node_actual',
    'terminal_phase', 'on_time_observed', 'placement_mismatch', 'container_exit_code', 'deadline_seconds',
    'end_to_end_observed_seconds', 'decision_elapsed_seconds',
    'deferral_wait_seconds', 'telemetry_reads', 'telemetry_age_at_dispatch_seconds',
    'api_submit_seconds', 'kubernetes_creation_to_container_start_seconds',
    'kubernetes_container_execution_seconds', 'cleanup_attempted_and_accepted',
    'cleanup_error', 'error',
)


@dataclass(frozen=True)
class PilotConfig:
    context: str = CONTEXT
    namespace: str = 'edgepilot-v2-validation'
    blocks: int = 2
    seed: int = 20261112
    deadline_seconds: float = 45.0
    cpu_m: int = 50
    sleep_seconds: int = 2
    max_wait_seconds: float = 4.0
    freshness_threshold_seconds: float = .25
    telemetry_refresh_seconds: float = .75
    placement_budget_seconds: float = 20.0
    poll_interval_seconds: float = .5

    def __post_init__(self):
        if self.context != CONTEXT:
            raise ValueError(f'pilot limited to disposable {CONTEXT} context')
        if not isinstance(self.namespace, str) or not _NAMESPACE.fullmatch(self.namespace):
            raise ValueError('pilot requires isolated edgepilot-v2-validation namespace')
        if type(self.blocks) is not int or not 1 <= self.blocks <= 5:
            raise ValueError('blocks must be integer 1..5 (20-Pod limit)')
        if type(self.seed) is not int:
            raise ValueError('seed must be integer')
        if type(self.cpu_m) is not int or not 10 <= self.cpu_m <= 200:
            raise ValueError('cpu_m must be integer 10..200')
        if type(self.sleep_seconds) is not int or not 1 <= self.sleep_seconds <= 10:
            raise ValueError('sleep_seconds must be integer 1..10')
        for field in ('deadline_seconds', 'max_wait_seconds', 'freshness_threshold_seconds',
                      'telemetry_refresh_seconds', 'placement_budget_seconds',
                      'poll_interval_seconds'):
            val = getattr(self, field)
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
                raise ValueError(f'{field} must be finite numeric')
            low = .2 if field in ('poll_interval_seconds', 'telemetry_refresh_seconds') else 0
            if val < low:
                raise ValueError(f'{field} must be >= {low}')
        if not 10 <= self.deadline_seconds <= 120:
            raise ValueError('deadline must be 10..120 seconds')
        if self.max_wait_seconds > 15:
            raise ValueError('max_wait_seconds must be <=15 seconds')
        if self.placement_budget_seconds > self.deadline_seconds:
            raise ValueError('placement budget cannot exceed deadline')
        if self.poll_interval_seconds > 5 or self.telemetry_refresh_seconds > 5:
            raise ValueError('poll/refresh intervals must be <= 5 seconds')


def order_by_block(blocks: int, seed: int) -> list[tuple[int, int, str]]:
    """Reproducible rotating order; each group of four blocks balances positions."""
    if not isinstance(blocks, int) or not 1 <= blocks <= 5:
        raise ValueError('blocks must be 1..5')
    order = list(POLICIES)
    random.Random(seed).shuffle(order)
    return [(block + 1, slot + 1, order[(slot + block) % 4])
            for block in range(blocks) for slot in range(4)]


def policy_for(name: str, config: PilotConfig):
    if name == 'greedy':
        return Greedy()
    if name == 'age_penalized':
        return AgePenalized()
    if name == 'uncertainty_aware':
        return UncertaintyAware()
    if name == 'bounded_deferral':
        return BoundedDeferral(max_wait=config.max_wait_seconds,
                               freshness_threshold=config.freshness_threshold_seconds,
                               placement_latency=config.placement_budget_seconds)
    raise ValueError(f'unknown policy {name!r}')


def choose_node(client: Kubectl, policy_name: str, cfg: PilotConfig,
                workload: Workload, *, monotonic: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Use only API snapshots whose receipt time is <= decision time.

    Bounded-deferral rechecks telemetry on configured refresh intervals and
    stops waiting at the hard maximum. No knowledge of future node state.
    """
    policy = policy_for(policy_name, cfg)
    read_count = 0
    total_wait = 0.0
    while True:
        snapshot = capture_snapshot(client, monotonic=monotonic)
        read_count += 1
        now = monotonic()
        if snapshot['receipt_monotonic'] > now + 1e-6:
            raise RuntimeError('snapshot receipt is in the future')
        samples = samples_from_inventory(snapshot['node_inventory'],
                                         snapshot['measurement_window_start_monotonic'],
                                         snapshot['receipt_monotonic'])
        decision = policy.decide(workload, now, samples, now - workload.arrival_time)
        if decision is None:
            return {'node': None, 'status': 'NoEligibleNode', 'reads': read_count,
                    'wait': total_wait, 'snapshot': snapshot, 'age': None}
        if decision.node_id is not None:
            chosen = next((n for n in snapshot['node_inventory']
                           if n['name'] == decision.node_id and n['usable_for_model']), None)
            if chosen is None:
                raise RuntimeError('policy selected non-usable node')
            return {'node': chosen, 'status': 'Selected', 'reads': read_count,
                    'wait': total_wait, 'snapshot': snapshot,
                    'age': now - snapshot['measurement_window_start_monotonic']}
        # Only BoundedDeferral should generate a wait decision.
        if policy_name != 'bounded_deferral':
            raise RuntimeError('unexpected wait by immediate policy')
        remaining = decision.wait_until - now
        if remaining <= 1e-6:
            raise RuntimeError('non-progressing deferral decision')
        duration = min(cfg.telemetry_refresh_seconds, remaining)
        before = monotonic()
        sleep(duration)
        after = monotonic()
        if after <= before:
            raise RuntimeError('non-advancing timer while waiting')
        total_wait += after - before


def run_trial(client: Kubectl, policy_name: str, cfg: PilotConfig, *,
              block: int, order: int, monotonic: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Run <=1 Pod and attempt deletion of its exact name if creation attempted."""
    if policy_name not in POLICIES:
        raise ValueError('unknown policy')
    start = monotonic()
    workload = Workload(f'm6b-b{block}-{policy_name}', start, start + cfg.deadline_seconds,
                        cfg.cpu_m / 1000., float(cfg.sleep_seconds))
    row: dict[str, Any] = {
        'block': block, 'order': order, 'policy': policy_name,
        'deadline_seconds': cfg.deadline_seconds, 'terminal_phase': 'NotSubmitted',
        'on_time_observed': False, 'deferral_wait_seconds': 0.0,
        'telemetry_reads': 0, 'cleanup_attempted_and_accepted': False,
    }
    name: str | None = None
    create_attempted = False
    try:
        chosen = choose_node(client, policy_name, cfg, workload,
                             monotonic=monotonic, sleep=sleep)
        row['telemetry_reads'] = chosen['reads']
        row['deferral_wait_seconds'] = chosen['wait']
        row['decision_elapsed_seconds'] = monotonic() - start
        row['telemetry_age_at_dispatch_seconds'] = chosen['age']
        node = chosen['node']
        if node is None:
            row['terminal_phase'] = chosen['status']
            return row
        row['node_selected'] = node['name']
        if monotonic() >= workload.deadline:
            row['terminal_phase'] = 'DeadlineBeforeSubmit'
            return row
        name = f'epv2-pilot-{uuid4().hex[:12]}'
        row['pod_name'] = name
        manifest = pod_manifest(name, node['hostname_label'], cfg.cpu_m, cfg.sleep_seconds)
        create_attempted = True
        submit_start = monotonic()
        client.call('create', '-n', cfg.namespace, '-f', '-', stdin=json.dumps(manifest))
        row['api_submit_seconds'] = monotonic() - submit_start
        while True:
            try:
                pod = client.get_json('pod', name, '-n', cfg.namespace)
                obs = pod_observation(pod)
                row['terminal_phase'] = obs['phase']
                row['node_actual'] = obs['node_name']
                row['placement_mismatch'] = (bool(obs['node_name'])
                                             and obs['node_name'] != row['node_selected'])
                for key in ('container_exit_code',
                            'kubernetes_creation_to_container_start_seconds',
                            'kubernetes_container_execution_seconds'):
                    row[key] = obs[key]
            except KubectlError as exc:
                row['last_poll_error'] = str(exc)
            elapsed = monotonic() - start
            row['end_to_end_observed_seconds'] = elapsed
            if row['terminal_phase'] in ('Succeeded', 'Failed'):
                row['on_time_observed'] = (row['terminal_phase'] == 'Succeeded'
                                           and row.get('container_exit_code') == 0
                                           and not row.get('placement_mismatch', False)
                                           and elapsed <= cfg.deadline_seconds)
                break
            if elapsed >= cfg.deadline_seconds:
                row['terminal_phase'] = 'TimedOutObservation'
                break
            sleep(min(cfg.poll_interval_seconds, cfg.deadline_seconds - elapsed))
    except (KubectlError, ValueError, RuntimeError) as exc:
        row['error'] = str(exc)
        row['terminal_phase'] = 'ProbeError'
    finally:
        row.setdefault('end_to_end_observed_seconds', monotonic() - start)
        if create_attempted and name is not None:
            try:
                client.call('delete', 'pod', name, '-n', cfg.namespace,
                            '--ignore-not-found=true', '--wait=true', '--timeout=15s')
                row['cleanup_attempted_and_accepted'] = True
            except KubectlError as exc:
                row['cleanup_error'] = str(exc)
    return row


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Descriptive summaries only; these small sequential blocks imply no causal CI."""
    out = []
    for name in POLICIES:
        trials = [r for r in rows if r['policy'] == name]
        observed = [float(r['end_to_end_observed_seconds']) for r in trials
                    if r.get('terminal_phase') in ('Succeeded', 'Failed')
                    and r.get('end_to_end_observed_seconds') is not None]
        out.append({
            'policy': name, 'trials': len(trials),
            'on_time_rate': (sum(r['on_time_observed'] for r in trials) / len(trials)) if trials else None,
            'terminal_successes': sum(r.get('terminal_phase') == 'Succeeded' for r in trials),
            'p50_observed_terminal_seconds': statistics.median(observed) if observed else None,
            'failed_or_timed_out': sum(r.get('terminal_phase') != 'Succeeded' for r in trials),
        })
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: tuple[str, ...] | list[str]):
    with path.open('w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def run_pilot(client: Kubectl, cfg: PilotConfig, output: Path, *,
              monotonic: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    """Create sequential controlled Pods; each trial is recorded immediately."""
    # Fail closed if an unexpected workload already exists in the reserved namespace.
    client.get_json('namespace', cfg.namespace)
    existing = client.get_json('pods', '-n', cfg.namespace)
    if not isinstance(existing.get('items'), list):
        raise KubectlError('invalid namespace Pod inventory')
    if any((p.get('status') or {}).get('phase') not in ('Succeeded', 'Failed')
           for p in existing['items']):
        raise KubectlError('validation namespace has active Pods; refuse to interfere')
    output.mkdir(parents=True, exist_ok=True)
    plan = order_by_block(cfg.blocks, cfg.seed)
    manifest = {
        'study': 'EdgePilot v2 / sequential Kubernetes policy pilot',
        'measurement_class': 'real kind Pods, matched workload blocks; not simultaneous or causal',
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'config': asdict(cfg), 'plan': [{'block': b, 'order': o, 'policy': p}
                                      for b, o, p in plan],
        'limitations': [
            'kind workers share the host CPU/memory and are not independent physical edge devices',
            'Kubernetes default scheduler still binds the Pod with nodeSelector',
            'CPU requests are reservations, not measured CPU usage',
            'Sequential trials experience uncontrolled time-varying host conditions',
            'Different policy outcomes require actual differences in choices or waiting',
            'A few pilot blocks are insufficient for statistical or causal conclusions',
        ],
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    rows = []
    for block, order, policy in plan:
        # Catch interruption? KeyboardInterrupt allowed to propagate; already completed
        # trials are flushed to CSV and exact live pod has finally cleanup in run_trial.
        row = run_trial(client, policy, cfg, block=block, order=order,
                        monotonic=monotonic, sleep=sleep)
        rows.append(row)
        _write_csv(output / 'trials.csv', rows, FIELDS)
        (output / 'summary.json').write_text(json.dumps(summarize(rows), indent=2) + '\n',
                                              encoding='utf-8')
        print(f"Block {block}/{cfg.blocks} · {policy}: {row['terminal_phase']}; "
              f"on-time={row['on_time_observed']}; "
              f"observed={row.get('end_to_end_observed_seconds', 0):.2f}s")
        if row.get('cleanup_error'):
            raise KubectlError(f'cleanup not confirmed for {row.get("pod_name")}; aborting next trial')
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', default=CONTEXT)
    parser.add_argument('--namespace', default='edgepilot-v2-validation')
    parser.add_argument('--blocks', type=int, default=2)
    parser.add_argument('--seed', type=int, default=20261112)
    parser.add_argument('--deadline-seconds', type=float, default=45.)
    parser.add_argument('--cpu-m', type=int, default=50)
    parser.add_argument('--sleep-seconds', type=int, default=2)
    parser.add_argument('--max-wait-seconds', type=float, default=4.)
    parser.add_argument('--freshness-threshold-seconds', type=float, default=.25)
    parser.add_argument('--telemetry-refresh-seconds', type=float, default=.75)
    parser.add_argument('--placement-budget-seconds', type=float, default=20.)
    parser.add_argument('--poll-interval-seconds', type=float, default=.5)
    parser.add_argument('--output', type=Path, default=Path('research_outputs/kubernetes/policy_pilot'))
    parser.add_argument('--execute', action='store_true', help='Create up to 20 disposable Pods, sequentially')
    args = parser.parse_args(argv)
    cfg = PilotConfig(context=args.context, namespace=args.namespace,
                      blocks=args.blocks, seed=args.seed,
                      deadline_seconds=args.deadline_seconds, cpu_m=args.cpu_m,
                      sleep_seconds=args.sleep_seconds, max_wait_seconds=args.max_wait_seconds,
                      freshness_threshold_seconds=args.freshness_threshold_seconds,
                      telemetry_refresh_seconds=args.telemetry_refresh_seconds,
                      placement_budget_seconds=args.placement_budget_seconds,
                      poll_interval_seconds=args.poll_interval_seconds)
    plan = order_by_block(cfg.blocks, cfg.seed)
    print(f'Plan: {len(plan)} sequential Pods in context {cfg.context}, namespace {cfg.namespace}')
    print('Order: ' + ', '.join(f'B{b}:{p}' for b, _, p in plan))
    if not args.execute:
        print('DRY RUN. No cluster read or write. Add --execute to run the real pilot.')
        return
    client = Kubectl(cfg.context)
    client.verify_context()
    rows = run_pilot(client, cfg, args.output)
    print(f'Recorded {len(rows)} trials in {args.output}')
    for record in summarize(rows):
        print(f"  {record['policy']:19} on-time={record['on_time_rate']:.1%} "
              f"({record['trials']} trials)")
    print('EXPLORATORY ONLY: sequential Pods, small sample, no causal claims.')


if __name__ == '__main__':
    main()
