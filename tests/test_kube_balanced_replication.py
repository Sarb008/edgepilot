"""M6F: tests use fixture evidence and fake clients, NEVER a live cluster."""
from dataclasses import replace
import csv
import json
from pathlib import Path

import pytest

from research.kube_balanced_replication import (
    ReplicationConfig, execute, main as replication_main, planned_runs,
)
from research.kube_stratified_analysis import (
    analyze, export, main as strata_main, PAIR_FIELDS, STRATUM_FIELDS,
)
from research.kube_feasibility_analysis import EvidenceError
from research.kube_feasibility_pilot import run_experiment
from research.kube_pilot import POLICIES
from test_kube_feasibility_analysis import create_fixture_run, change_json
from test_kube_feasibility_pilot import FakeCluster, Clock


@pytest.mark.parametrize('kwargs', [
    {'repeats': 0}, {'repeats': -1}, {'repeats': 3}, {'repeats': True},
    {'repeats': 1.1}, {'repeats': '2'},
    {'seed': -1}, {'seed': True}, {'seed': 2**31-2}, {'seed': 1.1},
    {'deadline_seconds': float('nan')}, {'deadline_seconds': 10},
    {'deadline_seconds': float('inf')}, {'deadline_seconds': True},
    {'competitor_cpu_m': 600}, {'competitor_cpu_m': 99},
    {'margin_cpu_m': 200}, {'margin_cpu_m': 30},
])
def test_bad_replication_config(kwargs):
    with pytest.raises(ValueError):
        ReplicationConfig(**kwargs)


def test_run_config_deterministic_and_bounded():
    cfg = ReplicationConfig(repeats=2, seed=1234)
    assert cfg.run_config(0).blocks == cfg.run_config(1).blocks == 2
    assert cfg.run_config(0).seed == 1234
    assert cfg.run_config(1).seed == 1235
    with pytest.raises(ValueError):
        cfg.run_config(2)
    with pytest.raises(ValueError):
        cfg.run_config(True)


def test_planned_runs_full_cover_and_reproducibility():
    cfg = ReplicationConfig(repeats=2, seed=199)
    a = planned_runs(cfg)
    assert a == planned_runs(cfg)
    assert len(a) == 2
    assert [r['directory'] for r in a] == ['run_01', 'run_02']
    assert sum(len(r['policy_order']) for r in a) == 16
    assert all(len({t['policy'] for t in r['policy_order'] if t['block'] == block}) == 4
               for r in a for block in (1, 2))
    assert [r['seed'] for r in a] == [199, 200]


def test_replication_cli_dry_run_has_no_side_effects(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'pilot'
    def forbidden_client(*args, **kwargs):
        raise AssertionError('dry run accessed Kubernetes')
    monkeypatch.setattr('research.kube_balanced_replication.Kubectl', forbidden_client)
    replication_main(['--repeats', '2', '--seed', '3', '--output', str(path)])
    out = capsys.readouterr().out
    assert '16 trials' in out and '32 disposable Pods' in out
    assert 'DRY RUN' in out
    assert not path.exists()


def test_stratified_worker_specific_effect(tmp_path):
    run = create_fixture_run(tmp_path/'run', blocks=2)
    result = analyze([run])
    assert result['block_count'] == 2 and result['trial_count'] == 8
    assert result['worker_exposure'] == {'worker-a': 1, 'worker-b': 1}
    assert len(result['strata']) == 8
    assert len(result['block_pairs']) == 8
    by = {(r['perturbed_worker'], r['policy']): r for r in result['strata']}
    assert by['worker-a', 'greedy']['on_time'] == 0
    assert by['worker-b', 'greedy']['on_time'] == 0
    assert by['worker-a', 'bounded_deferral']['on_time'] == 1
    assert by['worker-b', 'age_penalized']['on_time'] == 1
    assert by['worker-a', 'bounded_deferral']['delta_on_time_vs_greedy_pp'] == 100.
    assert by['worker-a', 'uncertainty_aware']['refusals'] == 1
    assert by['worker-a', 'greedy']['recorded_insufficient_cpu'] == 1
    assert by['worker-a', 'greedy']['selected_perturbed_worker'] == 1


def test_strata_rejects_initial_unbalanced_pilot(tmp_path):
    initial = create_fixture_run(tmp_path/'initial', blocks=1)
    repeat = create_fixture_run(tmp_path/'repeat', blocks=2, seed=11)
    with pytest.raises(EvidenceError, match='balanced two-worker'):
        analyze([initial, repeat])


def test_strata_rejects_one_block(tmp_path):
    initial = create_fixture_run(tmp_path/'initial', blocks=1)
    with pytest.raises(EvidenceError, match='balanced two-worker'):
        analyze([initial])


def test_strata_accepts_multiple_balanced_runs(tmp_path):
    run1 = create_fixture_run(tmp_path/'run1', blocks=2)
    run2 = create_fixture_run(tmp_path/'run2', blocks=2, seed=11)
    result = analyze([run1, run2])
    assert result['worker_exposure'] == {'worker-a': 2, 'worker-b': 2}
    assert len(result['block_pairs']) == 16
    assert result['trial_count'] == 16 and result['block_count'] == 4
    assert all(row['trials'] == 2 for row in result['strata'])
    assert set(row['policy'] for row in result['strata']) == set(POLICIES)


def test_strata_rejects_incomparable_deadlines(tmp_path):
    a = create_fixture_run(tmp_path/'a', blocks=2)
    b = create_fixture_run(tmp_path/'b', blocks=2)
    change_json(b/'manifest.json', lambda d: d['config'].__setitem__('deadline_seconds', 21.))
    with pytest.raises(EvidenceError, match='different workload parameters'):
        analyze([a,b])


def test_strata_rejects_incomplete_trace(tmp_path):
    path = create_fixture_run(tmp_path/'broken', blocks=2)
    next((path/'traces').glob('*.json')).unlink()
    with pytest.raises(EvidenceError):
        analyze([path])


def test_export_separate_csv_files(tmp_path):
    path = create_fixture_run(tmp_path/'run', blocks=2)
    report = analyze([path]); output = tmp_path/'report'
    export(report, output, [path])
    with (output/'worker_strata.csv').open(newline='', encoding='utf-8') as stream:
        reader = csv.DictReader(stream)
        assert tuple(reader.fieldnames) == STRATUM_FIELDS
        assert len(list(reader)) == 8
    with (output/'block_pairs.csv').open(newline='', encoding='utf-8') as stream:
        reader = csv.DictReader(stream)
        assert tuple(reader.fieldnames) == PAIR_FIELDS
        assert len(list(reader)) == 8
    assert json.loads((output/'summary.json').read_text())['block_count'] == 2
    with pytest.raises(FileExistsError):
        export(report, output, [path])


@pytest.mark.parametrize('place', ['inside', 'parent', 'same'])
def test_export_refuses_evidence_overlap(tmp_path, place):
    path = create_fixture_run(tmp_path/'parent'/'run', blocks=2)
    result = analyze([path])
    output = {'inside': path/'report', 'parent': tmp_path/'parent', 'same': path}[place]
    with pytest.raises(EvidenceError, match='separate'):
        export(result, output, [path])


def test_offline_strata_cli_no_cluster(tmp_path, monkeypatch, capsys):
    path = create_fixture_run(tmp_path/'run', blocks=2)
    output = tmp_path/'analysis'
    monkeypatch.setattr('research.kube_stratified_analysis.Kubectl', None, raising=False)
    strata_main(['--run', str(path), '--output', str(output)])
    assert '8 policy trials' in capsys.readouterr().out
    assert (output/'worker_strata.csv').is_file()


def _fake_runner(fake_clock):
    def call(client, cfg, path):
        return run_experiment(client, cfg, path, monotonic=fake_clock, sleep=fake_clock.sleep)
    return call


def test_execute_fake_cluster_end_to_end(tmp_path):
    fake, clock = FakeCluster(), Clock()
    cfg = ReplicationConfig(repeats=2, seed=2023)
    result = execute(fake, cfg, tmp_path/'complete', runner=_fake_runner(clock))
    assert result['block_count'] == 4
    assert result['worker_exposure'] == {'worker-a': 2, 'worker-b': 2}
    assert len(result['block_pairs']) == 16
    assert (tmp_path/'complete'/'stratified_report'/'worker_strata.csv').is_file()
    manifest = json.loads((tmp_path/'complete'/'replication_manifest.json').read_text())
    assert manifest['status'] == 'complete'
    assert manifest['completed_runs'] == ['run_01', 'run_02']
    assert fake.competitor is None and fake.probe is None
    assert len([op for op in fake.ops if op[:2] == ('call', 'create')]) <= 32


def test_execute_fails_closed_when_namespace_not_empty(tmp_path):
    fake = FakeCluster(); fake.namespace_pods = True
    path = tmp_path/'not-created'
    with pytest.raises(Exception, match='ZERO Pods'):
        execute(fake, ReplicationConfig(), path)
    assert not path.exists()


def test_execute_preserves_existing_folder(tmp_path):
    fake = FakeCluster()
    path = tmp_path/'existing'
    path.mkdir(); (path/'evidence.txt').write_text('do not overwrite')
    with pytest.raises(FileExistsError):
        execute(fake, ReplicationConfig(), path)
    assert (path/'evidence.txt').read_text() == 'do not overwrite'
    assert fake.ops == []


def test_execute_aborts_on_cluster_mismatch_before_creating_output(tmp_path):
    fake = FakeCluster()
    def bad_snapshot(client):
        return {'node_inventory': []}
    path = tmp_path/'output'
    with pytest.raises(Exception, match='exactly 2'):
        execute(fake, ReplicationConfig(), path, snapshot=bad_snapshot)
    assert not path.exists()


def test_execute_preserves_partial_evidence_on_failure(tmp_path):
    fake = FakeCluster(); clock=Clock()
    count = []
    def failing_second(client, cfg, path):
        count.append(path)
        if len(count) == 2:
            raise RuntimeError('deliberate fixture interruption')
        return run_experiment(client, cfg, path, monotonic=clock, sleep=clock.sleep)
    path = tmp_path/'partial'
    with pytest.raises(RuntimeError, match='deliberate fixture interruption'):
        execute(fake, ReplicationConfig(repeats=2), path, runner=failing_second)
    manifest = json.loads((path/'replication_manifest.json').read_text())
    assert manifest['status'] == 'incomplete'
    assert manifest['completed_runs'] == ['run_01']
    assert (path/'run_01'/'trials.csv').exists()
    assert not (path/'stratified_report').exists()


def test_execute_fail_if_audit_detects_partial_run(tmp_path):
    fake = FakeCluster()
    def fake_corrupt(client,cfg,path):
        path.mkdir(); (path/'trials.csv').write_text('bad')
    path = tmp_path/'out'
    with pytest.raises(EvidenceError):
        execute(fake, ReplicationConfig(), path, runner=fake_corrupt)
    assert json.loads((path/'replication_manifest.json').read_text())['status'] == 'incomplete'
