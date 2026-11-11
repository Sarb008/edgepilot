"""Deterministic, event-driven scheduling with separated actual and observed state.

Event order for identical timestamps: completion, telemetry delivery, arrival, retry.
Telemetry snapshots represent actual state at *measurement time*, not delivery time.
A placement fails immediately when real capacity is insufficient; no hidden retry.
"""
from dataclasses import dataclass
import heapq
from itertools import count
from .models import NodeState, TelemetrySample, Workload
from .policies import Policy


@dataclass(frozen=True)
class Result:
    workload_id: str
    status: str  # completed, deadline_missed, rejected, capacity_failed
    node_id: str | None
    start: float | None
    finish: float | None
    deadline: float

    @property
    def on_time(self) -> bool:
        return self.status == 'completed'


class Simulator:
    def __init__(self, nodes: list[NodeState], workloads: list[Workload], policy: Policy,
                 telemetry_interval: float = 1.0, telemetry_delay: float = 0.0,
                 horizon: float | None = None):
        if not nodes or len({n.node_id for n in nodes}) != len(nodes):
            raise ValueError('nodes must be nonempty and unique')
        if len({w.workload_id for w in workloads}) != len(workloads):
            raise ValueError('workload ids must be unique')
        if telemetry_interval <= 0 or telemetry_delay < 0:
            raise ValueError('invalid telemetry interval/delay')
        self.nodes = {n.node_id: n for n in nodes}
        self.workloads = {w.workload_id: w for w in workloads}
        self.policy = policy
        self.interval = telemetry_interval
        self.delay = telemetry_delay
        self.horizon = horizon if horizon is not None else max((w.deadline for w in workloads), default=0)
        if self.horizon < 0:
            raise ValueError('horizon must be nonnegative')
        self.actual = {n.node_id: n.cpu_used for n in nodes}
        self.observed: dict[str, TelemetrySample] = {}
        self.results: dict[str, Result] = {}
        self.events: list[tuple[float, int, int, str, object]] = []
        self.sequence = count()
        self.trace: list[tuple[float, str, str]] = []

    def _push(self, t: float, priority: int, kind: str, data: object):
        if t <= self.horizon + 1e-9:
            heapq.heappush(self.events, (t, priority, next(self.sequence), kind, data))

    def _attempt(self, w: Workload, now: float):
        if w.workload_id in self.results:
            return
        if now + w.duration > w.deadline + 1e-9:
            self.results[w.workload_id] = Result(w.workload_id, 'deadline_missed', None, None, None, w.deadline)
            return
        d = self.policy.decide(w, now, dict(self.observed), now - w.arrival_time)
        if d is None:
            self.results[w.workload_id] = Result(w.workload_id, 'rejected', None, None, None, w.deadline)
            return
        if d.wait_until is not None:
            if d.wait_until <= now + 1e-9:
                raise ValueError('policy must schedule retry strictly in future')
            self._push(d.wait_until, 3, 'retry', w.workload_id)
            self.trace.append((now, 'defer', w.workload_id))
            return
        node = self.nodes.get(d.node_id)
        if node is None or d.node_id not in self.observed:
            raise ValueError('policy selected unknown or unobserved node')
        if self.actual[d.node_id] + w.cpu_required > node.cpu_capacity + 1e-9:
            self.results[w.workload_id] = Result(w.workload_id, 'capacity_failed', d.node_id, None, None, w.deadline)
            self.trace.append((now, 'capacity_failed', w.workload_id))
            return
        self.actual[d.node_id] += w.cpu_required
        finish = now + w.duration
        self._push(finish, 0, 'complete', (w.workload_id, d.node_id, w.cpu_required, now))
        self.trace.append((now, 'start', w.workload_id))

    def run(self) -> dict[str, Result]:
        for w in self.workloads.values():
            self._push(w.arrival_time, 2, 'arrival', w.workload_id)
        t = 0.0
        while t <= self.horizon + 1e-9:
            self._push(t, 1, 'measure', None)
            t += self.interval
        while self.events:
            now, _, _, kind, data = heapq.heappop(self.events)
            if kind == 'measure':
                for node in self.nodes.values():
                    sample = TelemetrySample(node.node_id, now, now + self.delay, node.cpu_capacity, self.actual[node.node_id])
                    self._push(sample.received_at, 1, 'deliver', sample)
            elif kind == 'deliver':
                sample = data
                old = self.observed.get(sample.node_id)
                if old is None or sample.measured_at >= old.measured_at:
                    self.observed[sample.node_id] = sample
            elif kind in ('arrival', 'retry'):
                self._attempt(self.workloads[data], now)
            elif kind == 'complete':
                wid, nid, cpu, start = data
                self.actual[nid] -= cpu
                w = self.workloads[wid]
                self.results[wid] = Result(wid, 'completed' if now <= w.deadline + 1e-9 else 'deadline_missed', nid, start, now, w.deadline)
                self.trace.append((now, 'complete', wid))
        for w in self.workloads.values():
            if w.workload_id not in self.results:
                self.results[w.workload_id] = Result(w.workload_id, 'deadline_missed', None, None, None, w.deadline)
        return dict(self.results)
