"""Predeclared paired experiments for optional reliability stress events.

Fault schedules are not conditioned on scheduler actions or outcomes. The same
(seed, scenario) produces identical workloads, telemetry, and exogenous faults
for each policy. No optimization on evaluation seeds is performed here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .reliability import BackgroundLoad, NodeOutage, ReliabilityConfig, ReliabilitySimulator
from .study import Condition, generate, policies
from .policies import BoundedDeferral


@dataclass(frozen=True, slots=True)
class ReliabilityScenario:
    name: str
    config: ReliabilityConfig


def scenarios() -> tuple[ReliabilityScenario, ...]:
    outages = (NodeOutage('node-0', 21.0, 24.0), NodeOutage('node-1', 37.0, 40.0))
    interference = (
        BackgroundLoad('node-0', 18.0, 2.5), BackgroundLoad('node-0', 31.0, 0.0),
        BackgroundLoad('node-1', 38.0, 2.0), BackgroundLoad('node-1', 52.0, 0.0),
    )
    return (
        ReliabilityScenario('nominal', ReliabilityConfig()),
        ReliabilityScenario('dispatch_latency', ReliabilityConfig(dispatch_delay=0.4, max_placement_retries=1)),
        ReliabilityScenario('node_outages', ReliabilityConfig(outages=outages, max_restarts=1,
                                                            max_placement_retries=1)),
        ReliabilityScenario('external_contention', ReliabilityConfig(background=interference, max_restarts=1,
                                                                     max_placement_retries=1)),
        ReliabilityScenario('combined_stress', ReliabilityConfig(dispatch_delay=0.4, outages=outages,
                                                                background=interference, max_restarts=2,
                                                                max_placement_retries=1)),
    )


def run_reliability_study(seeds: Iterable[int],
                          stress_scenarios: Iterable[ReliabilityScenario] | None = None,
                          base: Condition | None = None) -> list[dict]:
    seed_ids = tuple(seeds)
    scenario_defs = tuple(scenarios() if stress_scenarios is None else stress_scenarios)
    if not seed_ids or not scenario_defs:
        raise ValueError('seeds and scenarios must be nonempty')
    if len(set(seed_ids)) != len(seed_ids):
        raise ValueError('seeds must be unique')
    if len({s.name for s in scenario_defs}) != len(scenario_defs):
        raise ValueError('scenario names must be unique')
    for s in scenario_defs:
        if not isinstance(s, ReliabilityScenario) or not isinstance(s.config, ReliabilityConfig):
            raise ValueError('invalid scenario definition')
    condition = base or Condition('stress_baseline')
    rows: list[dict] = []
    for seed in seed_ids:
        # Generate ONCE per seed, not once per policy or reliability scenario.
        nodes, workloads, impairment = generate(seed, condition)
        for scenario in scenario_defs:
            configured_policies = policies()
            # The time budget is a known constant, not an observation of future capacity.
            configured_policies['bounded_deferral'] = BoundedDeferral(
                max_wait=2.0, freshness_threshold=0.75,
                placement_latency=scenario.config.dispatch_delay)
            for label, policy in configured_policies.items():
                sim = ReliabilitySimulator(nodes, workloads, policy,
                                           telemetry_interval=condition.telemetry_interval,
                                           impairment=impairment,
                                           reliability=scenario.config)
                sim.run()
                rows.append({'condition': scenario.name, 'seed': seed, 'policy': label, **sim.metrics()})
    return rows
