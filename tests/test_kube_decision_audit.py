"""M6G: real-cluster evidence replay is fully OFFLINE, never launches Pods."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from research.kube_decision_audit import (
    DETAIL_FIELDS, _greedy_tie, _numeric, _samples,
    analyze_replays, export_replays, main, replay_trace,
)
from research.kube_feasibility_analysis import EvidenceError
from research.kube_feasibility_pilot import FeasibilityConfig, run_experiment
from research.kube_pilot import POLICIES
from research.models import Workload, TelemetrySample
from test_kube_feasibility_pilot import FakeCluster, Clock


@pytest.fixture
def completed_run(tmp_path):
    fake, clock = FakeCluster(), Clock()
    config = FeasibilityConfig(blocks=2, seed=456)
    path = tmp_path / 'realistic_fake_run'
    run_experiment(fake, config, path, monotonic=clock, sleep=clock.sleep)
    return path


def _trace(path: Path, policy: str = 'greedy'):
    trace_file = next((path / 'traces').glob(f'*_{policy}.json'))
    return json.loads(trace_file.read_text(encoding='utf-8'))


def test_replay_all_policies_same_snapshot(completed_run):
    trace = _trace(completed_run)
    rows = replay_trace(trace, FeasibilityConfig(blocks=2))
    assert len(rows) == 4
    assert {r['replayed_policy'] for r in rows} == set(POLICIES)
    assert {r['fresh_reference_worker'] for r in rows} == {trace['result']['reference_worker']}
    assert {r['perturbed_worker'] for r in rows} == {trace['result']['perturbed_worker']}
    assert len({r['observed_selected_worker'] for r in rows}) == 1


def test_greedy_tie_gives_reverse_worker(completed_run):
    rows = replay_trace(_trace(completed_run), FeasibilityConfig(blocks=2))
    r = next(r for r in rows if r['replayed_policy'] == 'greedy')
    assert r['initial_decision'] == 'select'
    assert r['greedy_exact_top_tie_count'] == 2
    assert r['greedy_lexicographic_first'] == 'worker-a'
    assert r['greedy_reverse_lexicographic'] == 'worker-b'
    assert r['initial_selected_worker'] == 'worker-a'


def test_delayed_policy_initial_wait_not_predicted_outcome(completed_run):
    trace = _trace(completed_run, 'bounded_deferral')
    rows = replay_trace(trace, FeasibilityConfig(blocks=2))
    d = next(r for r in rows if r['replayed_policy'] == 'bounded_deferral')
    assert d['initial_decision'] == 'wait'
    assert d['initial_selected_worker'] is None
    assert 0 < d['initial_wait_until_seconds_from_arrival'] <= 3
    assert d['initial_selected_fresh_feasible'] is None
    assert d['observed_selected_worker'] is not None  # later actual decision, not replay


def test_uncertainty_may_reject_but_completion_not_inferred(completed_run):
    d = next(r for r in replay_trace(_trace(completed_run), FeasibilityConfig(blocks=2))
             if r['replayed_policy'] == 'uncertainty_aware')
    assert d['initial_decision'] == 'reject'
    assert d['initial_selected_fresh_feasible'] is None


def test_end_to_end_balanced_report(completed_run):
    result = analyze_replays([completed_run])
    assert result['historical_trial_count'] == 8
    assert result['replay_decision_count'] == 32
    assert result['worker_exposure'] == {'worker-a': 1, 'worker-b': 1}
    assert result['greedy_exact_top_tie_inputs'] == 8
    assert result['greedy_tie_flip_changes_worker'] == 8
    assert result['greedy_tie_flip_changes_fresh_feasibility'] == 8
    by = {r['policy']: r for r in result['policy_initial_summary']}
    assert by['greedy']['initial_fresh_infeasible_selections'] == 4
    assert by['age_penalized']['initial_fresh_infeasible_selections'] == 0
    assert by['bounded_deferral']['initial_waits'] == 8
    assert by['uncertainty_aware']['initial_refusals'] == 8
    assert len(result['details']) == 32


def test_two_runs_never_claim_replays_are_independent(completed_run, tmp_path):
    fake, clock = FakeCluster(), Clock()
    next_run = tmp_path / 'run_02'
    run_experiment(fake, FeasibilityConfig(blocks=2, seed=987), next_run,
                   monotonic=clock, sleep=clock.sleep)
    result = analyze_replays([completed_run, next_run])
    assert result['historical_trial_count'] == 16
    assert result['replay_decision_count'] == 64
    assert result['worker_exposure'] == {'worker-a': 2, 'worker-b': 2}
    assert len(result['evidence']) == 2


def test_single_block_refused_as_unbalanced(tmp_path):
    fake, clock = FakeCluster(), Clock()
    path = tmp_path / 'one_block'
    run_experiment(fake, FeasibilityConfig(blocks=1), path,
                   monotonic=clock, sleep=clock.sleep)
    with pytest.raises(EvidenceError, match='balanced two-worker'):
        analyze_replays([path])


@pytest.mark.parametrize('value', [None, True, False, '42', float('nan'), float('inf'), float('-inf')])
def test_numeric_non_finite_and_type_rejected(value):
    with pytest.raises(EvidenceError):
        _numeric(value, 'test')


@pytest.mark.parametrize('mixed', [None, [], {}, {'worker-a': None},
    {'worker-a': {'node_id': 'worker-b'}}, {'worker-a': []}])
def test_bad_mixed_samples_rejected(mixed):
    with pytest.raises(EvidenceError):
        _samples(mixed)


@pytest.mark.parametrize('mutation', [
    lambda t: t['result'].__setitem__('target_age_seconds', -1),
    lambda t: t['result'].__setitem__('target_age_seconds', float('nan')),
    lambda t: t['result'].__setitem__('peer_age_seconds', 999),
    lambda t: t['result'].__setitem__('probe_cpu_m', 0),
    lambda t: t['result'].__setitem__('probe_cpu_m', True),
    lambda t: t['result'].__setitem__('deadline_seconds', 99),
    lambda t: t['result'].__setitem__('reference_worker', 'not-a-worker'),
    lambda t: t['mixed_policy_input']['worker-a'].__setitem__('received_at', 9999),
    lambda t: t['mixed_policy_input']['worker-a'].__setitem__('node_id', 'bogus'),
    lambda t: t['mixed_policy_input'].pop('worker-a'),
    lambda t: t['mixed_policy_input']['worker-b'].__setitem__('cpu_used', 99),
])
def test_trace_corruption_fails_closed(completed_run, mutation):
    trace = _trace(completed_run)
    mutation(trace)
    with pytest.raises(EvidenceError):
        replay_trace(trace, FeasibilityConfig(blocks=2))


def test_real_greedy_tie_not_synthetic_epsilon():
    sample_a = TelemetrySample('worker-a', 1., 2., 2., 0.)
    sample_b = TelemetrySample('worker-b', 1., 2., 2., 1e-10)
    workload = Workload('test', 3., 20., 1., 1.)
    inventory = {'node_inventory': [
        {'name': 'worker-a', 'usable_for_model': True, 'estimated_unreserved_cpu_cores': 2.},
        {'name': 'worker-b', 'usable_for_model': True, 'estimated_unreserved_cpu_cores': 2.},
    ]}
    tie = _greedy_tie(workload, {'worker-a': sample_a, 'worker-b': sample_b}, inventory, 1000)
    assert tie['count'] == 1
    assert tie['lex'] == tie['reverse'] == 'worker-a'


def test_output_emits_csv_json_without_touching_source(completed_run, tmp_path):
    report = analyze_replays([completed_run])
    source = (completed_run / 'trials.csv').read_bytes()
    path = tmp_path / 'output'
    export_replays(report, path, [completed_run])
    assert (completed_run / 'trials.csv').read_bytes() == source
    assert json.loads((path / 'summary.json').read_text())['historical_trial_count'] == 8
    with (path / 'replayed_decisions.csv').open(newline='', encoding='utf-8') as stream:
        reader = csv.DictReader(stream)
        assert tuple(reader.fieldnames) == DETAIL_FIELDS
        assert len(list(reader)) == 32
    with pytest.raises(FileExistsError):
        export_replays(report, path, [completed_run])


@pytest.mark.parametrize('placement', ['same', 'child', 'parent'])
def test_export_refuses_overlap(completed_run, tmp_path, placement):
    report = analyze_replays([completed_run])
    where = {'same': completed_run,
             'child': completed_run / 'new',
             'parent': tmp_path}[placement]
    with pytest.raises(EvidenceError, match='separate'):
        export_replays(report, where, [completed_run])


def test_reject_duplicate_input(completed_run):
    with pytest.raises(EvidenceError, match='distinct'):
        analyze_replays([completed_run, completed_run])


def test_reject_missing_folder(tmp_path):
    with pytest.raises(EvidenceError):
        analyze_replays([tmp_path / 'missing'])


def test_offline_cli_never_contacts_kubernetes(completed_run, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError('Kubernetes client used in offline audit')
    monkeypatch.setattr('research.kube_decision_audit.Kubectl', forbidden, raising=False)
    path = tmp_path / 'out'
    main(['--run', str(completed_run), '--output', str(path)])
    assert '32 initial decisions' in capsys.readouterr().out
    assert (path / 'replayed_decisions.csv').is_file()


def test_csv_trace_mismatch_rejected(completed_run):
    path = completed_run / 'trials.csv'
    contents = path.read_text()
    path.write_text(contents.replace('1900', '1800', 1))
    with pytest.raises(EvidenceError):
        analyze_replays([completed_run])


def test_unbalanced_and_incompatible_config_rejected(completed_run, tmp_path):
    fake, clock = FakeCluster(), Clock()
    second = tmp_path / 'second'
    run_experiment(fake, FeasibilityConfig(blocks=2, margin_cpu_m=80), second,
                   monotonic=clock, sleep=clock.sleep)
    with pytest.raises(EvidenceError, match='different workload parameters'):
        analyze_replays([completed_run, second])
