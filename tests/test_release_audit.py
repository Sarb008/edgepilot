"""Phase 3: independently replay synthetic and real historical evidence."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from research.phase2_sensitivity import cases, run
from research.phase2_real_report import pool
from research.kube_wait_value import WaitValueConfig, run_experiment
from research.release_audit import (audit_real, audit_release, audit_synthetic,
                                    git_provenance)
from test_kube_tie_crossover import Clock, FakeCluster


@pytest.fixture
def tiny_synthetic(tmp_path: Path):
    selected = tuple(cases()[n] for n in (0, 54, 108, 162))
    path = tmp_path / 'synthetic'
    run(path, selected)
    return path, selected


@pytest.fixture
def tiny_real(tmp_path: Path):
    paths = []
    for n, seed in enumerate((20261116, 20261117)):
        directory = tmp_path / f'run_{n}'
        clock = Clock()
        run_experiment(FakeCluster(), WaitValueConfig(repeats=2, seed=seed),
                       directory, monotonic=clock, sleep=clock.sleep)
        paths.append(directory)
    report = tmp_path / 'real_report'
    pool(tuple(paths), report)
    return report, tuple(paths)


def test_replay_synthetic_exact_and_plot_attested(tiny_synthetic, tmp_path):
    original, selected = tiny_synthetic
    report = audit_synthetic(original, tmp_path / 'replay', selected=selected)
    assert report['status'] == 'passed'
    assert report['cases'] == 4 and report['policy_runs'] == 8
    assert report['csv_and_trace_byte_replay']
    assert report['original_plot_sha256_verified']


def test_synthetic_rejects_missing_plot(tiny_synthetic, tmp_path):
    original, selected = tiny_synthetic
    (original / 'sensitivity.png').unlink()
    with pytest.raises(FileNotFoundError):
        audit_synthetic(original, tmp_path / 'replay', selected=selected)


def test_synthetic_rejects_modified_csv_with_old_hash(tiny_synthetic, tmp_path):
    original, selected = tiny_synthetic
    path = original / 'paired.csv'
    path.write_text(path.read_text(encoding='utf-8').replace('worker-a', 'worker-z', 1), encoding='utf-8')
    with pytest.raises(ValueError, match='checksum mismatch'):
        audit_synthetic(original, tmp_path / 'replay', selected=selected)


def test_synthetic_rejects_modified_csv_even_if_hash_rewritten(tiny_synthetic, tmp_path):
    original, selected = tiny_synthetic
    path = original / 'paired.csv'
    path.write_text(path.read_text(encoding='utf-8').replace('worker-a', 'worker-z', 1), encoding='utf-8')
    manifest_path = original / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['artifact_sha256']['paired.csv'] = sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='nonreproducible'):
        audit_synthetic(original, tmp_path / 'replay', selected=selected)


def test_synthetic_rejects_missing_case_definition(tiny_synthetic, tmp_path):
    original, selected = tiny_synthetic
    manifest_path = original / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['cases'].pop()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='case definitions'):
        audit_synthetic(original, tmp_path / 'replay', selected=selected)


def test_synthetic_rejects_bad_summary(tiny_synthetic, tmp_path):
    original, selected = tiny_synthetic
    summary_path = original / 'summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    summary['by_change_type']['infeasible_change']['bounded_on_time'] = -1
    summary_path.write_text(json.dumps(summary), encoding='utf-8')
    with pytest.raises(ValueError, match='summary is not reproducible'):
        audit_synthetic(original, tmp_path / 'replay', selected=selected)


def test_repool_real_exact(tiny_real, tmp_path):
    original, runs = tiny_real
    report = audit_real(original, runs, tmp_path / 'repool', required_seeds=(20261116, 20261117))
    assert report['status'] == 'passed'
    assert report['runs'] == 2
    assert report['blocks'] == 8 and report['pod_trials'] == 16
    assert report['independent_repool_byte_exact']


def test_real_rejects_wrong_seed_order(tiny_real, tmp_path):
    original, runs = tiny_real
    with pytest.raises(ValueError, match='run order/seeds'):
        audit_real(original, runs[::-1], tmp_path / 'repool', required_seeds=(20261116, 20261117))


def test_real_rejects_modified_summary(tiny_real, tmp_path):
    original, runs = tiny_real
    path = original / 'summary.json'
    summary = json.loads(path.read_text(encoding='utf-8'))
    summary['descriptive'][-1]['age_on_time'] = -1
    path.write_text(json.dumps(summary), encoding='utf-8')
    with pytest.raises(ValueError, match='summary does not match'):
        audit_real(original, runs, tmp_path / 'repool', required_seeds=(20261116, 20261117))


def test_real_rejects_modified_raw_trace(tiny_real, tmp_path):
    original, runs = tiny_real
    first_trace = next((runs[0] / 'traces').glob('*.json'))
    record = json.loads(first_trace.read_text(encoding='utf-8'))
    record['result']['terminal_phase'] = 'Falsified'
    first_trace.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError):
        audit_real(original, runs, tmp_path / 'repool', required_seeds=(20261116, 20261117))


def test_release_gate_has_no_partial_output_when_fail(tiny_synthetic, tiny_real, tmp_path):
    synthetic, selected = tiny_synthetic
    real, sources = tiny_real
    output = tmp_path / 'released'
    with pytest.raises(ValueError, match='missing or extra frozen'):
        audit_release(synthetic, real, sources, output, synthetic_selected=selected)
    assert not output.exists()


def test_release_gate_writes_truthful_offline_artifacts(tiny_synthetic, tiny_real, tmp_path):
    synthetic, selected = tiny_synthetic
    real, sources = tiny_real
    output = tmp_path / 'gate'
    result = audit_release(synthetic, real, sources, output, root=tmp_path,
                           synthetic_selected=selected, required_seeds=(20261116, 20261117))
    assert result['gate'] == 'passed'
    assert result['real_cluster']['blocks'] == 8
    assert 'limitations' in result
    document = (output / 'RELEASE_GATE.md').read_text(encoding='utf-8')
    assert 'not' in document.lower() and 'Kubernetes' not in document[:20]
    assert 'sequential' in document.lower()
    assert json.loads((output / 'audit.json').read_text(encoding='utf-8'))['gate'] == 'passed'
    with pytest.raises(FileExistsError):
        audit_release(synthetic, real, sources, output, root=tmp_path,
                      synthetic_selected=selected, required_seeds=(20261116, 20261117))


def test_git_provenance_does_not_invent_missing_git(tmp_path):
    result = git_provenance(tmp_path)
    assert result['available'] is False
    assert 'head' not in result
