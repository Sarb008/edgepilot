"""Additional scheduling baselines, using only received telemetry."""
from dataclasses import dataclass
import math
from .models import TelemetrySample, Workload
from .policies import Decision


@dataclass(frozen=True)
class AgePenalized:
    penalty_per_second: float = 0.25

    def __post_init__(self):
        if not math.isfinite(self.penalty_per_second) or self.penalty_per_second < 0:
            raise ValueError('penalty must be finite and nonnegative')

    def decide(self, workload: Workload, now: float, samples: dict[str, TelemetrySample], waited: float) -> Decision | None:
        candidates = [s for s in samples.values() if s.available_cpu + 1e-9 >= workload.cpu_required]
        if not candidates:
            return None
        best = min(candidates, key=lambda s: (-(s.available_cpu - self.penalty_per_second * s.age_at(now)), s.node_id))
        return Decision(node_id=best.node_id)


@dataclass(frozen=True)
class UncertaintyAware:
    """Reserve CPU headroom proportional to telemetry age (not a learned model)."""
    reserve_per_second: float = 0.25

    def __post_init__(self):
        if not math.isfinite(self.reserve_per_second) or self.reserve_per_second < 0:
            raise ValueError('reserve must be finite and nonnegative')

    def decide(self, workload: Workload, now: float, samples: dict[str, TelemetrySample], waited: float) -> Decision | None:
        candidates = [s for s in samples.values()
                      if s.available_cpu - self.reserve_per_second * s.age_at(now) + 1e-9 >= workload.cpu_required]
        if not candidates:
            return None
        best = min(candidates, key=lambda s: (-(s.available_cpu - self.reserve_per_second * s.age_at(now)), s.node_id))
        return Decision(node_id=best.node_id)
