"""No real Kubernetes access. Fake two-worker cluster with simulated CPU requests."""
import csv
import json
from pathlib import Path

import pytest

from research.kube_tie_crossover import (
    ARMS, CrossoverConfig, greedy_tie_candidates, greedy_tie_select,
    main, plan_for, run_experiment, run_trial, summarize,
)
from research.kube_stale_pilot import NAMESPACE
from research.kube_validation import KubectlError
from research.models import TelemetrySample, Workload


class Clock:
    def __init__(self):
        self.value = 100.

    def __call__(self):
        self.value += .02
        return self.value

    def sleep(self, duration):
        self.value += duration


class FakeCluster:
    def __init__(self):
        self.ops = []
        self.competitor = None
        self.probe = None
        self.namespace_pods = False
        self.cleanup_failed = False
        self.fail_probe_create = False
        self.return_wrong_binding = False
        self.overwrite_perturbation = False
        self.unequal_baseline = False

    def verify_context(self):
        self.ops.append(('context-ok',))

    def get_json(self, *args):
        self.ops.append(('get', *args))
        if args == ('namespace', NAMESPACE):
            return {'metadata': {'name': NAMESPACE}}
        if args == ('pods', '-n', NAMESPACE):
            return {'items': ([{'status': {'phase': 'Running'}}]
                              if self.namespace_pods or self.competitor or self.probe else [])}
        if args == ('nodes',):
            return {'items': [
                {'metadata': {'name': worker, 'labels': {'kubernetes.io/hostname': worker}},
                 'spec': {'taints': []},
                 'status': {'allocatable': {'cpu': '2'},
                            'conditions': [{'type': 'Ready', 'status': 'True'}]}}
                for worker in ('worker-a', 'worker-b')]}
        if args == ('pods', '--all-namespaces'):
            pods = []
            if self.competitor and not self.overwrite_perturbation:
                pods.append({
                    'spec': {'nodeName': self.competitor['spec']['nodeSelector']['kubernetes.io/hostname'],
                             'containers': self.competitor['spec']['containers']},
                    'status': {'phase': 'Running'},
                })
            if self.unequal_baseline:
                pods.append({'spec': {'nodeName': 'worker-b', 'containers': [
                    {'resources': {'requests': {'cpu': '50m'}}}]}, 'status': {'phase': 'Running'}})
            if self.probe:
                pods.append({'spec': {
                    'nodeName': self.probe['spec']['nodeSelector']['kubernetes.io/hostname'],
                    'containers': self.probe['spec']['containers']},
                    'status': {'phase': 'Pending'}})
            return {'items': pods}
        if args[:1] == ('pod',):
            name = args[1]
            if self.competitor and name == self.competitor['metadata']['name']:
                expected = self.competitor['spec']['nodeSelector']['kubernetes.io/hostname']
                return {'spec': {'nodeName': ('worker-b' if self.return_wrong_binding else expected)},
                        'status': {'phase': 'Running'}}
            if self.probe and name == self.probe['metadata']['name']:
                expected = self.probe['spec']['nodeSelector']['kubernetes.io/hostname']
                req = int(self.probe['spec']['containers'][0]['resources']['requests']['cpu'][:-1])
                reserved = 0
                if (self.competitor and
                        self.competitor['spec']['nodeSelector']['kubernetes.io/hostname'] == expected):
                    reserved = int(self.competitor['spec']['containers'][0]['resources']['requests']['cpu'][:-1])
                if reserved + req > 2000:
                    return {'metadata': {'creationTimestamp': '2026-11-12T00:00:00Z'},
                            'spec': {'nodeName': None}, 'status': {'phase': 'Pending',
                            'conditions': [{'type': 'PodScheduled', 'status': 'False',
                                            'reason': 'Unschedulable', 'message': 'Insufficient cpu'}]}}
                return {'metadata': {'creationTimestamp': '2026-11-12T00:00:00Z'},
                        'spec': {'nodeName': expected},
                        'status': {'phase': 'Succeeded', 'containerStatuses': [
                            {'state': {'terminated': {'startedAt': '2026-11-12T00:00:01Z',
                                                      'finishedAt': '2026-11-12T00:00:03Z',
                                                      'exitCode': 0}}}]}}
            raise KubectlError('NotFound')
        raise AssertionError(args)

    def call(self, *args, stdin=None):
        self.ops.append(('call', *args))
        if args[0] == 'create':
            manifest = json.loads(stdin)
            role = manifest['metadata']['labels']['edgepilot-v2-role']
            if role == 'reservation':
                self.competitor = manifest
            elif role == 'feasibility-probe':
                if self.fail_probe_create:
                    raise KubectlError('ambiguous create failure')
                self.probe = manifest
            else:
                raise AssertionError(role)
            return ''
        if args[0] == 'delete':
            if self.cleanup_failed:
                raise KubectlError('delete failure')
            name = args[2]
            if self.competitor and self.competitor['metadata']['name'] == name:
                self.competitor = None
            if self.probe and self.probe['metadata']['name'] == name:
                self.probe = None
            return ''
        raise AssertionError(args)


def make_samples(a=2.0, b=2.0):
    return {'worker-a': TelemetrySample('worker-a', 20., 20., 2., 2.-a),
            'worker-b': TelemetrySample('worker-b', 20., 20., 2., 2.-b)}


def make_job(required=1.9):
    return Workload('job', 50., 75., required, 2.)


@pytest.mark.parametrize('kwargs', [
    {'context': 'production'}, {'namespace': 'default'}, {'repeats': 0},
    {'repeats': 3}, {'repeats': True}, {'seed': '2026'},
    {'deadline_seconds': float('nan')}, {'deadline_seconds': 14},
    {'competitor_cpu_m': 0}, {'competitor_cpu_m': 501},
    {'margin_cpu_m': 200}, {'margin_cpu_m': -1},
    {'competitor_hold_seconds': 120}, {'sleep_seconds': 9},
    {'min_stale_age_seconds': float('nan')},
])
def test_config_unsafe_inputs(kwargs):
    with pytest.raises(ValueError):
        CrossoverConfig(**kwargs)


def test_exact_greedy_tie_variants_are_pure():
    samples = make_samples()
    job = make_job()
    assert greedy_tie_candidates(job, samples) == ['worker-a', 'worker-b']
    assert greedy_tie_select(job, samples, 'greedy_lex') == 'worker-a'
    assert greedy_tie_select(job, samples, 'greedy_reverse') == 'worker-b'
    assert samples['worker-a'].available_cpu == 2.0  # not mutated


def test_greedy_uses_max_not_reverse_unconditionally():
    samples = make_samples(a=2.0, b=1.95)
    assert greedy_tie_candidates(make_job(), samples) == ['worker-a']
    assert greedy_tie_select(make_job(), samples, 'greedy_reverse') == 'worker-a'
    assert greedy_tie_select(make_job(2.01), samples, 'greedy_lex') is None
    with pytest.raises(ValueError):
        greedy_tie_select(make_job(), samples, 'uncertainty_aware')


def test_plan_balances_worker_and_position_and_is_reproducible():
    cfg = CrossoverConfig(repeats=2)
    plan = plan_for(cfg)
    assert len(plan) == 16 and plan == plan_for(cfg)
    assert plan != plan_for(CrossoverConfig(repeats=2, seed=4))
    for block in range(1, 5):
        assert set(arm for _, b, _, arm in plan if b == block) == set(ARMS)
        assert [p for _, b, p, _ in plan if b == block] == [1, 2, 3, 4]
    assert set((p for _, _, p, arm in plan if arm == 'greedy_lex')) == {1, 2, 3, 4}
    assert all(repeat == (block-1)//2+1 for repeat, block, *_ in plan)


def test_dry_run_creates_no_files_or_cluster(capsys, tmp_path):
    dest = tmp_path / 'no-files'
    main(['--repeats', '1', '--output', str(dest)])
    assert 'DRY RUN' in capsys.readouterr().out
    assert not dest.exists()


@pytest.mark.parametrize('arm,block,worker,feasible,succeeded', [
    ('greedy_lex', 1, 'worker-a', False, False),
    ('greedy_reverse', 1, 'worker-b', True, True),
    ('age_penalized', 1, 'worker-b', True, True),
    ('bounded_deferral', 1, 'worker-b', True, True),
    ('greedy_lex', 2, 'worker-a', True, True),
    ('greedy_reverse', 2, 'worker-b', False, False),
    ('age_penalized', 2, 'worker-a', True, True),
    ('bounded_deferral', 2, 'worker-a', True, True),
])
def test_realistic_feasibility_boundary_with_fake_cluster(arm, block, worker, feasible, succeeded):
    client, clock = FakeCluster(), Clock()
    row, trace = run_trial(client, arm, CrossoverConfig(), repeat=1, block=block,
                           position=1, monotonic=clock, sleep=clock.sleep)
    assert row['greedy_exact_top_tie'] is True
    assert row['greedy_tie_candidates'] == 'worker-a;worker-b'
    assert row['selected_worker'] == worker
    assert row['selected_feasible_in_treatment_snapshot'] is feasible
    assert row['on_time_observed'] is succeeded
    assert row['terminal_phase'] == ('Succeeded' if succeeded else 'TimedOutObservation')
    assert row['probe_cpu_m'] == 1900
    assert row['competitor_cleanup_confirmed'] and row['probe_cleanup_confirmed']
    assert client.competitor is None and client.probe is None
    assert 'old_snapshot' in trace and 'fresh_snapshot' in trace and 'mixed_policy_input' in trace
    assert row['reference_worker'] == ('worker-b' if block == 1 else 'worker-a')
    if arm == 'bounded_deferral':
        assert row['initial_action'] == 'wait' and row['wait_seconds'] > 0 and row['refreshes'] > 0
    else:
        assert row['initial_action'] == 'select' and row['wait_seconds'] == 0


def test_no_tie_fails_closed_and_cleans_reservation():
    client, clock = FakeCluster(), Clock()
    client.unequal_baseline = True
    row, _ = run_trial(client, 'greedy_reverse', CrossoverConfig(),
                       repeat=1, block=1, position=1, monotonic=clock, sleep=clock.sleep)
    assert row['terminal_phase'] == 'ProbeError'
    assert 'tie absent' in row['error']
    assert row['competitor_cleanup_confirmed']
    assert client.competitor is None and client.probe is None


def test_probe_create_error_still_cleanup():
    client, clock = FakeCluster(), Clock()
    client.fail_probe_create = True
    row, _ = run_trial(client, 'age_penalized', CrossoverConfig(),
                       repeat=1, block=1, position=1, monotonic=clock, sleep=clock.sleep)
    assert row['terminal_phase'] == 'ProbeError'
    assert row.get('error')
    assert row['probe_cleanup_confirmed'] and row['competitor_cleanup_confirmed']
    assert client.competitor is None


@pytest.mark.parametrize('fault', ['return_wrong_binding', 'overwrite_perturbation'])
def test_invalid_treatment_fails_closed_and_cleans(fault):
    client, clock = FakeCluster(), Clock()
    setattr(client, fault, True)
    row, _ = run_trial(client, 'greedy_lex', CrossoverConfig(),
                       repeat=1, block=1, position=1, monotonic=clock, sleep=clock.sleep)
    assert row['terminal_phase'] == 'ProbeError'
    assert row['competitor_cleanup_confirmed']
    assert client.competitor is None


def test_does_not_overwrite_existing_output(tmp_path):
    client = FakeCluster()
    output = tmp_path/'used'
    output.mkdir()
    with pytest.raises(FileExistsError):
        run_experiment(client, CrossoverConfig(), output)
    assert not client.competitor


def test_nonempty_namespace_fails_before_file_write(tmp_path):
    client = FakeCluster()
    client.namespace_pods = True
    output = tmp_path/'blocked'
    with pytest.raises(KubectlError):
        run_experiment(client, CrossoverConfig(), output)
    assert not output.exists()


def test_full_two_block_plan_data_and_cleanup(tmp_path):
    client, clock = FakeCluster(), Clock()
    output = tmp_path/'prospective'
    rows = run_experiment(client, CrossoverConfig(), output,
                          monotonic=clock, sleep=clock.sleep)
    assert len(rows) == 8
    assert len(list((output/'traces').glob('*.json'))) == 8
    assert len(list(csv.DictReader((output/'trials.csv').open(newline='')))) == 8
    plan = json.loads((output/'manifest.json').read_text())['plan']
    assert len(plan) == 8
    assert {row['perturbed_worker'] for row in rows} == {'worker-a', 'worker-b'}
    summaries = {r['arm']: r for r in summarize(rows)}
    assert summaries['greedy_lex']['on_time'] == 1
    assert summaries['greedy_reverse']['on_time'] == 1
    assert summaries['age_penalized']['on_time'] == 2
    assert summaries['bounded_deferral']['on_time'] == 2
    assert client.competitor is None and client.probe is None


def test_cleanup_failure_stops_experiment(tmp_path):
    client, clock = FakeCluster(), Clock()
    client.cleanup_failed = True
    output = tmp_path/'failed'
    with pytest.raises(KubectlError):
        run_experiment(client, CrossoverConfig(), output,
                       monotonic=clock, sleep=clock.sleep)
    assert len(list((output/'traces').glob('*.json'))) == 1
    assert (output/'trials.csv').is_file()


@pytest.mark.parametrize('arm,repeat,block,position', [
    ('garbage', 1, 1, 1), ('greedy_lex', 0, 1, 1), ('greedy_lex', 1, 3, 1),
    ('greedy_lex', 1, 2, 5), ('greedy_lex', 2, 1, 1),
])
def test_rejects_invalid_trial_plan(arm, repeat, block, position):
    with pytest.raises(ValueError):
        run_trial(FakeCluster(), arm, CrossoverConfig(), repeat=repeat,
                  block=block, position=position)


def complete_fake_run(tmp_path):
    client, clock = FakeCluster(), Clock()
    run = tmp_path/'run'
    run_experiment(client, CrossoverConfig(), run, monotonic=clock, sleep=clock.sleep)
    return run


def test_offline_audit_fake_run_and_export(tmp_path):
    from research.kube_tie_crossover_analysis import analyze, export
    run = complete_fake_run(tmp_path)
    report = analyze([run])
    assert report['trial_count'] == 8
    assert len(report['worker_strata']) == 8
    data = { (r['perturbed_worker'], r['arm']): r for r in report['worker_strata'] }
    assert data['worker-a', 'greedy_lex']['treatment_infeasible'] == 1
    assert data['worker-a', 'greedy_reverse']['on_time'] == 1
    assert data['worker-b', 'greedy_lex']['on_time'] == 1
    assert data['worker-b', 'greedy_reverse']['treatment_infeasible'] == 1
    assert data['worker-a', 'bounded_deferral']['initial_waits'] == 1
    assert report['evidence'][0]['sha256']['trials.csv']
    export(report, tmp_path/'analysis', [run])
    assert (tmp_path/'analysis'/'summary.json').exists()
    assert len(list(csv.DictReader((tmp_path/'analysis'/'worker_strata.csv').open()))) == 8


def test_offline_audit_rejects_duplicate_or_missing_run(tmp_path):
    from research.kube_tie_crossover_analysis import analyze, CrossoverEvidenceError
    run = complete_fake_run(tmp_path)
    with pytest.raises(CrossoverEvidenceError, match='same run'):
        analyze([run, run])
    with pytest.raises(CrossoverEvidenceError):
        analyze([tmp_path/'missing'])
    with pytest.raises(CrossoverEvidenceError):
        analyze([])


def test_offline_audit_detects_csv_trace_mismatch(tmp_path):
    from research.kube_tie_crossover_analysis import analyze, CrossoverEvidenceError
    run = complete_fake_run(tmp_path)
    src = run/'trials.csv'
    src.write_text(src.read_text().replace('greedy_lex', 'greedy_reverse', 1))
    with pytest.raises(CrossoverEvidenceError):
        analyze([run])


def test_offline_audit_detects_tampered_outcome(tmp_path):
    from research.kube_tie_crossover_analysis import analyze, CrossoverEvidenceError
    run = complete_fake_run(tmp_path)
    filename = next((run/'traces').glob('*greedy_lex.json'))
    d = json.loads(filename.read_text())
    d['result']['on_time_observed'] = not d['result']['on_time_observed']
    filename.write_text(json.dumps(d))
    with pytest.raises(CrossoverEvidenceError, match='CSV/trace mismatch'):
        analyze([run])


def test_offline_audit_detects_tampered_tie_input(tmp_path):
    from research.kube_tie_crossover_analysis import analyze, CrossoverEvidenceError
    run = complete_fake_run(tmp_path)
    filename = next((run/'traces').glob('*greedy_lex.json'))
    d = json.loads(filename.read_text())
    d['mixed_policy_input']['worker-a']['cpu_used'] = 0.5
    filename.write_text(json.dumps(d))
    with pytest.raises(CrossoverEvidenceError):
        analyze([run])


def test_offline_audit_detects_tampered_resource_boundary(tmp_path):
    from research.kube_tie_crossover_analysis import analyze, CrossoverEvidenceError
    run = complete_fake_run(tmp_path)
    filename = next((run/'traces').glob('*greedy_lex.json'))
    d = json.loads(filename.read_text())
    for n in d['fresh_snapshot']['node_inventory']:
        if n['name'] == d['result']['perturbed_worker']:
            n['requested_cpu_cores'] = 0.0
    filename.write_text(json.dumps(d))
    with pytest.raises(CrossoverEvidenceError):
        analyze([run])


def test_offline_audit_rejects_incomplete_run(tmp_path):
    from research.kube_tie_crossover_analysis import analyze, CrossoverEvidenceError
    run = complete_fake_run(tmp_path)
    csv_file = run/'trials.csv'
    csv_file.write_text('\n'.join(csv_file.read_text().splitlines()[:-1])+'\n')
    with pytest.raises(CrossoverEvidenceError, match='incomplete'):
        analyze([run])


def test_offline_audit_detects_invalid_plan(tmp_path):
    from research.kube_tie_crossover_analysis import analyze, CrossoverEvidenceError
    run = complete_fake_run(tmp_path)
    file = run/'manifest.json'
    manifest = json.loads(file.read_text())
    manifest['plan'][0]['arm'] = 'greedy_lex'
    file.write_text(json.dumps(manifest))
    with pytest.raises(CrossoverEvidenceError):
        analyze([run])


def test_offline_export_cannot_overwrite_or_write_inside_evidence(tmp_path):
    from research.kube_tie_crossover_analysis import analyze, export, CrossoverEvidenceError
    run = complete_fake_run(tmp_path)
    report = analyze([run])
    with pytest.raises(CrossoverEvidenceError):
        export(report, run/'audit', [run])
    dest = tmp_path/'report'
    dest.mkdir()
    with pytest.raises(FileExistsError):
        export(report, dest, [run])


def test_offline_cli_report_on_fake_run(tmp_path, capsys):
    from research.kube_tie_crossover_analysis import main as audit_main
    run = complete_fake_run(tmp_path)
    output = tmp_path/'summary'
    audit_main(['--run', str(run), '--output', str(output)])
    stdout = capsys.readouterr().out
    assert 'Audited 8 completed real-cluster trial records' in stdout
    assert (output/'summary.json').is_file()


def test_two_repeats_balanced_on_fake_cluster(tmp_path):
    client, clock = FakeCluster(), Clock()
    output = tmp_path/'two-repeats'
    rows = run_experiment(client, CrossoverConfig(repeats=2), output,
                          monotonic=clock, sleep=clock.sleep)
    assert len(rows) == 16
    assert len(list((output/'traces').glob('*.json'))) == 16
    for arm in ARMS:
        assert sum(r['arm'] == arm for r in rows) == 4
        assert sum(r['arm'] == arm and r['perturbed_worker'] == 'worker-a' for r in rows) == 2
        assert sum(r['arm'] == arm and r['perturbed_worker'] == 'worker-b' for r in rows) == 2
    assert all(r['competitor_cleanup_confirmed'] for r in rows)


def test_offline_audit_detects_tampered_initial_age_decision(tmp_path):
    from research.kube_tie_crossover_analysis import analyze, CrossoverEvidenceError
    run = complete_fake_run(tmp_path)
    trace_file = next((run/'traces').glob('*age_penalized.json'))
    trace = json.loads(trace_file.read_text())
    trace['result']['initial_selected_worker'] = trace['result']['perturbed_worker']
    trace_file.write_text(json.dumps(trace))
    csv_file = run/'trials.csv'
    with csv_file.open(newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        if row['arm'] == 'age_penalized' and int(row['block']) == trace['block']:
            row['initial_selected_worker'] = trace['result']['initial_selected_worker']
    with csv_file.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(CrossoverEvidenceError, match='initial policy action'):
        analyze([run])
