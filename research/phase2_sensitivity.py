"""Predeclared deterministic deadline/freshness sensitivity matrix for EdgePilot v2.

The matrix is an engineering stress design, NOT an iid sample. It deliberately
includes informative changes, feasible changes, and unchanged controls. Both
policies see exactly the same first telemetry and exogenous events in every pair.
No p-values, bootstrap CIs, or claims of population superiority are produced.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
from typing import Any

from .voi_study import MatchedCase, paired_rows, run_pair

WORKERS = ('worker-a', 'worker-b')
DELAYS = (.25, .75, 2.0)
DEADLINES = (4., 5., 6.)
WAIT_CAPS = (.5, 1.5, 3.)
DISPATCH_DELAYS = (.2, .6)
CHANGE_TYPES = (('unchanged', False, 1.3),
                ('feasible_change', True, .8),
                ('infeasible_change', True, 1.3))


def cases() -> tuple[tuple[str, MatchedCase], ...]:
    """Fixed factorial; a no-change control is counted only once per configuration."""
    result: list[tuple[str, MatchedCase]] = []
    for worker in WORKERS:
        for label, changed, reservation in CHANGE_TYPES:
            for delay in DELAYS:
                for deadline in DEADLINES:
                    for cap in WAIT_CAPS:
                        for dispatch in DISPATCH_DELAYS:
                            case = MatchedCase(worker, changed, delay, deadline,
                                               max_wait=cap,
                                               dispatch_delay=dispatch,
                                               reservation_cpu=reservation)
                            key = (f'{worker}|{label}|delay={delay}|deadline={deadline}'
                                   f'|waitcap={cap}|dispatch={dispatch}')
                            result.append((key, case))
    if len(set(k for k, _ in result)) != len(result):
        raise AssertionError('duplicate predeclared experiment key')
    return tuple(result)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def aggregate(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    if not rows:
        raise ValueError('empty paired data')
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row[key]), []).append(row)
    out = []
    for level, group in sorted(grouped.items()):
        diffs = [int(r['on_time_delta']) for r in group]
        out.append({'factor': key, 'level': level, 'paired_cases': len(group),
                    'age_on_time': sum(int(r['age_on_time']) for r in group),
                    'bounded_on_time': sum(int(r['bounded_on_time']) for r in group),
                    'bounded_better': sum(x > 0 for x in diffs),
                    'bounded_worse': sum(x < 0 for x in diffs),
                    'tied': sum(x == 0 for x in diffs),
                    'mean_delta_percentage_points': round(100 * mean(diffs), 6),
                    'mean_extra_wait_seconds': round(mean(float(r['extra_wait']) for r in group), 6)})
    return out


def _plot(summary_rows: list[dict[str, Any]], path: Path) -> None:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    data = {r['level']: r for r in summary_rows if r['factor'] == 'change_type'}
    labels = ['unchanged', 'feasible_change', 'infeasible_change']
    values = [data[label]['mean_delta_percentage_points'] for label in labels]
    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.bar(['Unchanged', 'Feasible change', 'Infeasible change'], values)
    ax.axhline(0, linewidth=.8, color='black')
    ax.set_ylabel('On-time difference (bounded − immediate), percentage points')
    ax.set_title('Predeclared deterministic sensitivity cases')
    fig.text(.5, .01, 'Not independent trials; no significance inference.', ha='center', fontsize=9)
    fig.tight_layout(rect=(0, .04, 1, 1))
    fig.savefig(path, dpi=140)
    plt.close(fig)


def run(output: Path, selected: tuple[tuple[str, MatchedCase], ...] | None = None) -> dict[str, Any]:
    """Fail closed on duplicate cases, mismatched inputs, and existing outputs."""
    if output.exists():
        raise FileExistsError(f'existing output, refusing overwrite: {output}')
    definitions = cases() if selected is None else selected
    if not definitions or len({k for k, _ in definitions}) != len(definitions):
        raise ValueError('nonempty unique experiment keys required')
    trial_rows: list[dict[str, Any]] = []
    paired: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    for key, case in definitions:
        if not isinstance(key, str) or not key or not isinstance(case, MatchedCase):
            raise ValueError('malformed predeclared case')
        labels = [entry for entry in CHANGE_TYPES if entry[1] == case.change and entry[2] == case.reservation_cpu]
        if not labels:
            raise ValueError('case not in declared change categories')
        change_type = labels[0][0]
        both, trace = run_pair(case)
        p = paired_rows(both)
        if len(p) != 1:
            raise AssertionError('expected exactly one matched pair')
        params = {'study_case_id': key, 'change_type': change_type,
                  'wait_cap': case.max_wait, 'dispatch_delay': case.dispatch_delay,
                  'reservation_cpu': case.reservation_cpu}
        trial_rows.extend({**params, **row} for row in both)
        paired.append({**params, **p[0]})
        traces.append({'study_case_id': key, **trace})
    summary_rows = []
    for axis in ('change_type', 'perturbed_worker', 'telemetry_delay', 'deadline',
                 'wait_cap', 'dispatch_delay'):
        summary_rows.extend(aggregate(paired, axis))
    limits = [
        'Synthetic fixed factorial of configured cases, not an iid sample; no inferential confidence intervals.',
        'The no-change control and both types of capacity change are predeclared and always reported.',
        'Full exogenous events and time-indexed decisions saved for each case.',
        'Fresh actual state is not passed to either policy; simulation uses request reservations.',
        'Repeated factors across a worker are correlated designs; count them as matched cases, not empirical replications.',
        'No Kubernetes performance or general causal conclusions can be derived from this figure.',
    ]
    manifest = {'study': 'EdgePilot v2 Phase 2 / predeclared sensitivity stress matrix',
                'design': '2 workers × 3 change types × 3 telemetry delays × 3 deadlines × 3 wait limits × 2 dispatch latencies',
                'case_count': len(paired), 'policy_runs': len(trial_rows),
                'axes': {'workers': WORKERS, 'change_types': CHANGE_TYPES, 'telemetry_delay': DELAYS,
                         'deadline': DEADLINES, 'max_wait': WAIT_CAPS, 'dispatch_delay': DISPATCH_DELAYS},
                'cases': [{'id': key, 'parameters': asdict(case)} for key, case in definitions],
                'limitations': limits}
    output.mkdir(parents=True, exist_ok=False)
    _write_csv(output / 'trials.csv', trial_rows)
    _write_csv(output / 'paired.csv', paired)
    _write_csv(output / 'strata.csv', summary_rows)
    with (output / 'traces.jsonl').open('w', encoding='utf-8') as f:
        for entry in traces:
            f.write(json.dumps(entry, sort_keys=True) + '\n')
    _plot(summary_rows, output / 'sensitivity.png')
    manifest['artifact_sha256'] = {p.name: sha256(p.read_bytes()).hexdigest()
                                   for p in sorted(output.iterdir()) if p.is_file()}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    result = {'cases': len(paired), 'policy_runs': len(trial_rows),
              'by_change_type': {row['level']: row for row in summary_rows
                                 if row['factor'] == 'change_type'},
              'limitations': limits}
    (output / 'summary.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('research_outputs/phase2_sensitivity'))
    args = parser.parse_args(argv)
    print(f'Fixed design: {len(cases())} matched cases, {len(cases()) * 2} deterministic simulations.')
    report = run(args.output)
    for label, row in report['by_change_type'].items():
        print(f'  {label:19} n={row["paired_cases"]} '
              f'age={row["age_on_time"]} bounded={row["bounded_on_time"]} '
              f'delta={row["mean_delta_percentage_points"]:+.2f} pp '
              f'extra_wait={row["mean_extra_wait_seconds"]:.2f}s')
    print(f'Wrote {args.output}; SYNTHETIC design only, no significance claims.')


if __name__ == '__main__':
    main()
