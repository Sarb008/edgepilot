"""Research simulator with event-driven wakeups and telemetry impairments.

Compatibility: uses the immutable v2 models and policies. No future telemetry is
visible; sampling snapshots actual CPU at measurement time.
"""
from __future__ import annotations
from dataclasses import dataclass
from itertools import count
import heapq
import math
from typing import Mapping

from .models import NodeState, TelemetrySample, Workload
from .policies import Decision, Policy
from .simulator import Result

EPS = 1e-9


@dataclass(frozen=True)
class Impairment:
    """Per-measurement delays and drops; keyed by (node_id, sample_index)."""
    delays: Mapping[tuple[str, int], float] | None = None
    drops: frozenset[tuple[str, int]] = frozenset()

    def __post_init__(self):
        for delay in (self.delays or {}).values():
            if not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 0:
                raise ValueError('delays must be finite and nonnegative')


class ResearchSimulator:
    """Single-use deterministic event engine; event priorities are documented below.

    At the same timestamp: completion (0), measurement (1), delivery (2),
    arrival (3), wake/retry (4). Each waiting job is woken once per delivered
    telemetry batch, not once per node; obsolete timeout events are ignored.
    """
    def __init__(self, nodes: list[NodeState], workloads: list[Workload], policy: Policy,
                 telemetry_interval: float = 1.0, telemetry_delay: float = 0.0,
                 impairment: Impairment | None = None, horizon: float | None = None):
        if not nodes or len({n.node_id for n in nodes}) != len(nodes):
            raise ValueError('nodes must be nonempty and unique')
        if len({w.workload_id for w in workloads}) != len(workloads):
            raise ValueError('workload IDs must be unique')
        for value, positive in ((telemetry_interval, True), (telemetry_delay, False)):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or (value <= 0 if positive else value < 0):
                raise ValueError('invalid telemetry timing')
        self.nodes = {n.node_id: n for n in nodes}
        self.workloads = {w.workload_id: w for w in workloads}
        self.policy = policy
        self.interval = telemetry_interval
        self.delay = telemetry_delay
        self.impairment = impairment or Impairment()
        self.horizon = max((w.deadline for w in workloads), default=0) if horizon is None else horizon
        if not isinstance(self.horizon, (int, float)) or not math.isfinite(self.horizon) or self.horizon < 0:
            raise ValueError('invalid horizon')
        self.actual = {n.node_id: n.cpu_used for n in nodes}
        self.observed: dict[str, TelemetrySample] = {}
        self.results: dict[str, Result] = {}
        self.trace: list[tuple[float, str, str]] = []
        self.events: list[tuple[float, int, int, str, object]] = []
        self.seq = count()
        self.pending: dict[str, tuple[float, int]] = {}
        self.generations: dict[str, int] = {}
        self.started = False
        self.cpu_area = {n.node_id: 0.0 for n in nodes}
        self._last_time = 0.0
        self.placement_failures = 0
        self.telemetry_deliveries = 0

    def _push(self, t: float, priority: int, kind: str, data: object):
        if t <= self.horizon + EPS:
            heapq.heappush(self.events, (t, priority, next(self.seq), kind, data))

    def _account(self, now: float):
        dt = now - self._last_time
        for nid, used in self.actual.items():
            self.cpu_area[nid] += used * dt
        self._last_time = now

    def _attempt(self, wid: str, now: float):
        if wid in self.results:
            return
        self.pending.pop(wid, None)
        w = self.workloads[wid]
        if now + w.duration > w.deadline + EPS:
            self.results[wid] = Result(wid, 'deadline_missed', None, None, None, w.deadline)
            return
        decision = self.policy.decide(w, now, dict(self.observed), now - w.arrival_time)
        if decision is None:
            self.results[wid] = Result(wid, 'rejected', None, None, None, w.deadline)
            return
        if decision.wait_until is not None:
            if not math.isfinite(decision.wait_until) or decision.wait_until <= now + EPS:
                raise ValueError('policy must wait until a finite future time')
            generation = self.generations.get(wid, 0) + 1
            self.generations[wid] = generation
            self.pending[wid] = (decision.wait_until, generation)
            self._push(decision.wait_until, 4, 'retry', (wid, generation))
            self.trace.append((now, 'defer', wid))
            return
        nid = decision.node_id
        if nid not in self.nodes or nid not in self.observed:
            raise ValueError('policy selected unknown or unobserved node')
        if self.actual[nid] + w.cpu_required > self.nodes[nid].cpu_capacity + EPS:
            self.placement_failures += 1
            self.results[wid] = Result(wid, 'capacity_failed', nid, None, None, w.deadline)
            self.trace.append((now, 'capacity_failed', wid))
            return
        self.actual[nid] += w.cpu_required
        self._push(now + w.duration, 0, 'complete', (wid, nid, w.cpu_required, now))
        self.trace.append((now, 'start', wid))

    def run(self) -> dict[str, Result]:
        if self.started:
            raise RuntimeError('simulator is single-use')
        self.started = True
        for w in self.workloads.values():
            self._push(w.arrival_time, 3, 'arrival', w.workload_id)
        i = 0
        while i * self.interval <= self.horizon + EPS:
            self._push(i * self.interval, 1, 'measure', i)
            i += 1
        while self.events:
            now, _, _, kind, data = heapq.heappop(self.events)
            self._account(now)
            if kind == 'complete':
                wid, nid, cpu, start = data
                self.actual[nid] -= cpu
                w = self.workloads[wid]
                self.results[wid] = Result(wid, 'completed' if now <= w.deadline + EPS else 'deadline_missed', nid, start, now, w.deadline)
                self.trace.append((now, 'complete', wid))
            elif kind == 'measure':
                for nid, node in self.nodes.items():
                    key = (nid, data)
                    if key in self.impairment.drops:
                        continue
                    delay = (self.impairment.delays or {}).get(key, self.delay)
                    sample = TelemetrySample(nid, now, now + delay, node.cpu_capacity, self.actual[nid])
                    self._push(sample.received_at, 2, 'deliver', sample)
            elif kind == 'deliver':
                sample = data
                previous = self.observed.get(sample.node_id)
                if previous is None or sample.measured_at >= previous.measured_at:
                    self.observed[sample.node_id] = sample
                    self.telemetry_deliveries += 1
                    # Wake only jobs waiting for telemetry; stale timeout events are invalidated.
                    for wid in sorted(self.pending):
                        self._push(now, 4, 'wake', (wid, self.pending[wid][1]))
            elif kind == 'arrival':
                self._attempt(data, now)
            elif kind in ('retry', 'wake'):
                wid, generation = data
                pending = self.pending.get(wid)
                if pending is not None and pending[1] == generation:
                    # Wake events may be duplicated by multiple deliveries at the same time.
                    self._attempt(wid, now)
        self._account(self.horizon)
        for w in self.workloads.values():
            if w.workload_id not in self.results:
                self.results[w.workload_id] = Result(w.workload_id, 'deadline_missed', None, None, None, w.deadline)
        return dict(self.results)

    def metrics(self) -> dict[str, float]:
        if not self.started:
            raise RuntimeError('run simulator first')
        results = list(self.results.values())
        total = len(results)
        return {
            'jobs': float(total),
            'on_time_rate': sum(r.on_time for r in results) / total if total else 0.0,
            'deadline_miss_rate': sum(r.status == 'deadline_missed' for r in results) / total if total else 0.0,
            'placement_failure_rate': self.placement_failures / total if total else 0.0,
            'rejection_rate': sum(r.status == 'rejected' for r in results) / total if total else 0.0,
            'mean_start_wait': (sum(r.start - self.workloads[r.workload_id].arrival_time for r in results if r.start is not None)
                                / sum(r.start is not None for r in results)) if any(r.start is not None for r in results) else 0.0,
            'cpu_utilization': (sum(self.cpu_area.values()) / (self.horizon * sum(n.cpu_capacity for n in self.nodes.values()))) if self.horizon > 0 else 0.0,
            'telemetry_deliveries': float(self.telemetry_deliveries),
        }
