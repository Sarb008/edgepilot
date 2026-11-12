"""M6H: offline two-worker tie-breaking ablation on validated M6G input replays.

Four sensitivity arms are scored on the same recorded input:
  - greedy_lex: actual deterministic lexical greedy initial selection
  - greedy_reverse: reverse lexical choice *only when max capacities tie*
  - greedy_uniform_tie: analytical expectation under a fair 50/50 tie choice
  - age_penalized: actual immediate age-aware selection on that input

Only the *retrospective fresh-reference CPU-request feasibility* of an initial
selection is scored. The uniform arm submits no Pod and invents no outcome.
No Kubernetes calls are made. Future information is never passed to a policy.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Sequence

from .kube_decision_audit import analyze_replays
from .kube_feasibility_analysis import EvidenceError

INPUT_FIELDS = (
    'run_index', 'block', 'position', 'observed_policy', 'perturbed_worker',
    'fresh_reference_worker', 'greedy_tie_count', 'greedy_lex_worker',
    'greedy_reverse_worker', 'greedy_lex_fresh_feasible',
    'greedy_reverse_fresh_feasible', 'greedy_uniform_expected_infeasible',
    'age_worker', 'age_fresh_feasible', 'age_agrees_with_fresh_reference',
    'bounded_initial_wait', 'uncertainty_initial_reject',
)
STRATA_FIELDS = (
    'perturbed_worker', 'inputs', 'greedy_lex_infeasible',
    'greedy_reverse_infeasible', 'greedy_uniform_expected_infeasible',
    'age_penalized_infeasible', 'greedy_tied_inputs',
)


def _group_replays(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score one *historical* input once, not four independent replays."""
    groups: dict[tuple[int, int, int], dict[str, dict[str, Any]]] = {}
    for record in details:
        key = (record['run_index'], record['block'], record['position'])
        group = groups.setdefault(key, {})
        policy = record['replayed_policy']
        if policy in group:
            raise EvidenceError('duplicate replay policy within one historical input')
        group[policy] = record
    rows: list[dict[str, Any]] = []
    expected_policies = {'greedy', 'age_penalized', 'bounded_deferral', 'uncertainty_aware'}
    for key, group in sorted(groups.items()):
        if set(group) != expected_policies:
            raise EvidenceError('missing or extra replay policy for historical input')
        greedy = group['greedy']
        age = group['age_penalized']
        deferred = group['bounded_deferral']
        conservative = group['uncertainty_aware']
        count = greedy['greedy_exact_top_tie_count']
        lex = greedy['greedy_lexicographic_first']
        reverse = greedy['greedy_reverse_lexicographic']
        lex_feasible = greedy['initial_selected_fresh_feasible']
        reverse_feasible = greedy['greedy_reverse_fresh_feasible']
        # Validated study has exactly two eligible workers. A 2-way top tie
        # gives two equally probable choices under a *hypothetical* fair coin;
        # no random execution or counterfactual Pod is asserted.
        if (type(count) is not int or count not in (1, 2)
                or not isinstance(lex, str) or not isinstance(reverse, str)
                or type(lex_feasible) is not bool or type(reverse_feasible) is not bool
                or greedy['initial_decision'] != 'select'
                or greedy['initial_selected_worker'] != lex
                or ((count == 1) != (lex == reverse))):
            raise EvidenceError('unexpected greedy tie or feasibility data')
        if (age['initial_decision'] != 'select' or type(age['initial_selected_fresh_feasible']) is not bool
                or age['initial_selected_worker'] is None):
            raise EvidenceError('age-aware policy must make an immediate selection for this study')
        if deferred['initial_decision'] != 'wait' or conservative['initial_decision'] != 'reject':
            raise EvidenceError('unexpected non-immediate policy decision in recorded input')
        for other in (age, deferred, conservative):
            for field in ('observed_policy', 'perturbed_worker', 'fresh_reference_worker'):
                if other[field] != greedy[field]:
                    raise EvidenceError('replayed policy metadata differs within historical input')
        expected_bad = ((not lex_feasible) + (not reverse_feasible)) / 2.0 if count == 2 else float(not lex_feasible)
        rows.append(dict(zip(INPUT_FIELDS, (
            *key, greedy['observed_policy'], greedy['perturbed_worker'],
            greedy['fresh_reference_worker'], count, lex, reverse,
            lex_feasible, reverse_feasible, expected_bad,
            age['initial_selected_worker'], age['initial_selected_fresh_feasible'],
            age['initial_agrees_with_fresh_reference'], True, True,
        ))))
    return rows


def ablate(directories: Sequence[Path]) -> dict[str, Any]:
    """Perform analytical sensitivity using only vetted M6G replay evidence."""
    replay = analyze_replays(directories)
    rows = _group_replays(replay['details'])
    n = replay['historical_trial_count']
    if len(rows) != n:
        raise EvidenceError('ablation input count differs from source trace count')
    workers = sorted(replay['worker_exposure'])
    if len(workers) != 2:
        raise EvidenceError('ablation requires exactly two perturbed worker strata')

    def scores(subset: list[dict[str, Any]], worker: str | None) -> dict[str, Any]:
        return {
            'perturbed_worker': worker or 'ALL',
            'inputs': len(subset),
            'greedy_lex_infeasible': sum(not r['greedy_lex_fresh_feasible'] for r in subset),
            'greedy_reverse_infeasible': sum(not r['greedy_reverse_fresh_feasible'] for r in subset),
            'greedy_uniform_expected_infeasible': sum(r['greedy_uniform_expected_infeasible'] for r in subset),
            'age_penalized_infeasible': sum(not r['age_fresh_feasible'] for r in subset),
            'greedy_tied_inputs': sum(r['greedy_tie_count'] == 2 for r in subset),
        }

    strata = [scores([r for r in rows if r['perturbed_worker'] == w], w) for w in workers]
    overall = scores(rows, None)
    # One four-policy replay per historical input. Do not turn the four replays
    # into four independent experimental observations or compute significance.
    return {
        'study': 'EdgePilot v2 M6H / tie-break-controlled initial-decision feasibility ablation',
        'historical_input_count': n,
        'retrospective_policy_decisions': replay['replay_decision_count'],
        'worker_exposure_blocks': replay['worker_exposure'],
        'summary': overall,
        'worker_strata': strata,
        'inputs': rows,
        'evidence': replay['evidence'],
        'limitations': [
            'OFFLINE ONLY: no new Kubernetes Pod, no extra workload completions',
            'Scores are retrospective fresh-reference CPU-request feasibility, NOT observed on-time rates',
            'Uniform tie choice is an analytical 50/50 expectation, NOT an executed randomized policy',
            'Each historical input counted once; four replayed policy decisions are correlated',
            'Age-penalized decisions use only the historically observed mixed-age telemetry',
            'Bounded deferral replay stops at the WAIT decision; later observations are not invented',
            'Reverse-tie selection is hypothetical; no counterfactual Pod outcome is asserted',
            'Kubernetes CPU requests are not actual CPU consumption or physical worker independence',
            'Small sequential kind experiments are not a randomized or causal validation',
        ],
    }


def export(report: dict[str, Any], output: Path, directories: Sequence[Path]) -> None:
    output = Path(output).resolve()
    for run in directories:
        source = Path(run).resolve()
        if output == source or output in source.parents or source in output.parents:
            raise EvidenceError('output must be separate from source evidence')
    if output.exists():
        raise FileExistsError(f'refuse to overwrite pre-existing output: {output}')
    output.mkdir(parents=True)
    body = {k: v for k, v in report.items() if k not in ('inputs', 'worker_strata')}
    (output / 'summary.json').write_text(json.dumps(body, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    for file, fields, rows in (
        ('input_ablation.csv', INPUT_FIELDS, report['inputs']),
        ('worker_strata.csv', STRATA_FIELDS, report['worker_strata']),
    ):
        with (output / file).open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='append', type=Path, required=True)
    parser.add_argument('--output', type=Path,
                        default=Path('research_outputs/kubernetes/tie_ablation_01'))
    args = parser.parse_args(argv)
    report = ablate(args.run)
    export(report, args.output, args.run)
    overall = report['summary']
    print(f"Audited {overall['inputs']} historical inputs, "
          f"{overall['greedy_tied_inputs']} exact two-way greedy ties (offline only).")
    print('Initial selection INFEASIBLE against a later fresh-reference CPU-request snapshot:')
    for label, key in (
        ('lexical greedy', 'greedy_lex_infeasible'),
        ('reverse-tie greedy', 'greedy_reverse_infeasible'),
        ('uniform-tie greedy (expected)', 'greedy_uniform_expected_infeasible'),
        ('immediate age-penalized', 'age_penalized_infeasible'),
    ):
        print(f'  {label:31} {overall[key]:.1f} / {overall["inputs"]} inputs')
    for row in report['worker_strata']:
        print(f"  worker stratum {row['perturbed_worker']}: n={row['inputs']}; "
              f"lex={row['greedy_lex_infeasible']}, "
              f"reverse={row['greedy_reverse_infeasible']}, "
              f"uniform expected={row['greedy_uniform_expected_infeasible']:.1f}, "
              f"age={row['age_penalized_infeasible']}")
    print(f'Wrote {args.output}; NOT a randomized Kubernetes experiment or latency comparison.')


if __name__ == '__main__':
    main()
