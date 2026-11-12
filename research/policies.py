"""Scheduling decisions use ONLY telemetry already delivered to the scheduler."""
from dataclasses import dataclass
from math import isfinite
from typing import Protocol
from .models import TelemetrySample, Workload


@dataclass(frozen=True)
class Decision:
    node_id: str | None = None
    wait_until: float | None = None

    def __post_init__(self):
        if (self.node_id is None) == (self.wait_until is None):
            raise ValueError('exactly one of node_id and wait_until required')


class Policy(Protocol):
    def decide(self, workload: Workload, now: float, samples: dict[str, TelemetrySample], waited: float) -> Decision | None: ...


def _best(workload: Workload, samples: dict[str, TelemetrySample]) -> str | None:
    eligible = [s for s in samples.values() if s.available_cpu + 1e-9 >= workload.cpu_required]
    return min(eligible, key=lambda s: (-s.available_cpu, s.node_id)).node_id if eligible else None


@dataclass(frozen=True)
class Greedy:
    def decide(self, workload: Workload, now: float, samples: dict[str, TelemetrySample], waited: float) -> Decision | None:
        node = _best(workload, samples)
        return Decision(node_id=node) if node else None


@dataclass(frozen=True)
class BoundedDeferral:
    max_wait: float
    freshness_threshold: float
    placement_latency: float = 0.0  # known dispatch/admission overhead, not future telemetry

    def __post_init__(self):
        for name in ('max_wait', 'freshness_threshold', 'placement_latency'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
                raise ValueError(f'{name} must be finite and nonnegative')

    def decide(self, workload: Workload, now: float, samples: dict[str, TelemetrySample], waited: float) -> Decision | None:
        node = _best(workload, samples)
        if node is not None and samples[node].age_at(now) <= self.freshness_threshold:
            return Decision(node_id=node)
        # No deferral once the execution deadline would be jeopardized.
        latest = min(workload.arrival_time + self.max_wait,
                     workload.deadline - workload.duration - self.placement_latency)
        if now + 1e-9 < latest:
            return Decision(wait_until=latest)
        return Decision(node_id=node) if node else None
