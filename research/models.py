"""Validated, immutable simulation inputs (CPU units are abstract cores)."""
from dataclasses import dataclass
from math import isfinite


def _number(name: str, value: float, *, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value <= 0 if positive else value < 0:
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")


def _identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _capacity(capacity: float, used: float) -> None:
    _number('cpu_capacity', capacity, positive=True)
    _number('cpu_used', used)
    if used > capacity:
        raise ValueError('cpu_used exceeds cpu_capacity')


@dataclass(frozen=True, slots=True)
class NodeState:
    node_id: str
    timestamp: float
    cpu_capacity: float
    cpu_used: float

    def __post_init__(self) -> None:
        _identifier('node_id', self.node_id)
        _number('timestamp', self.timestamp)
        _capacity(self.cpu_capacity, self.cpu_used)

    @property
    def available_cpu(self) -> float:
        return self.cpu_capacity - self.cpu_used


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    node_id: str
    measured_at: float
    received_at: float
    cpu_capacity: float
    cpu_used: float

    def __post_init__(self) -> None:
        _identifier('node_id', self.node_id)
        _number('measured_at', self.measured_at)
        _number('received_at', self.received_at)
        if self.received_at < self.measured_at:
            raise ValueError('received_at precedes measured_at')
        _capacity(self.cpu_capacity, self.cpu_used)

    @property
    def available_cpu(self) -> float:
        return self.cpu_capacity - self.cpu_used

    def age_at(self, now: float) -> float:
        _number('now', now)
        if now < self.received_at:
            raise ValueError('telemetry has not yet been received')
        return now - self.measured_at


@dataclass(frozen=True, slots=True)
class Workload:
    workload_id: str
    arrival_time: float
    deadline: float
    cpu_required: float
    duration: float

    def __post_init__(self) -> None:
        _identifier('workload_id', self.workload_id)
        _number('arrival_time', self.arrival_time)
        _number('deadline', self.deadline)
        _number('cpu_required', self.cpu_required, positive=True)
        _number('duration', self.duration, positive=True)
        if self.deadline <= self.arrival_time:
            raise ValueError('deadline must be after arrival_time')
