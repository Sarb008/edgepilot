"""Paired reproducible scenario generation and CSV exports. Run: python -m research.benchmark"""
from __future__ import annotations
import csv
from pathlib import Path
import random
from .advanced import ResearchSimulator, Impairment
from .baselines import AgePenalized, UncertaintyAware
from .models import NodeState, Workload
from .policies import Greedy, BoundedDeferral


def scenario(seed: int, jobs: int = 60):
    rng = random.Random(seed)
    nodes = [NodeState(f'node-{i}', 0, 4, 0) for i in range(3)]
    workloads = []
    for i in range(jobs):
        arrival = round(rng.uniform(1, 50), 3)
        duration = rng.choice((2.0, 3.0, 4.0, 5.0))
        slack = rng.choice((0.5, 1.0, 2.0, 4.0))
        workloads.append(Workload(f'job-{i:03}', arrival, arrival + duration + slack, rng.choice((1.0, 2.0)), duration))
    delays = {}
    drops = set()
    for tick in range(65):
        for node in nodes:
            if rng.random() < 0.08:
                drops.add((node.node_id, tick))
            else:
                delays[(node.node_id, tick)] = rng.choice((0.0, 0.5, 1.0, 2.0, 3.0))
    return nodes, workloads, Impairment(delays=delays, drops=frozenset(drops))


def compare(seeds=range(10)) -> list[dict]:
    rows = []
    for seed in seeds:
        nodes, workloads, impairment = scenario(seed)
        policies = {
            'greedy': Greedy(),
            'age_penalized': AgePenalized(0.3),
            'uncertainty_aware': UncertaintyAware(0.3),
            'bounded_deferral': BoundedDeferral(max_wait=2.0, freshness_threshold=0.75),
        }
        for name, policy in policies.items():
            sim = ResearchSimulator(nodes, workloads, policy, telemetry_interval=1.0, impairment=impairment)
            sim.run()
            rows.append({'seed': seed, 'policy': name, **sim.metrics()})
    return rows


def export(rows: list[dict], path: str | Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError('cannot export empty results')
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == '__main__':
    output = Path('research_outputs') / 'paired_baselines.csv'
    rows = compare()
    export(rows, output)
    print(f'Wrote {len(rows)} paired policy trials to {output}')
    for policy in sorted({row['policy'] for row in rows}):
        matching = [r for r in rows if r['policy'] == policy]
        print(f"{policy:20} mean on-time rate: {sum(r['on_time_rate'] for r in matching) / len(matching):.3f}")
