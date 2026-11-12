"""Summarize measured real Kubernetes smoke outputs without fabricating missing fields.

Pass --directory pointing to a directory of smoke_1.json ... smoke_N.json.
Only runs on saved JSON; never connects to or modifies a cluster.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import statistics
from typing import Any


METRICS = ('end_to_end_observed_seconds',
           'kubernetes_creation_to_container_start_seconds',
           'kubernetes_container_execution_seconds',
           'estimated_telemetry_age_at_decision_seconds',
           'submit_api_elapsed_seconds')


def read_smokes(directory: Path, pattern: str = 'smoke_*.json') -> list[dict[str, Any]]:
    if not directory.is_dir():
        raise ValueError(f'not a directory: {directory}')
    paths = sorted(directory.glob(pattern))
    if not paths:
        raise ValueError(f'no smoke files matching {pattern!r} in {directory}')
    rows: list[dict[str, Any]] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if 'smoke' not in payload or not isinstance(payload['smoke'], dict):
            raise ValueError(f'{path}: missing smoke object')
        sample = payload['smoke']
        row = {'filename': path.name,
               'context': payload.get('context'),
               'policy': sample.get('policy'),
               'node': sample.get('node_name'),
               'terminal_phase': sample.get('terminal_phase'),
               'on_time_observed': sample.get('on_time_observed'),
               'container_exit_code': sample.get('container_exit_code'),
               'cleanup_attempted_and_accepted': sample.get('cleanup_attempted_and_accepted')}
        for metric in METRICS:
            value = sample.get(metric)
            if value is not None and (not isinstance(value, (int, float))
                                      or isinstance(value, bool) or value < 0):
                raise ValueError(f'{path}: invalid {metric}: {value!r}')
            row[metric] = value
        rows.append(row)
    return rows


def quantile_nearest_rank(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    if not 0 < percent <= 1:
        raise ValueError('percent must be in (0,1]')
    import math
    return sorted(values)[max(0, math.ceil(len(values) * percent) - 1)]


def describe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    stats = {}
    for metric in METRICS:
        values = [float(r[metric]) for r in rows if r.get(metric) is not None]
        stats[metric] = {'count': len(values),
                         'min': min(values) if values else None,
                         'median': statistics.median(values) if values else None,
                         'p95_nearest_rank': quantile_nearest_rank(values, .95),
                         'max': max(values) if values else None}
    return {
        'sample_count': len(rows),
        'successful': sum(r['terminal_phase'] == 'Succeeded' for r in rows),
        'observed_on_time': sum(r['on_time_observed'] is True for r in rows),
        'cleanup_accepted': sum(r['cleanup_attempted_and_accepted'] is True for r in rows),
        'nodes_observed': sorted(set(str(r.get('node')) for r in rows if r.get('node'))),
        'metrics': stats,
        'limitations': [
            'Small observational sample, not a randomized policy comparison',
            'Client end-to-end duration includes polling delay',
            'Kubernetes timestamps are second-resolution and not combined with local monotonic times',
            'Image cache warmup can substantially change startup latency',
            'p95 nearest-rank for 10 samples is the maximum, not a stable tail estimate',
        ],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path,
                        default=Path('research_outputs/kubernetes'))
    parser.add_argument('--pattern', default='smoke_*.json')
    parser.add_argument('--output', type=Path,
                        default=Path('research_outputs/kubernetes/smoke_summary.json'))
    args = parser.parse_args(argv)
    rows = read_smokes(args.directory, args.pattern)
    result = describe(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    with (args.output.parent / 'smoke_samples.csv').open('w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Real smoke tests: {result['successful']}/{result['sample_count']} Succeeded; "
          f"{result['observed_on_time']} on time; {result['cleanup_accepted']} cleanup accepted")
    for metric, record in result['metrics'].items():
        print(f"  {metric:51} n={record['count']:2d}, "
              f"median={record['median']}, p95={record['p95_nearest_rank']}")
    print(f'Wrote {args.output}')


if __name__ == '__main__':
    main()
