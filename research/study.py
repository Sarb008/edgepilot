"""Predeclared synthetic sensitivity study for EdgePilot v2.

Every policy receives exactly the same workloads, nodes and impairment map for
one (condition, seed). Workload and telemetry RNG streams are independent so a
telemetry-only intervention leaves the generated workload trace unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import ceil, isfinite
from random import Random
from typing import Iterable

from .advanced import Impairment, ResearchSimulator
from .baselines import AgePenalized, UncertaintyAware
from .models import NodeState, Workload
from .policies import BoundedDeferral, Greedy


@dataclass(frozen=True, slots=True)
class Condition:
    name: str
    jobs: int = 60
    arrival_start: float = 8.0
    arrival_end: float = 58.0
    delay_base: float = 0.5
    delay_jitter: float = 1.0
    drop_probability: float = 0.08
    slack_multiplier: float = 1.0
    telemetry_interval: float = 1.0

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('condition name must be nonempty')
        if isinstance(self.jobs, bool) or not isinstance(self.jobs, int) or self.jobs < 1:
            raise ValueError('jobs must be a positive integer')
        for field in ('arrival_start', 'arrival_end', 'delay_base', 'delay_jitter',
                      'drop_probability', 'slack_multiplier', 'telemetry_interval'):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                raise ValueError(f'{field} must be finite')
        if not (0 <= self.arrival_start < self.arrival_end):
            raise ValueError('arrival window must be positive and increasing')
        if self.delay_base < 0 or self.delay_jitter < 0:
            raise ValueError('telemetry delays must be nonnegative')
        if not 0 <= self.drop_probability <= 1:
            raise ValueError('drop_probability must be within [0, 1]')
        if self.slack_multiplier < 0 or self.telemetry_interval <= 0:
            raise ValueError('slack_multiplier must be nonnegative and interval positive')


def conditions() -> tuple[Condition, ...]:
    """One-factor-at-a-time variations around an explicit baseline.

    These are exploratory sensitivity scenarios, not independent field data.
    """
    base = Condition('baseline')
    return (
        base,
        replace(base, name='lower_load', jobs=35),
        replace(base, name='higher_load', jobs=100),
        replace(base, name='fresh_telemetry', delay_base=0, delay_jitter=0, drop_probability=0),
        replace(base, name='severe_telemetry', delay_base=2, delay_jitter=3, drop_probability=0.25),
        replace(base, name='tight_deadlines', slack_multiplier=0.25),
        replace(base, name='loose_deadlines', slack_multiplier=2.0),
    )


def generate(seed: int, condition: Condition) -> tuple[list[NodeState], list[Workload], Impairment]:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError('seed must be an integer')
    # Separate deterministic streams. Interventions on telemetry do not change jobs.
    jobs_rng = Random(seed * 2 + 12345)
    telem_rng = Random(seed * 2 + 23456)
    nodes = [NodeState(f'node-{i}', 0, 4, 0) for i in range(3)]
    workloads = []
    for i in range(condition.jobs):
        arrival = round(jobs_rng.uniform(condition.arrival_start, condition.arrival_end), 6)
        duration = jobs_rng.choice((2.0, 3.0, 4.0, 5.0))
        slack = jobs_rng.choice((0.5, 1.0, 2.0, 4.0)) * condition.slack_multiplier
        cpu = jobs_rng.choice((1.0, 2.0))
        workloads.append(Workload(f'job-{i:04d}', arrival, arrival + duration + slack, cpu, duration))
    horizon = max(w.deadline for w in workloads)
    delays: dict[tuple[str, int], float] = {}
    drops: set[tuple[str, int]] = set()
    for i in range(ceil(horizon / condition.telemetry_interval) + 1):
        for node in nodes:
            key = (node.node_id, i)
            if telem_rng.random() < condition.drop_probability:
                drops.add(key)
            else:
                delays[key] = round(condition.delay_base + condition.delay_jitter * telem_rng.random(), 6)
    return nodes, workloads, Impairment(delays=delays, drops=frozenset(drops))


def policies():
    """Frozen policy parameters; do not tune on evaluation seeds."""
    return {
        'greedy': Greedy(),
        'age_penalized': AgePenalized(penalty_per_second=0.3),
        'uncertainty_aware': UncertaintyAware(reserve_per_second=0.3),
        'bounded_deferral': BoundedDeferral(max_wait=2.0, freshness_threshold=0.75),
    }


def run_study(seeds: Iterable[int] = range(30), study_conditions: Iterable[Condition] | None = None) -> list[dict]:
    conditions_to_run = tuple(conditions() if study_conditions is None else study_conditions)
    seeds_to_run = tuple(seeds)
    if not seeds_to_run or not conditions_to_run:
        raise ValueError('seeds and conditions must be nonempty')
    if len(set(seeds_to_run)) != len(seeds_to_run):
        raise ValueError('seeds must be unique')
    if len({c.name for c in conditions_to_run}) != len(conditions_to_run):
        raise ValueError('condition names must be unique')
    rows: list[dict] = []
    for condition in conditions_to_run:
        for seed in seeds_to_run:
            nodes, workloads, impairment = generate(seed, condition)
            for label, policy in policies().items():
                sim = ResearchSimulator(nodes, workloads, policy, telemetry_interval=condition.telemetry_interval,
                                        impairment=impairment)
                sim.run()
                rows.append({'condition': condition.name, 'seed': seed, 'policy': label, **sim.metrics()})
    return rows
