"""Offline report tests. Never contact any Kubernetes cluster."""
import csv
import json
from pathlib import Path

import pytest

from research.kube_feasibility_analysis import (
    EvidenceError, _bool, _nonnegative_float, _required_int,
    audit_runs, load_run, main, write_audit,
)
from research.kube_feasibility_pilot import FIELDS as PILOT_FIELDS
from research.kube_pilot import POLICIES, order_by_block


def create_fixture_run(path: Path, *, blocks: int = 1, seed: int = 10,
                       cpu_reason: str = '0/3 nodes are available: Insufficient cpu') -> Path:
    path.mkdir(parents=True)
    cfg = {'context': 'kind-edgepilot-v2', 'namespace': 'edgepilot-v2-validation',
           'blocks': blocks, 'seed': seed, 'deadline_seconds': 20.0,
           'competitor_cpu_m': 200, 'margin_cpu_m': 100,
           'competitor_hold_seconds': 60, 'sleep_seconds': 2}
    plan = order_by_block(blocks, seed)
    manifest = {'config': cfg, 'plan': [{'block': b, 'position': pos, 'policy': p}
                                       for b, pos, p in plan]}
    (path / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    (path / 'traces').mkdir()
    rows = []
    for block, position, policy in plan:
        target, peer = (('worker-a', 'worker-b') if block % 2 == 1
                        else ('worker-b', 'worker-a'))
        old_nodes = [{'name': 'worker-a', 'estimated_unreserved_cpu_cores': 2.,
                      'usable_for_model': True},
                     {'name': 'worker-b', 'estimated_unreserved_cpu_cores': 2.,
                      'usable_for_model': True}]
        fresh_nodes = [dict(node, estimated_unreserved_cpu_cores=(1.8 if node['name'] == target else 2.))
                       for node in old_nodes]
        old = {'measurement_window_start_monotonic': 100., 'receipt_monotonic': 101.,
               'node_inventory': old_nodes}
        fresh = {'measurement_window_start_monotonic': 102., 'receipt_monotonic': 103.,
                 'node_inventory': fresh_nodes}
        mixed = {target: {'measured_at': 100.}, peer: {'measured_at': 102.}}
        selected = target if policy == 'greedy' else (None if policy == 'uncertainty_aware' else peer)
        phase = ('TimedOutObservation' if policy == 'greedy' else
                 'NoEligibleNode' if policy == 'uncertainty_aware' else 'Succeeded')
        succeeded = phase == 'Succeeded'
        row = dict(block=block, position=position, policy=policy,
                   perturbed_worker=target, probe_cpu_m=1900,
                   selected_worker=selected, reference_worker=peer,
                   selected_differs_from_reference=(selected != peer),
                   selected_feasible_in_fresh_snapshot=(None if selected is None else selected == peer),
                   stale_target_feasible_in_old_snapshot=True,
                   stale_target_feasible_in_fresh_snapshot=False,
                   fresh_peer_feasible=True, target_age_seconds=3., peer_age_seconds=1.,
                   pre_arrival_staleness_delay_seconds=0., wait_seconds=(.8 if policy == 'bounded_deferral' else 0.),
                   refreshes=(1 if policy == 'bounded_deferral' else 0),
                   decision_elapsed_seconds=1., deadline_seconds=20.,
                   pod_name=f'probe-{block}-{policy}' if selected else None,
                   pod_actual_worker=(peer if succeeded else None), terminal_phase=phase,
                   container_exit_code=(0 if succeeded else None),
                   unschedulable_reason=(cpu_reason if policy == 'greedy' else None),
                   end_to_end_seconds=(4. if succeeded else 20. if selected else None),
                   on_time_observed=succeeded, probe_cleanup_confirmed=bool(selected),
                   competitor_cleanup_confirmed=True, error=None)
        trace = {'block': block, 'position': position, 'policy': policy,
                 'old_snapshot': old, 'fresh_snapshot': fresh,
                 'mixed_policy_input': mixed, 'result': row}
        (path / 'traces' / f'block{block:02d}_position{position:02d}_{policy}.json').write_text(
            json.dumps(trace), encoding='utf-8')
        rows.append(row)
    with (path / 'trials.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=PILOT_FIELDS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    return path


def change_json(path: Path, apply):
    value = json.loads(path.read_text(encoding='utf-8'))
    apply(value)
    path.write_text(json.dumps(value), encoding='utf-8')


def mutate_trace(path: Path, key: str, value):
    for p in (path / 'traces').glob('*.json'):
        if key == 'whatever' or p.name.endswith('greedy.json'):
            change_json(p, lambda v: v['result'].__setitem__(key, value))
            break


def first_trace(path: Path, policy: str = 'greedy'):
    return next((path / 'traces').glob(f'*_{policy}.json'))


def test_full_audit_sample(tmp_path):
    path = create_fixture_run(tmp_path / 'run')
    trials, audit = load_run(path)
    assert len(trials) == 4 and len(audit['sha256']) == 6
    result = audit_runs([path])
    assert result['trial_count'] == 4 and result['block_count'] == 1
    d = {r['policy']: r for r in result['policy_summaries']}
    assert d['greedy']['on_time'] == 0
    assert d['greedy']['fresh_infeasible_selections'] == 1
    assert d['greedy']['confirmed_insufficient_cpu'] == 1
    assert d['bounded_deferral']['on_time'] == 1
    assert d['age_penalized']['on_time'] == 1
    assert d['uncertainty_aware']['no_eligible_node'] == 1
    assert d['uncertainty_aware']['fresh_infeasible_selections'] == 0


def test_unconfirmed_timeout_does_not_prove_cpu_failure(tmp_path):
    path = create_fixture_run(tmp_path / 'run', cpu_reason='')
    result = audit_runs([path]); greedy = result['policy_summaries'][0]
    assert greedy['timed_out_observation'] == 1
    assert greedy['confirmed_insufficient_cpu'] == 0
    assert greedy['observed_timeout_without_cpu_evidence'] == 1


def test_two_blocks_worker_balance(tmp_path):
    path = create_fixture_run(tmp_path / 'run', blocks=2)
    report = audit_runs([path])
    assert report['trial_count'] == 8
    assert report['perturbation_target_blocks'] == {'worker-a': 1, 'worker-b': 1}
    assert len(report['by_block']) == 2
    assert all(values['delta_on_time_vs_greedy']['bounded_deferral'] == 1
               for values in report['by_block'].values())


def test_multiple_runs_same_basename(tmp_path):
    a = create_fixture_run(tmp_path / 'a' / 'run', blocks=2)
    b = create_fixture_run(tmp_path / 'b' / 'run', blocks=1, seed=11)
    result = audit_runs([a, b])
    assert len(result['by_block']) == 3
    assert result['trial_count'] == 12
    assert result['policy_summaries'][0]['trials'] == 3


def test_comparable_configurations(tmp_path):
    a = create_fixture_run(tmp_path / 'a')
    b = create_fixture_run(tmp_path / 'b')
    change_json(b / 'manifest.json', lambda v: v['config'].__setitem__('deadline_seconds', 21.))
    assert audit_runs([a,b])['comparable_configurations'] is False


def test_report_is_written_without_overwriting(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    report = audit_runs([run]); out = tmp_path / 'separate'
    write_audit(report, out, [run])
    assert json.loads((out / 'audit.json').read_text())['trial_count'] == 4
    with (out / 'policy_summary.csv').open(newline='') as f:
        assert len(list(csv.DictReader(f))) == 4
    with pytest.raises(FileExistsError):
        write_audit(report, out, [run])


def test_report_cannot_overwrite_evidence(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    report = audit_runs([run]); out = run / 'another-dir'
    with pytest.raises(EvidenceError):
        write_audit(report, out, [run])


def test_report_cannot_be_parent_of_evidence(tmp_path):
    run = create_fixture_run(tmp_path / 'parent' / 'run')
    with pytest.raises(EvidenceError):
        write_audit(audit_runs([run]), tmp_path / 'parent', [run])


def test_cli_offline(tmp_path, capsys):
    run = create_fixture_run(tmp_path / 'run')
    out = tmp_path / 'out'
    main(['--run', str(run), '--output', str(out)])
    assert 'Audited 4 real-cluster trial records' in capsys.readouterr().out
    assert (out / 'audit.json').is_file()


def test_missing_run(tmp_path):
    with pytest.raises(EvidenceError, match='does not exist'):
        audit_runs([tmp_path / 'missing'])


def test_empty_run_list():
    with pytest.raises(EvidenceError, match='at least one'):
        audit_runs([])


def test_duplicate_run(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    with pytest.raises(EvidenceError, match='duplicate'):
        audit_runs([run, run])


def test_missing_trace(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    first_trace(run).unlink()
    with pytest.raises(EvidenceError, match='valid JSON'):
        audit_runs([run])


def test_corrupt_json(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    (run / 'manifest.json').write_text('not json', encoding='utf-8')
    with pytest.raises(EvidenceError, match='valid JSON'):
        audit_runs([run])


def test_duplicate_manifest_plan_policy(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    change_json(run / 'manifest.json', lambda m: m['plan'][0].__setitem__('policy', 'greedy'))
    with pytest.raises(EvidenceError):
        audit_runs([run])


def test_missing_csv_trial(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    csvfile = run / 'trials.csv'
    csvfile.write_text('\n'.join(csvfile.read_text().splitlines()[:-1]) + '\n', encoding='utf-8')
    with pytest.raises(EvidenceError, match='CSV count'):
        audit_runs([run])


def test_swapped_csv_rows(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    file = run / 'trials.csv'
    lines = file.read_text(encoding='utf-8').splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    file.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    with pytest.raises(EvidenceError, match='violates recorded trial order'):
        audit_runs([run])


def test_trace_csv_mismatch(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    mutate_trace(run, 'probe_cpu_m', 1100)
    with pytest.raises(EvidenceError, match='CSV/trace mismatch'):
        audit_runs([run])


def test_unconfirmed_cleanup(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    trace_path = first_trace(run)
    change_json(trace_path, lambda t: t['result'].__setitem__('probe_cleanup_confirmed', False))
    with pytest.raises(EvidenceError):
        audit_runs([run])


def test_tampered_old_snapshot(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    p = first_trace(run)
    change_json(p, lambda t: t['old_snapshot']['node_inventory'][0].__setitem__(
        'estimated_unreserved_cpu_cores', 1.7))
    with pytest.raises(EvidenceError, match='old snapshot'):
        audit_runs([run])


def test_tampered_fresh_snapshot(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    p = first_trace(run)
    change_json(p, lambda t: t['fresh_snapshot']['node_inventory'][0].__setitem__(
        'estimated_unreserved_cpu_cores', 2.))
    with pytest.raises(EvidenceError, match='fresh target'):
        audit_runs([run])


def test_fabricated_timestamp_detected(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    p = first_trace(run)
    change_json(p, lambda t: t['mixed_policy_input']['worker-a'].__setitem__('measured_at', 101.))
    with pytest.raises(EvidenceError, match='fabricated timestamp'):
        audit_runs([run])


def test_malformed_snapshot_causal_order(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    p = first_trace(run)
    change_json(p, lambda t: t['old_snapshot'].__setitem__('receipt_monotonic', 104.))
    with pytest.raises(EvidenceError, match='causal order'):
        audit_runs([run])


def test_unverified_feasibility_boundary(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    p = first_trace(run)
    change_json(p, lambda t: t['result'].__setitem__('fresh_peer_feasible', False))
    with pytest.raises(EvidenceError):
        audit_runs([run])


def test_cross_policy_reference_mismatch(tmp_path):
    run = create_fixture_run(tmp_path / 'run')
    p = first_trace(run)
    change_json(p, lambda t: t['result'].__setitem__('reference_worker', 'worker-a'))
    with pytest.raises(EvidenceError):
        audit_runs([run])


@pytest.mark.parametrize('value', ['True', True, 'False', False])
def test_bool_parsing(value):
    assert _bool(value, 'b') is (str(value) == 'True')


@pytest.mark.parametrize('value', ['yes', 'true', 1, 0, None])
def test_invalid_boolean(value):
    with pytest.raises(EvidenceError):
        _bool(value, 'b')


@pytest.mark.parametrize('value', ['-1', 'nan', 'inf', '-inf', 'notfloat'])
def test_bad_float(value):
    with pytest.raises(EvidenceError):
        _nonnegative_float(value, 'latency')


@pytest.mark.parametrize('value', [True, 1.0, '-1', '1.0', 'x'])
def test_bad_integer(value):
    with pytest.raises(EvidenceError):
        _required_int(value, 'integer', minimum=1)
