"""Deadline-limited synthetic value-of-information experiment (November freeze plan).

A deliberately SMALL, predeclared mechanism study. Its exogenous conditions are
created before policy selection. An identical event stream is replayed for
immediate age-penalized scheduling and bounded deferral. This is **simulation**,
not independent real-cluster evidence and NOT an empirical effect estimate.

The experiment's primary outcome is on-time completion. Its accompanying
placement-failure and waiting metrics explain *why* an outcome changed.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
from typing import Any

from .advanced import Impairment
from .baselines import AgePenalized
from .models import NodeState, TelemetrySample, Workload
from .policies import BoundedDeferral, Decision
from .reliability import BackgroundLoad, ReliabilityConfig, ReliabilitySimulator

POLICIES = ('age_penalized', 'bounded_deferral')
_DELAYS = (0.25, 0.5, 1.0, 2.0)
_DEADLINES = (4.0, 5.0, 6.0)
_WORKERS = ('worker-a', 'worker-b')


@dataclass(frozen=True, slots=True)
class MatchedCase:
    """All timing is on a simulated monotonic seconds axis, not real wall time."""
    perturbed_worker: str
    change: bool
    telemetry_delay: float
    deadline: float
    arrival: float = 1.2
    workload_duration: float = 2.0
    dispatch_delay: float = 0.2
    cpu_required: float = 1.0
    reservation_cpu: float = 1.3
    reservation_time: float = 1.0
    max_wait: float = 3.0
    freshness_threshold: float = 1.0

    def __post_init__(self) -> None:
        if self.perturbed_worker not in _WORKERS:
            raise ValueError('perturbed worker must be one of two fixed workers')
        if type(self.change) is not bool:
            raise ValueError('change must be bool')
        for key in ('telemetry_delay', 'deadline', 'arrival', 'workload_duration',
                    'dispatch_delay', 'cpu_required', 'reservation_cpu',
                    'reservation_time', 'max_wait', 'freshness_threshold'):
            v = getattr(self, key)
            if isinstance(v, bool) or not isinstance(v, (float, int)) or not isfinite(v) or v < 0:
                raise ValueError(f'{key} must be finite and nonnegative')
        if not 0 < self.workload_duration or not 0 < self.cpu_required <= 2:
            raise ValueError('invalid workload requirements')
        if not 0 < self.reservation_cpu <= 2:
            raise ValueError('invalid background reservation')
        if self.deadline <= self.arrival:
            raise ValueError('deadline must exceed arrival')
        if self.reservation_time <= 0 or self.reservation_time >= self.arrival:
            raise ValueError('capacity perturbation must occur after t=0 before arrival')
        if self.telemetry_delay <= 0:
            raise ValueError('new measurements must arrive after their measurement time')

    @property
    def case_id(self) -> str:
        delay = str(self.telemetry_delay).replace('.', 'p')
        return (f'{self.perturbed_worker}-{int(self.change)}-'
                f'd{delay}-deadline{str(self.deadline).replace(".", "p")}')


def predeclared_cases() -> tuple[MatchedCase, ...]:
    """48 unique pairs; covers both workers and absence/presence of change."""
    return tuple(MatchedCase(worker, changed, delay, deadline)
                 for worker in _WORKERS
                 for changed in (False, True)
                 for delay in _DELAYS
                 for deadline in _DEADLINES)


class RecordingPolicy:
    """Record only policy-visible snapshots and decisions, never actual state."""
    def __init__(self, underlying: Any):
        self.underlying = underlying
        self.decisions: list[dict[str, Any]] = []

    def decide(self, workload: Workload, now: float,
               samples: dict[str, TelemetrySample], waited: float) -> Decision | None:
        if any(sample.received_at > now + 1e-8 or sample.measured_at > now + 1e-8
               for sample in samples.values()):
            raise AssertionError('future telemetry reached policy')
        snapshot = {name: asdict(sample) for name, sample in sorted(samples.items())}
        decision = self.underlying.decide(workload, now, samples, waited)
        self.decisions.append({
            'now': now, 'waited': waited, 'samples': snapshot,
            'action': ('reject' if decision is None else
                       'wait' if decision.wait_until is not None else 'select'),
            'worker': decision.node_id if decision else None,
            'wait_until': decision.wait_until if decision else None,
        })
        return decision


def run_pair(case: MatchedCase) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Two policy runs with the same initial input AND identical exogenous events."""
    nodes = [NodeState(name, 0.0, 2.0, 0.0) for name in _WORKERS]
    workload = Workload('job', case.arrival, case.deadline,
                        case.cpu_required, case.workload_duration)
    background = ((BackgroundLoad(case.perturbed_worker, case.reservation_time,
                                  case.reservation_cpu),) if case.change else ())
    config = ReliabilityConfig(dispatch_delay=case.dispatch_delay, background=background,
                               max_restarts=0, max_placement_retries=0)
    # t=0 is delivered promptly; all subsequent measurements arrive later.
    # This schedule is FIXED prior to choosing either policy.
    max_index = int(case.deadline) + 2
    delays = {(worker, 0): 0.0 for worker in _WORKERS}
    delays.update({(worker, i): case.telemetry_delay
                   for worker in _WORKERS for i in range(1, max_index + 1)})
    impairment = Impairment(delays=delays)
    output: list[dict[str, Any]] = []
    traces: dict[str, Any] = {}
    first_inputs: list[str] = []
    for label in POLICIES:
        policy = (AgePenalized() if label == 'age_penalized'
                  else BoundedDeferral(case.max_wait, case.freshness_threshold,
                                       placement_latency=case.dispatch_delay))
        recording = RecordingPolicy(policy)
        sim = ReliabilitySimulator(nodes, [workload], recording,
                                   telemetry_interval=1.0, impairment=impairment,
                                   reliability=config)
        result = sim.run()['job']
        if not recording.decisions:
            raise AssertionError('policy was never invoked; invalid case')
        first = recording.decisions[0]
        first_input = json.dumps({'at': first['now'], 'samples': first['samples']},
                                 sort_keys=True, separators=(',', ':'))
        first_inputs.append(first_input)
        # Intentional deferral is time until next policy invocation or wakeup,
        # not the entire start delay (which includes dispatch latency).
        intentional_wait = 0.0
        for before, after in zip(recording.decisions, recording.decisions[1:]):
            if before['action'] == 'wait':
                intentional_wait += after['now'] - before['now']
        if recording.decisions[-1]['action'] == 'wait':
            # The deadline can terminate a pending job with no further callback.
            intentional_wait += min(recording.decisions[-1]['wait_until'], case.deadline) - recording.decisions[-1]['now']
        row = {
            'case_id': case.case_id, 'perturbed_worker': case.perturbed_worker,
            'capacity_changed': int(case.change), 'telemetry_delay': case.telemetry_delay,
            'deadline': case.deadline, 'policy': label,
            'input_sha256': sha256(first_input.encode()).hexdigest(),
            'initial_action': first['action'], 'initial_worker': first['worker'] or '',
            'initial_telemetry_age': case.arrival,
            'selected_worker': result.node_id or '', 'status': result.status,
            'on_time': int(result.on_time), 'start': result.start, 'finish': result.finish,
            'time_to_completion': (result.finish - case.arrival if result.finish is not None else None),
            'intentional_wait': round(intentional_wait, 9),
            'placement_failures': sim.placement_failures,
            'dispatch_delay': case.dispatch_delay,
        }
        output.append(row)
        traces[label] = {'decisions': recording.decisions, 'events': sim.trace,
                         'result': asdict(result)}
    if first_inputs[0] != first_inputs[1]:
        raise AssertionError('policy arms did not receive the same first observation')
    trace = {'case': asdict(case), 'initial_input_sha256': output[0]['input_sha256'],
             'exogenous_background': [asdict(e) for e in background],
             'sample_delays': {f'{worker}:{index}': delay for (worker, index), delay in sorted(delays.items())},
             'arms': traces}
    return output, trace


def paired_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_case: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        label, case_id = row['policy'], row['case_id']
        if label not in POLICIES or label in by_case.setdefault(case_id, {}):
            raise ValueError('invalid or duplicate case/policy row')
        by_case[case_id][label] = row
    paired = []
    for case_id, arms in sorted(by_case.items()):
        if set(arms) != set(POLICIES):
            raise ValueError('missing paired arm')
        a, b = (arms[label] for label in POLICIES)
        if a['input_sha256'] != b['input_sha256']:
            raise ValueError('policy input mismatch')
        paired.append({
            'case_id': case_id, 'perturbed_worker': a['perturbed_worker'],
            'capacity_changed': a['capacity_changed'], 'telemetry_delay': a['telemetry_delay'],
            'deadline': a['deadline'],
            'age_on_time': a['on_time'], 'bounded_on_time': b['on_time'],
            'on_time_delta': b['on_time'] - a['on_time'],
            'age_wait': a['intentional_wait'], 'bounded_wait': b['intentional_wait'],
            'extra_wait': b['intentional_wait'] - a['intentional_wait'],
            'age_placement_failures': a['placement_failures'],
            'bounded_placement_failures': b['placement_failures'],
            'common_initial_input_sha256': a['input_sha256'],
        })
    return paired


def summary(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    def describe(group: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(group)
        return {'cases': n,
                'age_on_time': sum(p['age_on_time'] for p in group),
                'bounded_on_time': sum(p['bounded_on_time'] for p in group),
                'bounded_better_count': sum(p['on_time_delta'] > 0 for p in group),
                'bounded_worse_count': sum(p['on_time_delta'] < 0 for p in group),
                'tied_count': sum(p['on_time_delta'] == 0 for p in group),
                'mean_extra_wait': round(sum(p['extra_wait'] for p in group) / n, 6) if n else None}
    return {'all': describe(pairs),
            'by_change': {str(value): describe([p for p in pairs if p['capacity_changed'] == value])
                          for value in (0, 1)},
            'by_perturbed_worker': {worker: describe([p for p in pairs if p['perturbed_worker'] == worker])
                                     for worker in _WORKERS},
            'limitations': [
                'Deterministic synthetic mechanism matrix: conditions are NOT independent real-world trials.',
                'No confidence intervals or claims of external statistical significance are derived.',
                'The scheduler only sees telemetry received at its decision time.',
                'The simulator uses CPU reservations and predeclared external load; it does not measure actual CPU usage.',
                'Both arms receive exactly the same initial observation and exogenous event schedule.',
                'A policy that rejects or fails placement is scored as not on time.',
                'Comparisons on this matrix alone do not establish superiority on Kubernetes.',
            ]}


def _csv(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        raise ValueError('cannot export empty dataset')
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)


def export_study(output: Path, cases: tuple[MatchedCase, ...] | None = None) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f'output path exists; refusing to overwrite: {output}')
    selected = cases if cases is not None else predeclared_cases()
    if not selected or len(set(c.case_id for c in selected)) != len(selected):
        raise ValueError('cases must be nonempty and unique')
    rows: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    for case in selected:
        paired, trace = run_pair(case)
        rows.extend(paired)
        traces.append(trace)
    pairs = paired_rows(rows)
    report = summary(pairs)
    output.mkdir(parents=True, exist_ok=False)
    _csv(output / 'trials.csv', rows)
    _csv(output / 'paired.csv', pairs)
    (output / 'summary.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    manifest = {'study': 'EdgePilot v2 / deadline-aware value of telemetry information',
                'design': 'fixed complete factorial (2 workers × 2 change states × 4 delays × 3 deadlines)',
                'case_count': len(selected), 'policy_runs': len(rows),
                'policies': list(POLICIES), 'cases': [asdict(case) for case in selected],
                'hypotheses': [
                    'An informative refresh before the deadline may prevent a stale-capacity placement.',
                    'Waiting without useful capacity change imposes latency without admission benefit.',
                    'A late refresh can eliminate the value of bounded waiting.',
                    'Symmetric worker perturbation reveals fixed lexicographic-selection bias.',
                ], 'limitations': report['limitations']}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    with (output / 'traces.jsonl').open('w', encoding='utf-8') as f:
        for trace in traces:
            f.write(json.dumps(trace, sort_keys=True) + '\n')
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('research_outputs/voi_study_01'))
    args = parser.parse_args(argv)
    cases = predeclared_cases()
    print(f'Predeclared: {len(cases)} matched cases, {len(cases) * len(POLICIES)} deterministic policy runs.')
    report = export_study(args.output, cases)
    print(f'Wrote {args.output}: trials.csv, paired.csv, manifest.json, summary.json, traces.jsonl')
    for label, entry in [('all', report['all'])] + [(f'changed={key}', value) for key, value in report['by_change'].items()]:
        print(f'  {label:12} n={entry["cases"]:2} age={entry["age_on_time"]:2} '
              f'bounded={entry["bounded_on_time"]:2} '
              f'bounded better/worse/tie={entry["bounded_better_count"]}/'
              f'{entry["bounded_worse_count"]}/{entry["tied_count"]} '
              f'extra_wait={entry["mean_extra_wait"]:.3f}s')
    print('SIMULATION ONLY. No inferential p-values, confidence intervals or real-cluster performance claims.')


if __name__ == '__main__':
    main()
