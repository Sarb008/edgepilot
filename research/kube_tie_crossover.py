"""M6I: opt-in prospective, worker-balanced Kubernetes tie-break crossover.

Four arms: lexical greedy, reverse-lexical greedy (only for exact max-capacity
CPU-request ties), immediate age-penalized, and bounded deferral. Both greedy
arms consume the same *kind* of mixed-age input as the other arms. Worker
perturbation rotates independently from the deterministic per-block arm order.

Each trial creates at most ONE reservation Pod and ONE workload Pod, sequentially,
on a locked disposable kind cluster and namespace. A fresh pre-decision snapshot
is used to verify treatment and score retrospective feasibility, NEVER to choose
an arm's worker. Real completion/deadline outcomes come only from executed Pods.

This is a small controlled feasibility pilot, not a Kubernetes scheduler plugin,
causal identification, network-delay study, or CPU-utilization benchmark.
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

from .kube_feasibility_pilot import (
    FeasibilityConfig, high_request_probe_manifest, request_from_old_capacity,
    run_probe, verify_boundary,
)
from .kube_pilot import CONTEXT, policy_for
from .kube_stale_pilot import (
    NAMESPACE, assert_empty_namespace, inventory_by_name, mixed_samples,
    reservation_manifest, select_from_mixed, usable_workers, wait_for_reservation,
)
from .kube_validation import Kubectl, KubectlError, capture_snapshot, samples_from_inventory
from .models import TelemetrySample, Workload

ARMS = ('greedy_lex', 'greedy_reverse', 'age_penalized', 'bounded_deferral')
FIELDS = (
    'repeat', 'block', 'position', 'arm', 'perturbed_worker', 'reference_worker',
    'probe_cpu_m', 'greedy_exact_top_tie', 'greedy_tie_candidates',
    'initial_action', 'initial_selected_worker', 'selected_worker',
    'selected_feasible_in_treatment_snapshot', 'target_age_seconds',
    'peer_age_seconds', 'wait_seconds', 'refreshes', 'decision_elapsed_seconds',
    'deadline_seconds', 'terminal_phase', 'unschedulable_reason', 'pod_name',
    'pod_actual_worker', 'container_exit_code', 'end_to_end_seconds',
    'on_time_observed', 'probe_cleanup_confirmed',
    'competitor_cleanup_confirmed', 'error',
)


@dataclass(frozen=True)
class CrossoverConfig:
    context: str = CONTEXT
    namespace: str = NAMESPACE
    repeats: int = 1
    seed: int = 20261115
    deadline_seconds: float = 20.0
    competitor_cpu_m: int = 200
    margin_cpu_m: int = 100
    competitor_hold_seconds: int = 60
    sleep_seconds: int = 2
    poll_interval_seconds: float = 0.5
    min_stale_age_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.context != CONTEXT or self.namespace != NAMESPACE:
            raise ValueError('restricted to dedicated edgepilot-v2 kind context and namespace')
        if type(self.repeats) is not int or not 1 <= self.repeats <= 2:
            raise ValueError('repeats must be integer 1..2 (32-Pod maximum)')
        if type(self.seed) is not int:
            raise ValueError('seed must be integer')
        # Delegate all workload, deadline, reservation and polling safeguards.
        self.feasibility_config()

    def feasibility_config(self) -> FeasibilityConfig:
        return FeasibilityConfig(
            context=self.context, namespace=self.namespace, blocks=2,
            seed=self.seed, deadline_seconds=self.deadline_seconds,
            competitor_cpu_m=self.competitor_cpu_m,
            margin_cpu_m=self.margin_cpu_m,
            competitor_hold_seconds=self.competitor_hold_seconds,
            sleep_seconds=self.sleep_seconds,
            poll_interval_seconds=self.poll_interval_seconds,
            min_stale_age_seconds=self.min_stale_age_seconds,
        )


def plan_for(config: CrossoverConfig) -> list[tuple[int, int, int, str]]:
    """A deterministic 4x4 crossover: every arm occupies every slot once / 4 blocks.

    Blocks alternate the perturbed worker; arm position rotates across them.
    For repeats=1, each worker is perturbed once; for repeats=2, twice.
    """
    base = list(ARMS)
    random.Random(config.seed).shuffle(base)
    return [
        (block // 2 + 1, block + 1, slot + 1, base[(slot + block) % len(base)])
        for block in range(2 * config.repeats)
        for slot in range(len(base))
    ]


def greedy_tie_candidates(workload: Workload,
                          samples: dict[str, TelemetrySample]) -> list[str]:
    """Return every eligible worker with maximum REPORTED available CPU."""
    eligible = [sample for sample in samples.values()
                if sample.available_cpu + 1e-9 >= workload.cpu_required]
    if not eligible:
        return []
    maximum = max(sample.available_cpu for sample in eligible)
    return sorted(sample.node_id for sample in eligible
                  if math.isclose(sample.available_cpu, maximum, rel_tol=0, abs_tol=1e-9))


def greedy_tie_select(workload: Workload,
                      samples: dict[str, TelemetrySample], arm: str) -> str | None:
    """Two genuine greedy policies differing ONLY on the exact-max tie rule."""
    if arm not in ('greedy_lex', 'greedy_reverse'):
        raise ValueError('greedy arm must be greedy_lex or greedy_reverse')
    candidates = greedy_tie_candidates(workload, samples)
    if not candidates:
        return None
    return candidates[0] if arm == 'greedy_lex' else candidates[-1]


def _initial_action(arm: str, workload: Workload, now: float,
                    samples: dict[str, TelemetrySample], config: CrossoverConfig) -> tuple[str, str | None]:
    if arm.startswith('greedy_'):
        node = greedy_tie_select(workload, samples, arm)
        return ('select' if node else 'reject', node)
    decision = policy_for(arm, config.feasibility_config().stale_config().policy_config()).decide(
        workload, now, samples, now - workload.arrival_time,
    )
    if decision is None:
        return 'reject', None
    if decision.node_id is None:
        return 'wait', None
    return 'select', decision.node_id


def run_trial(client: Kubectl, arm: str, config: CrossoverConfig, *,
              repeat: int, block: int, position: int,
              monotonic: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> tuple[dict[str, Any], dict[str, Any]]:
    if (arm not in ARMS or type(repeat) is not int or type(block) is not int
            or type(position) is not int or not 1 <= repeat <= config.repeats
            or not 1 <= block <= 2 * config.repeats or not 1 <= position <= len(ARMS)
            or repeat != (block - 1) // 2 + 1):
        raise ValueError('invalid crossover trial plan')
    cfg = config.feasibility_config()
    row: dict[str, Any] = {
        'repeat': repeat, 'block': block, 'position': position, 'arm': arm,
        'deadline_seconds': config.deadline_seconds, 'terminal_phase': 'NotStarted',
        'on_time_observed': False, 'probe_cleanup_confirmed': False,
        'competitor_cleanup_confirmed': False,
    }
    trace: dict[str, Any] = {'repeat': repeat, 'block': block, 'position': position, 'arm': arm}
    competitor_name = f'epv2-cross-res-{uuid4().hex[:10]}'
    competitor_attempted = False
    try:
        assert_empty_namespace(client, config.namespace)
        old = capture_snapshot(client, monotonic=monotonic)
        workers = usable_workers(old)
        target = workers[(block - 1) % 2]
        peer = next(w for w in workers if w['name'] != target['name'])
        row['perturbed_worker'] = target['name']
        request_m = request_from_old_capacity(target, peer, cfg)
        row['probe_cpu_m'] = request_m
        trace['old_snapshot'] = old
        trace['competitor_pod'] = competitor_name
        competitor_attempted = True  # uncertain create outcome must still be cleaned
        client.call('create', '-n', config.namespace, '-f', '-', stdin=json.dumps(
            reservation_manifest(competitor_name, target['hostname_label'],
                                 config.competitor_cpu_m, config.competitor_hold_seconds)))
        wait_for_reservation(client, competitor_name, target['name'], timeout=20.,
                             monotonic=monotonic, sleep=sleep)
        fresh = capture_snapshot(client, monotonic=monotonic)
        trace['fresh_snapshot'] = fresh
        row.update(verify_boundary(old, fresh, target['name'], peer['name'], request_m, cfg))
        mixed = mixed_samples(old, fresh, target['name'])
        trace['mixed_policy_input'] = {name: asdict(sample) for name, sample in mixed.items()}
        before_aging = monotonic()
        shortfall = config.min_stale_age_seconds - mixed[target['name']].age_at(before_aging)
        if shortfall > 0:
            sleep(shortfall)
        wait_for_reservation(client, competitor_name, target['name'], timeout=1.,
                             monotonic=monotonic, sleep=sleep)
        now = monotonic()
        row['target_age_seconds'] = mixed[target['name']].age_at(now)
        row['peer_age_seconds'] = mixed[peer['name']].age_at(now)
        workload = Workload(f'm6i-b{block}-{arm}', now, now + config.deadline_seconds,
                            request_m / 1000., float(config.sleep_seconds))
        candidates = greedy_tie_candidates(workload, mixed)
        row['greedy_tie_candidates'] = ';'.join(candidates)
        row['greedy_exact_top_tie'] = len(candidates) == 2
        # A valid tie-controlled experiment needs a real, two-worker, *observed*
        # capacity tie. Abort rather than silently turn into an untied study.
        if len(candidates) != 2:
            raise KubectlError('reported max-CPU tie absent: cannot run tie-controlled trial')
        # The reference is evaluator-only and MUST NOT influence selection.
        fresh_samples = samples_from_inventory(fresh['node_inventory'],
                             fresh['measurement_window_start_monotonic'], fresh['receipt_monotonic'])
        reference = policy_for('greedy', cfg.stale_config().policy_config()).decide(
            workload, now, fresh_samples, 0.)
        row['reference_worker'] = reference.node_id if reference else None
        action, initial_worker = _initial_action(arm, workload, now, mixed, config)
        row['initial_action'] = action
        row['initial_selected_worker'] = initial_worker
        if arm.startswith('greedy_'):
            selected = greedy_tie_select(workload, mixed, arm)
            chosen = {'selected': selected, 'waited': 0., 'refreshes': 0}
        else:
            chosen = select_from_mixed(client, cfg.stale_config(), arm, workload, mixed,
                                       monotonic=monotonic, sleep=sleep)
        row['selected_worker'] = chosen['selected']
        row['wait_seconds'] = chosen['waited']
        row['refreshes'] = chosen['refreshes']
        row['decision_elapsed_seconds'] = monotonic() - workload.arrival_time
        if chosen['selected']:
            destination = inventory_by_name(fresh).get(chosen['selected'])
            if not destination or not destination['usable_for_model']:
                raise KubectlError('selected worker missing or ineligible in treatment snapshot')
            row['selected_feasible_in_treatment_snapshot'] = (
                destination['estimated_unreserved_cpu_cores'] * 1000 >= request_m - 1e-6)
            if monotonic() >= workload.deadline:
                row['terminal_phase'] = 'DeadlineBeforeSubmit'
            else:
                probe_name = f'epv2-cross-job-{uuid4().hex[:10]}'
                row['pod_name'] = probe_name
                row.update(run_probe(client, cfg, probe_name, destination['hostname_label'], request_m,
                                     workload.arrival_time, chosen['selected'],
                                     monotonic=monotonic, sleep=sleep))
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


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {'arm': arm, 'trials': len(subset),
         'on_time': sum(r.get('on_time_observed') is True for r in subset),
         'refusals': sum(r.get('terminal_phase') == 'NoEligibleNode' for r in subset),
         'observation_timeouts': sum(r.get('terminal_phase') == 'TimedOutObservation' for r in subset),
         'treatment_snapshot_infeasible': sum(
             r.get('selected_feasible_in_treatment_snapshot') is False for r in subset),
         'total_wait_seconds': round(sum(float(r.get('wait_seconds') or 0) for r in subset), 6)}
        for arm in ARMS for subset in [[r for r in rows if r['arm'] == arm]]
    ]


def run_experiment(client: Kubectl, config: CrossoverConfig, output: Path, *,
                   monotonic: Callable[[], float] = time.monotonic,
                   sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    client.verify_context()
    assert_empty_namespace(client, config.namespace)
    if output.exists():
        raise FileExistsError(f'refuse to overwrite existing evidence path: {output}')
    plan = plan_for(config)
    output.mkdir(parents=True)
    manifest = {
        'study': 'EdgePilot v2 M6I / prospective worker-balanced tie-break crossover',
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'config': asdict(config),
        'plan': [{'repeat': rep, 'block': block, 'position': pos, 'arm': arm,
                  'perturbed_worker_slot': (block - 1) % 2} for rep, block, pos, arm in plan],
        'limitations': [
            'Prospective but sequential, not simultaneous paired policy execution',
            'Each trial captures new old and fresh snapshots; policy arms do not literally share one snapshot',
            'The targeted worker rotates independently from arm type; tie ordering is explicit',
            'The experiment aborts rather than analyzing a non-tie as a tie-controlled case',
            'The fresh treatment snapshot is for retrospective audit only, never a policy input',
            'A failed observation deadline does not prove permanent workload admission failure',
            'Only one high-CPU-request sleeping Pod per workload; no actual CPU-load saturation',
            'Two kind workers are containers on the same host; no independent physical edge machines',
            'Artificial telemetry withholding is not naturally observed network delay',
            'Pod creation uses default kube-scheduler with nodeSelector, not an installed custom scheduler',
            'Order rotation cannot eliminate time drift or establish generalizable causal effects',
            'A rejection or wait must not be scored as a completed Pod',
        ],
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    rows: list[dict[str, Any]] = []
    for rep, block, pos, arm in plan:
        row, trace = run_trial(client, arm, config, repeat=rep, block=block, position=pos,
                               monotonic=monotonic, sleep=sleep)
        rows.append(row)
        trace_dir = output / 'traces'
        trace_dir.mkdir(exist_ok=True)
        (trace_dir / f'block{block:02d}_position{pos:02d}_{arm}.json').write_text(
            json.dumps(trace, indent=2) + '\n', encoding='utf-8')
        with (output / 'trials.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
        (output / 'summary.json').write_text(json.dumps(summarize(rows), indent=2) + '\n',
                                             encoding='utf-8')
        print(f'Block {block}/{2 * config.repeats} · {arm}: '
              f'{row["terminal_phase"]}; worker={row.get("selected_worker")}; '
              f'treatment-feasible={row.get("selected_feasible_in_treatment_snapshot")}; '
              f'on-time={row["on_time_observed"]}')
        if (row.get('error') or row.get('probe_cleanup_error') or row.get('competitor_cleanup_error')
                or not row.get('competitor_cleanup_confirmed')
                or (row.get('pod_name') and not row.get('probe_cleanup_confirmed'))):
            raise KubectlError('trial or cleanup failed; STOP and inspect isolated namespace')
        assert_empty_namespace(client, config.namespace)
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', default=CONTEXT)
    parser.add_argument('--namespace', default=NAMESPACE)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--seed', type=int, default=20261115)
    parser.add_argument('--deadline-seconds', type=float, default=20.)
    parser.add_argument('--competitor-cpu-m', type=int, default=200)
    parser.add_argument('--margin-cpu-m', type=int, default=100)
    parser.add_argument('--execute', action='store_true', help='Opt in to a maximum of 32 disposable Pods')
    parser.add_argument('--output', type=Path,
                        default=Path('research_outputs/kubernetes/tie_crossover_01'))
    args = parser.parse_args(argv)
    cfg = CrossoverConfig(context=args.context, namespace=args.namespace,
                          repeats=args.repeats, seed=args.seed,
                          deadline_seconds=args.deadline_seconds,
                          competitor_cpu_m=args.competitor_cpu_m,
                          margin_cpu_m=args.margin_cpu_m)
    plan = plan_for(cfg)
    print(f'Plan: {len(plan)} sequential trials across {2*cfg.repeats} blocks; '
          f'at most {len(plan)*2} disposable Pods')
    for rep, block, pos, arm in plan:
        print(f'  R{rep} B{block} #{pos} {arm}; perturb worker slot {(block-1)%2}')
    if not args.execute:
        print('DRY RUN. No cluster reads, Pods or output files. Add --execute to opt in.')
        return
    client = Kubectl(cfg.context)
    rows = run_experiment(client, cfg, args.output)
    print(f'Completed {len(rows)} Kubernetes trials in {args.output}')
    for row in summarize(rows):
        print(f'  {row["arm"]:19} on-time={row["on_time"]}/{row["trials"]}; '
              f'fresh-infeasible={row["treatment_snapshot_infeasible"]}; '
              f'waiting={row["total_wait_seconds"]:.2f}s')
    print('CONTROLLED LOCAL PILOT ONLY: observations are sequential and not causal estimates.')


if __name__ == '__main__':
    main()
