"""Deadline-bound Phase 2: fixed synthetic study and strict pooled real evidence."""
from __future__ import annotations

import csv
from dataclasses import replace
from pathlib import Path
import json
import pytest

from research.phase2_sensitivity import cases, CHANGE_TYPES, aggregate, run
from research.phase2_real_report import pool
from research.kube_wait_value import WaitValueConfig, run_experiment
from research.voi_study import MatchedCase
from test_kube_tie_crossover import Clock, FakeCluster


def two_fake_runs(tmp_path: Path):
    paths = []
    for number, seed in enumerate((20261116, 20261117), 1):
        path = tmp_path / f'run_{number:02}'
        clock = Clock()
        run_experiment(FakeCluster(), WaitValueConfig(repeats=2, seed=seed),
                       path, monotonic=clock, sleep=clock.sleep)
        paths.append(path)
    return tuple(paths)


def test_predeclared_matrix_unique_and_complete():
    matrix = cases()
    assert len(matrix) == 324
    assert len(set(k for k, _ in matrix)) == 324
    assert set(c.perturbed_worker for _, c in matrix) == {'worker-a', 'worker-b'}
    for label, changed, reservation in CHANGE_TYPES:
        part = [(k, c) for k, c in matrix if (c.change, c.reservation_cpu) == (changed, reservation)]
        assert len(part) == 108
        assert all(label in k for k, _ in part)
    assert {c.dispatch_delay for _, c in matrix} == {.2, .6}
    assert {c.max_wait for _, c in matrix} == {.5, 1.5, 3.}
    assert {c.telemetry_delay for _, c in matrix} == {.25, .75, 2.}
    assert {c.deadline for _, c in matrix} == {4., 5., 6.}


def test_minimal_sensitivity_outputs_auditable(tmp_path):
    matrix = cases()
    sampled = (matrix[0], matrix[54], matrix[108], matrix[216])
    output = tmp_path / 'study'
    report = run(output, sampled)
    assert report['cases'] == 4
    trials = list(csv.DictReader((output / 'trials.csv').open(encoding='utf-8')))
    assert len(trials) == 8
    pairs = list(csv.DictReader((output / 'paired.csv').open(encoding='utf-8')))
    assert len(pairs) == 4
    for key in {r['study_case_id'] for r in trials}:
        armrows = [r for r in trials if r['study_case_id'] == key]
        assert len(armrows) == 2
        assert len({r['input_sha256'] for r in armrows}) == 1
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['case_count'] == 4
    for name, digest in manifest['artifact_sha256'].items():
        from hashlib import sha256
        assert sha256((output / name).read_bytes()).hexdigest() == digest
    assert (output / 'sensitivity.png').read_bytes().startswith(b'\x89PNG')
    assert len((output / 'traces.jsonl').read_text(encoding='utf-8').splitlines()) == 4
    with pytest.raises(FileExistsError):
        run(output, sampled)


def test_full_sensitivity_counts_and_negative_controls(tmp_path):
    report = run(tmp_path / 'full')
    assert report['cases'] == 324
    assert report['policy_runs'] == 648
    zero = report['by_change_type']['unchanged']
    assert zero['paired_cases'] == 108
    assert zero['age_on_time'] == zero['bounded_on_time'] == 108
    assert zero['mean_extra_wait_seconds'] >= 0
    positive = report['by_change_type']['infeasible_change']
    assert positive['paired_cases'] == 108
    assert positive['bounded_on_time'] >= positive['age_on_time']
    feasible = report['by_change_type']['feasible_change']
    assert feasible['bounded_on_time'] == feasible['age_on_time']
    assert 'no inferential' in ' '.join(report['limitations'])


def test_duplicate_or_bad_sensitivity_case_rejected(tmp_path):
    c = cases()[0]
    with pytest.raises(ValueError, match='unique'):
        run(tmp_path/'dupe', (c, c))
    assert not (tmp_path/'dupe').exists()
    bad = (('wrong', replace(c[1], reservation_cpu=1.0)),)
    with pytest.raises(ValueError, match='declared'):
        run(tmp_path/'bad', bad)


@pytest.mark.parametrize('bad', [[], [{'level': 'a'}]])
def test_empty_or_partial_aggregate_rejected(bad):
    if bad:
        with pytest.raises(KeyError):
            aggregate(bad, 'change_type')
    else:
        with pytest.raises(ValueError, match='empty'):
            aggregate(bad, 'change_type')


def test_real_pool_complete_worker_balanced(tmp_path):
    paths = two_fake_runs(tmp_path)
    out = tmp_path/'pooled'
    report = pool(paths, out)
    assert report['runs'] == 2
    assert report['blocks'] == 8 and report['trials'] == 16
    assert report['worker_exposure'] == {'worker-a': 4, 'worker-b': 4}
    overall = next(r for r in report['descriptive'] if r['perturbed_worker'] == 'ALL')
    assert overall['bounded_on_time'] == 8
    assert overall['age_on_time'] == 4
    assert overall['bounded_better'] == 4
    assert overall['age_better'] == 0
    assert overall['ties'] == 4
    assert overall['mean_bounded_wait_seconds'] > 0
    assert len(list(csv.DictReader((out/'paired_blocks.csv').open(encoding='utf-8')))) == 8
    assert len(list(csv.DictReader((out/'audited_trials.csv').open(encoding='utf-8')))) == 16
    assert len(list(csv.DictReader((out/'worker_strata.csv').open(encoding='utf-8')))) == 3
    with pytest.raises(FileExistsError):
        pool(paths, out)


def test_real_pool_refuses_duplicate_paths(tmp_path):
    paths = two_fake_runs(tmp_path)
    with pytest.raises(ValueError, match='duplicate run path'):
        pool((paths[0], paths[0]), tmp_path/'pooled')


def test_real_pool_refuses_duplicate_seed(tmp_path):
    paths = two_fake_runs(tmp_path)
    mpath = paths[1]/'manifest.json'
    manifest = json.loads(mpath.read_text(encoding='utf-8'))
    manifest['config']['seed'] = 20261116
    mpath.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        pool(paths, tmp_path/'pooled')


def test_real_pool_refuses_changed_parameter(tmp_path):
    paths = two_fake_runs(tmp_path)
    mpath = paths[1]/'manifest.json'
    manifest = json.loads(mpath.read_text(encoding='utf-8'))
    manifest['config']['deadline_seconds'] = 21.0
    mpath.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        pool(paths, tmp_path/'pooled')


def test_real_pool_refuses_missing_trace(tmp_path):
    paths = two_fake_runs(tmp_path)
    next((paths[1]/'traces').glob('*.json')).unlink()
    with pytest.raises(FileNotFoundError):
        pool(paths, tmp_path/'pooled')


def test_real_pool_refuses_bad_row(tmp_path):
    paths = two_fake_runs(tmp_path)
    trials = paths[0]/'trials.csv'
    original = trials.read_text(encoding='utf-8')
    trials.write_text(original.replace('Succeeded', 'Falsified', 1), encoding='utf-8')
    with pytest.raises(ValueError, match='mismatch'):
        pool(paths, tmp_path/'pooled')


def test_real_pool_refuses_incomplete_runs(tmp_path):
    path = tmp_path/'short'
    clock = Clock()
    run_experiment(FakeCluster(), WaitValueConfig(repeats=1), path, monotonic=clock, sleep=clock.sleep)
    with pytest.raises(ValueError, match='two-repeat'):
        pool((path,), tmp_path/'pooled')


def test_real_pool_without_runs(tmp_path):
    with pytest.raises(ValueError, match='at least one'):
        pool((), tmp_path/'pooled')


def test_real_pool_invalid_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        pool((tmp_path/'missing',), tmp_path/'pooled')
