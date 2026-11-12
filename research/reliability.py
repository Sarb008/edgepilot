"""Optional reliability stress model for EdgePilot v2.

All outages and competing background demand are *exogenous* and fixed before a
policy run. A policy sees only delivered telemetry, never these future events.

CPU requests are reservations; an outage or a background-demand increase
preempts running jobs as necessary. Preempted jobs restart from scratch (no
checkpointing), with explicit restart limits. This is a synthetic model, not a
Kubernetes performance measurement.
"""
from __future__ import annotations

from dataclasses import dataclass
import heapq
from math import isfinite

from .advanced import EPS, ResearchSimulator, Impairment
from .models import NodeState, TelemetrySample, Workload
from .policies import Policy
from .simulator import Result


def _finite(name: str, value: float, *, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f'{name} must be finite')
    if value <= 0 if positive else value < 0:
        raise ValueError(f'{name} must be {"positive" if positive else "nonnegative"}')


@dataclass(frozen=True, slots=True)
class NodeOutage:
    node_id: str
    start: float
    end: float

    def __post_init__(self):
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise ValueError('invalid node ID')
        _finite('outage start', self.start)
        _finite('outage end', self.end)
        if self.end <= self.start:
            raise ValueError('outage must have positive duration')


@dataclass(frozen=True, slots=True)
class BackgroundLoad:
    """Set reserved, non-EdgePilot CPU on a node at a specified time."""
    node_id: str
    time: float
    cpu_used: float

    def __post_init__(self):
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise ValueError('invalid node ID')
        _finite('load time', self.time)
        _finite('background CPU', self.cpu_used)


@dataclass(frozen=True, slots=True)
class ReliabilityConfig:
    dispatch_delay: float = 0.0
    restart_backoff: float = 0.5
    retry_backoff: float = 0.5
    max_restarts: int = 1
    max_placement_retries: int = 0
    outages: tuple[NodeOutage, ...] = ()
    background: tuple[BackgroundLoad, ...] = ()

    def __post_init__(self):
        _finite('dispatch_delay', self.dispatch_delay)
        _finite('restart_backoff', self.restart_backoff, positive=True)
        _finite('retry_backoff', self.retry_backoff, positive=True)
        for key in ('max_restarts', 'max_placement_retries'):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f'{key} must be a nonnegative integer')
        if any(not isinstance(e, NodeOutage) for e in self.outages):
            raise ValueError('outages must contain NodeOutage objects')
        if any(not isinstance(e, BackgroundLoad) for e in self.background):
            raise ValueError('background must contain BackgroundLoad objects')
        by_node: dict[str, list[NodeOutage]] = {}
        for outage in self.outages:
            by_node.setdefault(outage.node_id, []).append(outage)
        for outages in by_node.values():
            ordered = sorted(outages, key=lambda o: o.start)
            if any(a.end >= b.start for a, b in zip(ordered, ordered[1:])):
                raise ValueError('outages on a node may not overlap')
        if len({(e.node_id, e.time) for e in self.background}) != len(self.background):
            raise ValueError('duplicate background-load change')


class ReliabilitySimulator(ResearchSimulator):
    """Drop-in stress extension with zero-fault parity to ResearchSimulator.

    Event precedence at equal timestamps:
      0 completion, 1 outage start, 2 background change, 3 outage end,
      4 measurement, 5 delivery, 6 arrival, 7 timeout/wake/restart, 8 dispatch.

    Jobs are not secretly moved or retried: retries are explicit and bounded.
    A dispatch checks *actual* availability when it executes, not at decision time.
    """
    def __init__(self, nodes: list[NodeState], workloads: list[Workload], policy: Policy,
                 telemetry_interval: float = 1.0, telemetry_delay: float = 0.0,
                 impairment: Impairment | None = None, horizon: float | None = None,
                 reliability: ReliabilityConfig | None = None):
        super().__init__(nodes, workloads, policy, telemetry_interval,
                         telemetry_delay, impairment, horizon)
        self.reliability = reliability or ReliabilityConfig()
        cfg = self.reliability
        for event in (*cfg.outages, *cfg.background):
            if event.node_id not in self.nodes:
                raise ValueError('fault references unknown node')
            if isinstance(event, BackgroundLoad) and event.cpu_used > self.nodes[event.node_id].cpu_capacity:
                raise ValueError('background load exceeds node capacity')
        self.background = {nid: n.cpu_used for nid, n in self.nodes.items()}
        self.offline: set[str] = set()
        # running[job] = (node, CPU reservation, start time, generation)
        self.running: dict[str, tuple[str, float, float, int]] = {}
        self.generation: dict[str, int] = {}
        self.restart_attempts: dict[str, int] = {}
        self.failed_dispatches: dict[str, int] = {}
        self.preempted: set[str] = set()
        self.recovery_waiting: dict[str, set[str]] = {nid: set() for nid in self.nodes}
        self.failed_outage_jobs = 0
        self.failed_interference_jobs = 0
        self.outage_preemptions = 0
        self.interference_preemptions = 0
        self.placement_retries = 0
        self.restarts = 0
        self.down_area = {nid: 0.0 for nid in self.nodes}
        self.job_cpu_area = {nid: 0.0 for nid in self.nodes}
        self.background_cpu_area = {nid: 0.0 for nid in self.nodes}

    def _account(self, now: float):
        dt = now - self._last_time
        if dt < -EPS:
            raise RuntimeError('events processed out of order')
        for nid in self.nodes:
            if nid in self.offline:
                self.down_area[nid] += dt
            else:
                self.cpu_area[nid] += self.actual[nid] * dt
                self.background_cpu_area[nid] += self.background[nid] * dt
                self.job_cpu_area[nid] += (self.actual[nid] - self.background[nid]) * dt
        self._last_time = now

    def _attempt(self, wid: str, now: float):
        if wid in self.results or wid in self.running:
            return
        self.pending.pop(wid, None)
        w = self.workloads[wid]
        if now + self.reliability.dispatch_delay + w.duration > w.deadline + EPS:
            self.results[wid] = Result(wid, 'deadline_missed', None, None, None, w.deadline)
            return
        decision = self.policy.decide(w, now, dict(self.observed), now - w.arrival_time)
        if decision is None:
            self.results[wid] = Result(wid, 'rejected', None, None, None, w.deadline)
            return
        if decision.wait_until is not None:
            if not isfinite(decision.wait_until) or decision.wait_until <= now + EPS:
                raise ValueError('policy must defer to finite future time')
            generation = self.generations.get(wid, 0) + 1
            self.generations[wid] = generation
            self.pending[wid] = (decision.wait_until, generation)
            self._push(decision.wait_until, 7, 'retry', (wid, generation))
            self.trace.append((now, 'defer', wid))
            return
        nid = decision.node_id
        if nid not in self.nodes or nid not in self.observed:
            raise ValueError('policy selected unknown/unobserved node')
        if self.reliability.dispatch_delay:
            self._push(now + self.reliability.dispatch_delay, 8, 'dispatch', (wid, nid))
            self.trace.append((now, 'dispatch_requested', wid))
        else:
            self._dispatch(wid, nid, now)

    def _dispatch(self, wid: str, nid: str, now: float):
        if wid in self.results or wid in self.running:
            return
        w = self.workloads[wid]
        if now + w.duration > w.deadline + EPS:
            self.results[wid] = Result(wid, 'deadline_missed', None, None, None, w.deadline)
            return
        if nid in self.offline or self.actual[nid] + w.cpu_required > self.nodes[nid].cpu_capacity + EPS:
            self.placement_failures += 1
            self.trace.append((now, 'placement_failed', wid))
            failures = self.failed_dispatches.get(wid, 0) + 1
            self.failed_dispatches[wid] = failures
            next_time = now + self.reliability.retry_backoff
            if failures <= self.reliability.max_placement_retries and (
                next_time + self.reliability.dispatch_delay + w.duration <= w.deadline + EPS
            ):
                self.placement_retries += 1
                self._push(next_time, 7, 'placement_retry', wid)
                return
            self.results[wid] = Result(wid, 'capacity_failed', nid, None, None, w.deadline)
            return
        self.actual[nid] += w.cpu_required
        generation = self.generation.get(wid, 0) + 1
        self.generation[wid] = generation
        self.running[wid] = (nid, w.cpu_required, now, generation)
        self._push(now + w.duration, 0, 'complete', (wid, generation))
        self.trace.append((now, 'start', wid))

    def _preempt(self, wid: str, now: float, cause: str):
        nid, cpu, _, _ = self.running.pop(wid)
        self.actual[nid] -= cpu
        self.preempted.add(wid)
        if cause == 'outage':
            self.outage_preemptions += 1
        else:
            self.interference_preemptions += 1
        self.trace.append((now, f'preempt_{cause}', wid))
        tries = self.restart_attempts.get(wid, 0) + 1
        self.restart_attempts[wid] = tries
        w = self.workloads[wid]
        if tries > self.reliability.max_restarts:
            self.results[wid] = Result(wid, f'failed_{cause}', nid, None, None, w.deadline)
            if cause == 'outage':
                self.failed_outage_jobs += 1
            else:
                self.failed_interference_jobs += 1
            return
        if cause == 'outage':
            # Do not inspect the future outage end time: wait for a recovery EVENT.
            self.recovery_waiting[nid].add(wid)
            self.trace.append((now, 'await_recovery', wid))
        else:
            self._queue_restart(wid, now)

    def _queue_restart(self, wid: str, now: float):
        w = self.workloads[wid]
        retry_at = now + self.reliability.restart_backoff
        if retry_at + self.reliability.dispatch_delay + w.duration > w.deadline + EPS:
            self.results[wid] = Result(wid, 'deadline_missed', None, None, None, w.deadline)
            return
        self.restarts += 1
        self._push(retry_at, 7, 'restart', wid)

    def _begin_outage(self, event: NodeOutage, now: float):
        nid = event.node_id
        self.offline.add(nid)
        for wid in sorted(w for w, (n, _, _, _) in self.running.items() if n == nid):
            self._preempt(wid, now, 'outage')
        self.trace.append((now, 'outage_start', nid))

    def _background_change(self, event: BackgroundLoad, now: float):
        nid = event.node_id
        previous = self.background[nid]
        self.background[nid] = event.cpu_used
        self.actual[nid] += event.cpu_used - previous
        # An exogenous demand increase may force full-workload restart.
        # Deterministic victim selection: most recently started first, then ID.
        while self.actual[nid] > self.nodes[nid].cpu_capacity + EPS:
            candidates = [(start, wid) for wid, (n, _, start, _) in self.running.items() if n == nid]
            if not candidates:
                raise RuntimeError('background resource invariant broken')
            _, wid = max(candidates)
            self._preempt(wid, now, 'interference')
        self.trace.append((now, 'background_change', nid))

    def run(self) -> dict[str, Result]:
        if self.started:
            raise RuntimeError('simulator is single-use')
        self.started = True
        for w in self.workloads.values():
            self._push(w.arrival_time, 6, 'arrival', w.workload_id)
        for event in self.reliability.outages:
            self._push(event.start, 1, 'outage_start', event)
            self._push(event.end, 3, 'outage_end', event)
        for event in self.reliability.background:
            self._push(event.time, 2, 'background', event)
        sample_index = 0
        while sample_index * self.interval <= self.horizon + EPS:
            self._push(sample_index * self.interval, 4, 'measure', sample_index)
            sample_index += 1
        while self.events:
            now, _, _, kind, data = heapq.heappop(self.events)
            self._account(now)
            if kind == 'complete':
                wid, generation = data
                current = self.running.get(wid)
                if current is None or current[3] != generation:
                    continue  # preempted completion: never release CPU twice
                nid, cpu, start, _ = self.running.pop(wid)
                self.actual[nid] -= cpu
                w = self.workloads[wid]
                status = 'completed' if now <= w.deadline + EPS else 'deadline_missed'
                self.results[wid] = Result(wid, status, nid, start, now, w.deadline)
                self.trace.append((now, 'complete', wid))
            elif kind == 'outage_start':
                self._begin_outage(data, now)
            elif kind == 'background':
                self._background_change(data, now)
            elif kind == 'outage_end':
                self.offline.remove(data.node_id)
                self.trace.append((now, 'outage_end', data.node_id))
                for wid in sorted(self.recovery_waiting[data.node_id]):
                    self._queue_restart(wid, now)
                self.recovery_waiting[data.node_id].clear()
            elif kind == 'measure':
                for nid, node in self.nodes.items():
                    key = (nid, data)
                    if key in self.impairment.drops:
                        continue
                    delay = (self.impairment.delays or {}).get(key, self.delay)
                    sample = TelemetrySample(nid, now, now + delay, node.cpu_capacity, self.actual[nid])
                    self._push(sample.received_at, 5, 'deliver', sample)
            elif kind == 'deliver':
                sample = data
                previous = self.observed.get(sample.node_id)
                if previous is None or sample.measured_at >= previous.measured_at:
                    self.observed[sample.node_id] = sample
                    self.telemetry_deliveries += 1
                    for wid in sorted(self.pending):
                        self._push(now, 7, 'wake', (wid, self.pending[wid][1]))
            elif kind == 'arrival':
                self._attempt(data, now)
            elif kind in ('retry', 'wake'):
                wid, generation = data
                pending = self.pending.get(wid)
                if pending is not None and pending[1] == generation:
                    self._attempt(wid, now)
            elif kind in ('restart', 'placement_retry'):
                self._attempt(data, now)
            elif kind == 'dispatch':
                wid, nid = data
                self._dispatch(wid, nid, now)
        self._account(self.horizon)
        for w in self.workloads.values():
            if w.workload_id not in self.results:
                self.results[w.workload_id] = Result(w.workload_id, 'deadline_missed', None, None, None, w.deadline)
        return dict(self.results)

    def metrics(self) -> dict[str, float]:
        baseline = super().metrics()
        total = len(self.results)
        capacity_time = self.horizon * sum(n.cpu_capacity for n in self.nodes.values())
        completed = [r for r in self.results.values() if r.on_time]
        baseline.update({
            'preemption_rate': len(self.preempted) / total if total else 0.0,
            'not_on_time_rate': 1.0 - baseline['on_time_rate'] if total else 0.0,
            'outage_preemptions': float(self.outage_preemptions),
            'interference_preemptions': float(self.interference_preemptions),
            'restart_count': float(self.restarts),
            'recovered_jobs': float(sum(r.workload_id in self.preempted for r in completed)),
            'placement_retries': float(self.placement_retries),
            'outage_job_failure_rate': self.failed_outage_jobs / total if total else 0.0,
            'interference_job_failure_rate': self.failed_interference_jobs / total if total else 0.0,
            'mean_response_time': sum(r.finish - self.workloads[r.workload_id].arrival_time for r in completed) / len(completed)
            if completed else 0.0,
            'node_downtime_fraction': sum(self.down_area.values()) / (self.horizon * len(self.nodes)) if self.horizon else 0.0,
            'job_cpu_utilization': sum(self.job_cpu_area.values()) / capacity_time if capacity_time else 0.0,
            'background_cpu_utilization': sum(self.background_cpu_area.values()) / capacity_time if capacity_time else 0.0,
        })
        return baseline
