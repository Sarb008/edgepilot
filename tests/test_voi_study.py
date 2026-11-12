"""Matched-input, no-future-telemetry simulator controls (no cluster access)."""
from dataclasses import replace
import csv
import json

import pytest

from research.voi_study import (
    MatchedCase, POLICIES, export_study, paired_rows, predeclared_cases,
    run_pair, summary,
)


def test_fixed_factorial_is_full_and_unique():
    cases = predeclared_cases()
    assert len(cases) == 48
    assert len({c.case_id for c in cases}) == 48
    assert {c.perturbed_worker for c in cases} == {'worker-a', 'worker-b'}
    assert {c.change for c in cases} == {False, True}
    assert {c.telemetry_delay for c in cases} == {.25, .5, 1., 2.}
    assert {c.deadline for c in cases} == {4., 5., 6.}


@pytest.mark.parametrize('change', [False, True])
@pytest.mark.parametrize('worker', ['worker-a', 'worker-b'])
@pytest.mark.parametrize('delay', [.25, .5, 1., 2.])
def test_matched_first_observation_is_identical(change, worker, delay):
    rows, trace = run_pair(MatchedCase(worker, change, delay, 5.))
    assert len(rows) == 2
    assert rows[0]['input_sha256'] == rows[1]['input_sha256']
    assert rows[0]['policy'] == POLICIES[0]
    assert rows[1]['policy'] == POLICIES[1]
    inputs = [trace['arms'][policy]['decisions'][0]['samples'] for policy in POLICIES]
    assert inputs[0] == inputs[1]
    assert all(sample['measured_at'] == 0 for sample in inputs[0].values())
    assert all(sample['received_at'] == 0 for sample in inputs[0].values())
    assert all(sample['cpu_used'] == 0 for sample in inputs[0].values())
    for policy in POLICIES:
        for item in trace['arms'][policy]['decisions']:
            assert all(sample['received_at'] <= item['now'] for sample in item['samples'].values())


@pytest.mark.parametrize('delay', [.25, .5, 1.])
def test_informative_refresh_rescues_stale_capacity_failure(delay):
    pair, trace = run_pair(MatchedCase('worker-a', True, delay, 5.))
    age, bounded = pair
    assert age['status'] == 'capacity_failed'
    assert age['placement_failures'] == 1
    assert age['on_time'] == 0
    assert bounded['status'] == 'completed'
    assert bounded['selected_worker'] == 'worker-b'
    assert bounded['on_time'] == 1
    assert bounded['intentional_wait'] > 0
    assert trace['arms']['bounded_deferral']['decisions'][0]['action'] == 'wait'
    assert trace['arms']['age_penalized']['decisions'][0]['action'] == 'select'


@pytest.mark.parametrize('delay', [.25, .5, 1., 2.])
def test_no_change_adds_wait_without_completion_benefit(delay):
    age, bounded = run_pair(MatchedCase('worker-a', False, delay, 5.))[0]
    assert age['on_time'] == bounded['on_time'] == 1
    assert bounded['intentional_wait'] > age['intentional_wait']
    assert bounded['start'] > age['start']


def test_late_refresh_eliminates_completion_benefit():
    age, bounded = run_pair(MatchedCase('worker-a', True, 2., 5.))[0]
    assert age['status'] == bounded['status'] == 'capacity_failed'
    assert bounded['intentional_wait'] > 0


def test_other_worker_perturbation_makes_immediate_age_aware_successful():
    age, bounded = run_pair(MatchedCase('worker-b', True, 1., 5.))[0]
    assert age['on_time'] == 1
    assert bounded['on_time'] == 1
    assert age['selected_worker'] == bounded['selected_worker'] == 'worker-a'
    assert bounded['start'] > age['start']


def test_determinism_and_independent_runs():
    case = MatchedCase('worker-a', True, .5, 6.)
    assert run_pair(case) == run_pair(case)


def test_synthetic_primary_summary_and_sanity():
    rows = [row for case in predeclared_cases() for row in run_pair(case)[0]]
    pairs = paired_rows(rows)
    report = summary(pairs)
    assert len(rows) == 96 and len(pairs) == 48
    assert report['all']['cases'] == 48
    assert report['by_change']['0']['bounded_better_count'] == 0
    assert report['by_change']['0']['mean_extra_wait'] > 0
    assert report['by_change']['1']['bounded_better_count'] > 0
    assert report['all']['bounded_on_time'] > report['all']['age_on_time']
    assert 'NOT independent' in ' '.join(report['limitations'])


def test_exports_and_never_overwrites(tmp_path):
    dest = tmp_path / 'voi'
    cases = (MatchedCase('worker-a', True, .25, 5.), MatchedCase('worker-b', False, 1., 6.))
    r = export_study(dest, cases)
    assert r['all']['cases'] == 2
    assert len(list(csv.DictReader((dest / 'trials.csv').open(encoding='utf-8')))) == 4
    assert len(list(csv.DictReader((dest / 'paired.csv').open(encoding='utf-8')))) == 2
    assert len((dest / 'traces.jsonl').read_text(encoding='utf-8').splitlines()) == 2
    assert len(json.loads((dest / 'manifest.json').read_text(encoding='utf-8'))['cases']) == 2
    with pytest.raises(FileExistsError):
        export_study(dest, cases)


def test_pair_missing_arm_rejected():
    one = run_pair(MatchedCase('worker-a', False, .25, 5.))[0][0]
    with pytest.raises(ValueError, match='missing'):
        paired_rows([one])


def test_duplicate_or_mismatched_pair_rejected():
    rows, _ = run_pair(MatchedCase('worker-a', True, .25, 5.))
    with pytest.raises(ValueError, match='duplicate'):
        paired_rows(rows + [rows[0]])
    with pytest.raises(ValueError, match='mismatch'):
        paired_rows([rows[0], {**rows[1], 'input_sha256': 'tampered'}])


def test_export_refuses_duplicate_cases(tmp_path):
    case = MatchedCase('worker-b', True, .25, 5.)
    with pytest.raises(ValueError, match='unique'):
        export_study(tmp_path / 'bad', (case, case))
    assert not (tmp_path / 'bad').exists()


@pytest.mark.parametrize('change', [-1, 1, None, 'yes'])
def test_change_rejects_nonboolean(change):
    with pytest.raises(ValueError):
        MatchedCase('worker-a', change, .5, 5.)


@pytest.mark.parametrize('kw', [
    {'perturbed_worker': 'nope'}, {'telemetry_delay': 0},
    {'telemetry_delay': float('nan')}, {'deadline': float('inf')},
    {'deadline': 1.2}, {'workload_duration': 0},
    {'cpu_required': 0}, {'cpu_required': 3},
    {'reservation_cpu': 0}, {'reservation_cpu': 3},
    {'reservation_time': 1.2}, {'arrival': -1},
    {'max_wait': -1}, {'dispatch_delay': -1},
    {'freshness_threshold': -1},
])
def test_invalid_case_rejected(kw):
    with pytest.raises(ValueError):
        replace(MatchedCase('worker-a', True, .5, 5.), **kw)
