"""No real-cluster access; reuse M6I fake two-worker Kubernetes interface."""
import csv
import json
import pytest

from test_kube_tie_crossover import Clock, FakeCluster
from research.kube_wait_value import (
    ARMS, WaitValueConfig, common_old_samples, main, plan_for,
    run_experiment, run_trial, summarize,
)
from research.kube_wait_value_analysis import audit, export_audit
from research.kube_validation import KubectlError
from research.models import Workload


def fake_trial(cfg, rep, block, pos, arm, fake=None):
    clock = Clock()
    return run_trial(fake or FakeCluster(), arm, cfg, repeat=rep, block=block,
                     position=pos, monotonic=clock, sleep=clock.sleep)


@pytest.mark.parametrize('kwargs', [
    {'context': 'production'}, {'namespace': 'default'}, {'repeats': 0},
    {'repeats': 3}, {'repeats': True}, {'seed': '20261116'},
    {'deadline_seconds': 10}, {'deadline_seconds': float('nan')},
    {'margin_cpu_m': 250}, {'competitor_cpu_m': 50},
    {'competitor_hold_seconds': 10}, {'min_stale_age_seconds': -1},
])
def test_safety_config(kwargs):
    with pytest.raises(ValueError):
        WaitValueConfig(**kwargs)


def test_balanced_two_arm_plan():
    cfg = WaitValueConfig(repeats=2)
    plan = plan_for(cfg)
    assert len(plan) == 8
    assert plan == plan_for(cfg)
    for block in range(1, 5):
        assert {p[-1] for p in plan if p[1] == block} == set(ARMS)
    for target_slot in (0, 1):
        subset = [p for p in plan if (p[1] - 1) % 2 == target_slot]
        for arm in ARMS:
            assert {p[2] for p in subset if p[3] == arm} == {1, 2}


def test_dry_run_does_not_connect_or_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('research.kube_wait_value.Kubectl',
                        lambda _: pytest.fail('dry run contacted Kubernetes'))
    dest = tmp_path / 'no_side_effects'
    main(['--repeats', '2', '--output', str(dest)])
    assert not dest.exists()
    assert 'DRY RUN' in capsys.readouterr().out


@pytest.mark.parametrize('block,expected_node', [(1, 'worker-b'), (2, 'worker-a')])
def test_bounded_waits_and_recovers_actual_feasibility(block, expected_node):
    cfg = WaitValueConfig()
    rep, _, pos, arm = next(p for p in plan_for(cfg) if p[1] == block and p[3] == 'bounded_deferral')
    row, trace = fake_trial(cfg, rep, block, pos, arm)
    assert row['initial_action'] == 'wait'
    assert row['initial_reported_cpu_tie'] is True
    assert row['initial_equal_measurement_age'] is True
    assert row['selected_worker'] == expected_node
    assert row['selected_feasible_in_treatment'] is True
    assert row['on_time_observed'] is True
    assert row['wait_seconds'] > 0 and row['refreshes'] > 0
    assert row['competitor_cleanup_confirmed'] and row['probe_cleanup_confirmed']
    assert all(s['cpu_used'] == 0 for s in trace['initial_policy_input'].values())


@pytest.mark.parametrize('block,phase', [(1, 'TimedOutObservation'), (2, 'Succeeded')])
def test_immediate_age_penalized_on_common_old_input(block, phase):
    cfg = WaitValueConfig()
    rep, _, pos, arm = next(p for p in plan_for(cfg) if p[1] == block and p[3] == 'age_penalized')
    row, _ = fake_trial(cfg, rep, block, pos, arm)
    assert row['initial_action'] == 'select'
    assert row['initial_worker'] == 'worker-a'
    assert row['terminal_phase'] == phase
    assert row['wait_seconds'] == 0 and row['refreshes'] == 0
    assert row['competitor_cleanup_confirmed'] and row['probe_cleanup_confirmed']


def test_run_and_offline_audit(tmp_path):
    cfg = WaitValueConfig(repeats=2)
    clock = Clock()
    fake = FakeCluster()
    output = tmp_path / 'actual_protocol_simulated'
    rows = run_experiment(fake, cfg, output, monotonic=clock, sleep=clock.sleep)
    assert len(rows) == 8
    assert fake.competitor is fake.probe is None
    assert len(list(csv.DictReader((output / 'trials.csv').open(encoding='utf-8')))) == 8
    audited, report = audit(output)
    assert len(audited) == 8
    assert report['worker_exposure'] == {'worker-a': 2, 'worker-b': 2}
    assert report['by_arm']['bounded_deferral']['on_time'] == 4
    assert report['by_arm']['age_penalized']['on_time'] == 2
    dest = tmp_path / 'audit'
    export_audit(output, dest)
    assert (dest / 'audit.json').exists()
    assert len(list(csv.DictReader((dest / 'audited_trials.csv').open(encoding='utf-8')))) == 8
    with pytest.raises(FileExistsError):
        export_audit(output, dest)
    with pytest.raises(FileExistsError):
        run_experiment(FakeCluster(), cfg, output, monotonic=Clock(), sleep=Clock().sleep)


def test_failure_cleanup_is_still_required():
    cfg = WaitValueConfig()
    rep, block, pos, arm = plan_for(cfg)[0]
    fake = FakeCluster()
    fake.fail_probe_create = True
    row, trace = fake_trial(cfg, rep, block, pos, arm, fake)
    assert row['terminal_phase'] == 'ProbeError'
    assert row['competitor_cleanup_confirmed'] is True
    assert fake.competitor is None


def test_cleanup_failure_is_fail_closed(tmp_path):
    cfg = WaitValueConfig()
    fake = FakeCluster()
    fake.cleanup_failed = True
    clock = Clock()
    with pytest.raises(KubectlError, match='cleanup'):
        run_experiment(fake, cfg, tmp_path/'incomplete', monotonic=clock, sleep=clock.sleep)
    assert (tmp_path/'incomplete'/'trials.csv').exists()  # keep diagnostic partial evidence


def test_both_samples_source_ages_must_match():
    cfg = WaitValueConfig()
    fake = FakeCluster()
    clock = Clock()
    from research.kube_validation import capture_snapshot
    old = capture_snapshot(fake, monotonic=clock)
    samples = common_old_samples(old, 1900, clock(), Workload('j', 101, 121, 1.9, 2))
    assert len(samples) == 2


def test_trace_tampering_is_detected(tmp_path):
    cfg = WaitValueConfig()
    clock = Clock()
    src = tmp_path/'source'
    run_experiment(FakeCluster(), cfg, src, monotonic=clock, sleep=clock.sleep)
    trace_path = next((src/'traces').glob('*.json'))
    trace = json.loads(trace_path.read_text(encoding='utf-8'))
    trace['result']['on_time_observed'] = not trace['result']['on_time_observed']
    trace_path.write_text(json.dumps(trace), encoding='utf-8')
    with pytest.raises(ValueError, match='mismatch'):
        audit(src)


def test_corrupted_manifest_plan_rejected(tmp_path):
    cfg = WaitValueConfig()
    clock = Clock()
    src = tmp_path/'source'
    run_experiment(FakeCluster(), cfg, src, monotonic=clock, sleep=clock.sleep)
    p = src/'manifest.json'
    obj = json.loads(p.read_text(encoding='utf-8'))
    obj['plan'][0]['position'] = 99
    p.write_text(json.dumps(obj), encoding='utf-8')
    with pytest.raises(ValueError, match='plan'):
        audit(src)


def test_recorded_source_timestamp_tampering_rejected(tmp_path):
    cfg = WaitValueConfig()
    clock = Clock()
    src = tmp_path/'source'
    run_experiment(FakeCluster(), cfg, src, monotonic=clock, sleep=clock.sleep)
    trace_path = next((src/'traces').glob('*.json'))
    trace = json.loads(trace_path.read_text(encoding='utf-8'))
    first = next(iter(trace['initial_policy_input']))
    trace['initial_policy_input'][first]['measured_at'] += 99
    trace_path.write_text(json.dumps(trace), encoding='utf-8')
    with pytest.raises(ValueError, match='initial policy input'):
        audit(src)


def test_summary_cannot_count_refusal_as_success():
    rows = [{'arm':'age_penalized','terminal_phase':'NoEligibleNode','on_time_observed':False},
            {'arm':'bounded_deferral','terminal_phase':'TimedOutObservation','on_time_observed':False}]
    s = summarize(rows)
    assert all(x['on_time'] == 0 for x in s)
