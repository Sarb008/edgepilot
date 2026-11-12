"""M6H: same-input two-way tie ablation remains strictly offline."""
import copy
import csv
import json
from pathlib import Path

import pytest

from research.kube_tie_ablation import (
    INPUT_FIELDS, STRATA_FIELDS, _group_replays, ablate, export, main,
)
from research.kube_decision_audit import analyze_replays
from research.kube_feasibility_analysis import EvidenceError
from research.kube_feasibility_pilot import FeasibilityConfig, run_experiment
from test_kube_feasibility_pilot import FakeCluster, Clock


@pytest.fixture
def run_dir(tmp_path):
    cluster, clock = FakeCluster(), Clock()
    directory = tmp_path / 'run'
    run_experiment(cluster, FeasibilityConfig(blocks=2, seed=20261114), directory,
                   monotonic=clock, sleep=clock.sleep)
    return directory


def test_study_counts_historical_inputs_not_independent_replays(run_dir):
    report = ablate([run_dir])
    assert report['historical_input_count'] == 8
    assert report['retrospective_policy_decisions'] == 32
    assert len(report['inputs']) == 8
    assert sum(s['inputs'] for s in report['worker_strata']) == 8
    assert set(report['worker_exposure_blocks'].values()) == {1}


def test_two_way_tie_expected_half_bad_when_one_worker_infeasible(run_dir):
    report = ablate([run_dir])
    assert all(row['greedy_tie_count'] == 2 for row in report['inputs'])
    assert all(row['greedy_uniform_expected_infeasible'] == 0.5 for row in report['inputs'])
    assert all(row['greedy_lex_fresh_feasible'] != row['greedy_reverse_fresh_feasible']
               for row in report['inputs'])
    assert report['summary']['greedy_uniform_expected_infeasible'] == 4.0


def test_worker_stratification_exposes_order_bias(run_dir):
    report = ablate([run_dir])
    strata = {s['perturbed_worker']: s for s in report['worker_strata']}
    assert strata['worker-a']['greedy_lex_infeasible'] == 4
    assert strata['worker-a']['greedy_reverse_infeasible'] == 0
    assert strata['worker-b']['greedy_lex_infeasible'] == 0
    assert strata['worker-b']['greedy_reverse_infeasible'] == 4
    assert sum(s['greedy_uniform_expected_infeasible'] for s in strata.values()) == 4


def test_balanced_tie_controls_and_immediate_age_aware(run_dir):
    report = ablate([run_dir])
    total = report['summary']
    assert total['greedy_lex_infeasible'] == 4
    assert total['greedy_reverse_infeasible'] == 4
    assert total['age_penalized_infeasible'] == 0
    assert all(r['age_agrees_with_fresh_reference'] is True for r in report['inputs'])
    assert all(r['bounded_initial_wait'] and r['uncertainty_initial_reject'] for r in report['inputs'])


def test_same_observed_input_replayed_exactly_once(run_dir):
    report = ablate([run_dir])
    assert len({(r['run_index'], r['block'], r['position']) for r in report['inputs']}) == 8
    assert set(r['observed_policy'] for r in report['inputs']) == {
        'greedy', 'age_penalized', 'bounded_deferral', 'uncertainty_aware'}


def test_no_counterfactual_completion_rate_claim(run_dir):
    report = ablate([run_dir])
    assert 'completion' not in report['summary']
    assert 'deadline' not in report['summary']
    limitations = ' '.join(report['limitations']).lower()
    assert 'not observed on-time rates' in limitations
    assert 'no counterfactual pod' in limitations


def test_export_valid_csv_and_no_source_mutation(run_dir, tmp_path):
    report = ablate([run_dir])
    original = (run_dir / 'trials.csv').read_bytes()
    output = tmp_path / 'report'
    export(report, output, [run_dir])
    assert (run_dir / 'trials.csv').read_bytes() == original
    loaded = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
    assert loaded['historical_input_count'] == 8
    assert 'inputs' not in loaded
    for name, fields, expected in (
        ('input_ablation.csv', INPUT_FIELDS, 8),
        ('worker_strata.csv', STRATA_FIELDS, 2),
    ):
        with (output / name).open(newline='', encoding='utf-8') as stream:
            reader = csv.DictReader(stream)
            assert tuple(reader.fieldnames) == fields
            assert len(list(reader)) == expected
    with pytest.raises(FileExistsError):
        export(report, output, [run_dir])


@pytest.mark.parametrize('where', ['same', 'child', 'parent'])
def test_output_cannot_overlap_source(run_dir, tmp_path, where):
    report = ablate([run_dir])
    dest = {'same': run_dir, 'child': run_dir / 'sub', 'parent': tmp_path}[where]
    with pytest.raises(EvidenceError, match='separate'):
        export(report, dest, [run_dir])


def test_missing_run_fails_closed(tmp_path):
    with pytest.raises(EvidenceError):
        ablate([tmp_path / 'nonexistent'])


def test_duplicate_run_fails_closed(run_dir):
    with pytest.raises(EvidenceError):
        ablate([run_dir, run_dir])


def test_two_valid_runs_aggregate_without_double_counting(run_dir, tmp_path):
    cluster, clock = FakeCluster(), Clock()
    second = tmp_path / 'second'
    run_experiment(cluster, FeasibilityConfig(blocks=2, seed=12987), second,
                   monotonic=clock, sleep=clock.sleep)
    report = ablate([run_dir, second])
    assert report['historical_input_count'] == 16
    assert report['retrospective_policy_decisions'] == 64
    assert report['summary']['greedy_uniform_expected_infeasible'] == 8.0
    assert report['worker_exposure_blocks'] == {'worker-a': 2, 'worker-b': 2}


def test_unbalanced_one_block_is_not_pooled(tmp_path):
    cluster, clock = FakeCluster(), Clock()
    directory = tmp_path / 'unbalanced'
    run_experiment(cluster, FeasibilityConfig(blocks=1), directory,
                   monotonic=clock, sleep=clock.sleep)
    with pytest.raises(EvidenceError, match='balanced'):
        ablate([directory])


def test_incompatible_manifest_fails_closed(run_dir, tmp_path):
    cluster, clock = FakeCluster(), Clock()
    second = tmp_path / 'different'
    run_experiment(cluster, FeasibilityConfig(blocks=2, margin_cpu_m=80), second,
                   monotonic=clock, sleep=clock.sleep)
    with pytest.raises(EvidenceError, match='different workload parameters'):
        ablate([run_dir, second])


@pytest.mark.parametrize('mutation', [
    lambda rows: rows.pop(),
    lambda rows: rows.append(copy.deepcopy(rows[0])),
    lambda rows: rows[0].__setitem__('greedy_exact_top_tie_count', 3),
    lambda rows: rows[0].__setitem__('greedy_lexicographic_first', None),
    lambda rows: rows[0].__setitem__('greedy_reverse_lexicographic', None),
    lambda rows: rows[0].__setitem__('greedy_reverse_fresh_feasible', None),
    lambda rows: rows[0].__setitem__('initial_selected_fresh_feasible', 'True'),
    lambda rows: rows[0].__setitem__('initial_selected_worker', 'nonexistent'),
])
def test_corrupt_replay_rows_fail_closed(run_dir, mutation):
    data = copy.deepcopy(analyze_replays([run_dir])['details'])
    mutation(data)
    with pytest.raises(EvidenceError):
        _group_replays(data)


def test_cross_policy_metadata_corruption_rejected(run_dir):
    rows = copy.deepcopy(analyze_replays([run_dir])['details'])
    aged = next(r for r in rows if r['replayed_policy'] == 'age_penalized')
    aged['perturbed_worker'] = 'bogus'
    with pytest.raises(EvidenceError, match='metadata'):
        _group_replays(rows)


def test_no_cluster_calls_and_helpful_cli(run_dir, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError('Kubernetes access attempted in offline M6H')
    monkeypatch.setattr('research.kube_tie_ablation.Kubectl', forbidden, raising=False)
    output = tmp_path / 'cmd_output'
    main(['--run', str(run_dir), '--output', str(output)])
    result = capsys.readouterr().out
    assert '8 historical inputs' in result
    assert 'uniform-tie greedy (expected)' in result
    assert (output / 'summary.json').exists()


def test_outcome_tampering_in_source_rejected(run_dir):
    source = run_dir / 'trials.csv'
    content = source.read_text(encoding='utf-8')
    assert 'greedy' in content
    source.write_text(content.replace('greedy', 'fake_policy', 1), encoding='utf-8')
    with pytest.raises(EvidenceError):
        ablate([run_dir])


def test_single_max_capacity_no_tie_has_deterministic_expected_score(run_dir):
    replay = copy.deepcopy(analyze_replays([run_dir])['details'])
    greedy = next(r for r in replay if r['replayed_policy'] == 'greedy')
    greedy['greedy_exact_top_tie_count'] = 1
    greedy['greedy_reverse_lexicographic'] = greedy['greedy_lexicographic_first']
    greedy['greedy_reverse_fresh_feasible'] = greedy['initial_selected_fresh_feasible']
    scored = _group_replays(replay)
    first = next(r for r in scored if (r['run_index'], r['block'], r['position']) == (
        greedy['run_index'], greedy['block'], greedy['position']))
    assert first['greedy_tie_count'] == 1
    assert first['greedy_uniform_expected_infeasible'] == float(not first['greedy_lex_fresh_feasible'])


def test_single_max_capacity_must_have_identical_lex_reverse(run_dir):
    replay = copy.deepcopy(analyze_replays([run_dir])['details'])
    greedy = next(r for r in replay if r['replayed_policy'] == 'greedy')
    greedy['greedy_exact_top_tie_count'] = 1
    with pytest.raises(EvidenceError, match='tie'):
        _group_replays(replay)


def test_two_way_tie_cannot_have_identical_lex_reverse(run_dir):
    replay = copy.deepcopy(analyze_replays([run_dir])['details'])
    greedy = next(r for r in replay if r['replayed_policy'] == 'greedy')
    greedy['greedy_reverse_lexicographic'] = greedy['greedy_lexicographic_first']
    with pytest.raises(EvidenceError, match='tie'):
        _group_replays(replay)


def test_age_policy_rejection_must_not_be_counted_as_selection(run_dir):
    replay = copy.deepcopy(analyze_replays([run_dir])['details'])
    age = next(r for r in replay if r['replayed_policy'] == 'age_penalized')
    age['initial_decision'] = 'reject'
    age['initial_selected_worker'] = None
    age['initial_selected_fresh_feasible'] = None
    with pytest.raises(EvidenceError, match='age-aware'):
        _group_replays(replay)


def test_bounded_wait_cannot_be_relabelled_success(run_dir):
    replay = copy.deepcopy(analyze_replays([run_dir])['details'])
    bounded = next(r for r in replay if r['replayed_policy'] == 'bounded_deferral')
    bounded['initial_decision'] = 'select'
    with pytest.raises(EvidenceError, match='non-immediate'):
        _group_replays(replay)
