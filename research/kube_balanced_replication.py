"""M6F: opt-in, limited balanced repetitions of the existing M6D kind pilot.

Dry run makes no Kubernetes calls or files. --execute uses at most TWO
2-block M6D runs: 32 disposable Pods in total (sequential, two per trial).
Each run perturbs both workers once. Only the fixed disposable kind context
and the dedicated validation namespace are permitted. Evidence never overwrites.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable

from .kube_feasibility_analysis import audit_runs
from .kube_feasibility_pilot import FeasibilityConfig, run_experiment
from .kube_pilot import CONTEXT, POLICIES, order_by_block
from .kube_stale_pilot import NAMESPACE, assert_empty_namespace, usable_workers
from .kube_validation import Kubectl, capture_snapshot
from .kube_stratified_analysis import analyze, export


@dataclass(frozen=True)
class ReplicationConfig:
    repeats: int = 1
    seed: int = 20261114
    deadline_seconds: float = 20.
    competitor_cpu_m: int = 200
    margin_cpu_m: int = 100

    def __post_init__(self) -> None:
        if type(self.repeats) is not int or not 1 <= self.repeats <= 2:
            raise ValueError('repeats must be integer 1..2 (32-Pod maximum)')
        if type(self.seed) is not int or not 0 <= self.seed < 2**31 - 2:
            raise ValueError('seed must be integer in [0, 2^31 - 3]')
        self.run_config(0)  # reuse all CPU/deadline safety validations

    def run_config(self, index: int) -> FeasibilityConfig:
        if type(index) is not int or not 0 <= index < self.repeats:
            raise ValueError('invalid repeat index')
        return FeasibilityConfig(context=CONTEXT, namespace=NAMESPACE,
                                 blocks=2, seed=self.seed + index,
                                 deadline_seconds=self.deadline_seconds,
                                 competitor_cpu_m=self.competitor_cpu_m,
                                 margin_cpu_m=self.margin_cpu_m)


def planned_runs(config: ReplicationConfig) -> list[dict]:
    return [{'index': i + 1,
             'directory': f'run_{i+1:02d}',
             'seed': config.seed + i,
             'policy_order': [{'block': b, 'position': pos, 'policy': p}
                              for b, pos, p in order_by_block(2, config.seed + i)],
             'expected_target': {'block_1': 'first worker sorted by name',
                                 'block_2': 'second worker sorted by name'}}
            for i in range(config.repeats)]


def execute(client: Kubectl, config: ReplicationConfig, output: Path, *,
            runner: Callable = run_experiment,
            snapshot: Callable = capture_snapshot) -> dict:
    """Fail closed on error. Never delete unrelated Pods or overwrite evidence."""
    output = Path(output)
    if output.exists():
        raise FileExistsError(f'refusing existing experiment output: {output}')
    client.verify_context()
    assert_empty_namespace(client, NAMESPACE)
    nodes = usable_workers(snapshot(client))
    worker_names = [n['name'] for n in nodes]
    output.mkdir(parents=True, exist_ok=False)
    run_dirs: list[Path] = []
    manifest = {'study': 'EdgePilot v2 / M6F balanced Kubernetes CPU-request feasibility',
                'created_at_utc': datetime.now(timezone.utc).isoformat(),
                'config': asdict(config), 'workers_at_start': worker_names,
                'plan': planned_runs(config), 'status': 'incomplete',
                'completed_runs': [],
                'notes': ['2 sequential blocks per run, one perturbation per worker',
                          'Each trial samples a newly created reservation, not a matched cluster state',
                          'This is an opt-in local kind experiment, not a causal field trial']}
    manifest_path = output / 'replication_manifest.json'
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    for i, part in enumerate(manifest['plan']):
        run_dir = output / part['directory']
        # run_experiment performs its own context, namespace and cleanup checks.
        runner(client, config.run_config(i), run_dir)
        # Verify correctness *before* starting the next run.
        single = audit_runs([run_dir])
        if single['block_count'] != 2 or set(single['perturbation_target_blocks']) != set(worker_names):
            raise RuntimeError('unexpected worker exposure; stop for manual inspection')
        if any(n != 1 for n in single['perturbation_target_blocks'].values()):
            raise RuntimeError('run is not balanced across workers; stop')
        assert_empty_namespace(client, NAMESPACE)
        run_dirs.append(run_dir)
        manifest['completed_runs'].append(part['directory'])
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    result = analyze(run_dirs)
    export(result, output / 'stratified_report', run_dirs)
    manifest['status'] = 'complete'
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return result


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repeats', type=int, default=1)
    p.add_argument('--seed', type=int, default=20261114)
    p.add_argument('--deadline-seconds', type=float, default=20.)
    p.add_argument('--competitor-cpu-m', type=int, default=200)
    p.add_argument('--margin-cpu-m', type=int, default=100)
    p.add_argument('--output', type=Path,
                   default=Path('research_outputs/kubernetes/balanced_replication_01'))
    p.add_argument('--execute', action='store_true', help='Opt in: up to 32 Pods, only on local kind')
    a = p.parse_args(argv)
    cfg = ReplicationConfig(a.repeats, a.seed, a.deadline_seconds,
                            a.competitor_cpu_m, a.margin_cpu_m)
    plan = planned_runs(cfg)
    print(f'Plan: {cfg.repeats} complete runs x 2 blocks x 4 policies = '
          f'{cfg.repeats*8} trials, <= {cfg.repeats*16} disposable Pods')
    for run in plan:
        print(f"  {run['directory']}: seed={run['seed']}; "
              + ', '.join(f"B{x['block']}:{x['policy']}" for x in run['policy_order']))
    if not a.execute:
        print('DRY RUN. No cluster reads/writes, no output files. Add --execute to opt in.')
        return
    report = execute(Kubectl(CONTEXT), cfg, a.output)
    print(f"Completed {report['trial_count']} trials / {report['block_count']} balanced blocks")
    print(f"Worker exposure: {report['worker_exposure']}")
    print(f'Output: {a.output}')
    print('DESCRIPTIVE ONLY: sequential trials, no causal claims; CPU requests are not utilization.')


if __name__ == '__main__':
    main()
