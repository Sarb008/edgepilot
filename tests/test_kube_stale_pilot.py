"""M6C tests use a fake kubectl client and fake clock: NEVER touch a real cluster."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from research.kube_stale_pilot import (
    CONTEXT, NAMESPACE, StaleConfig, assert_empty_namespace, inventory_by_name,
    main, mixed_samples, reservation_manifest, run_experiment, run_stale_trial,
    select_from_mixed, summarize, usable_workers, verified_request_change,
    wait_for_reservation,
)
from research.kube_validation import KubectlError
from research.models import Workload


class Clock:
    def __init__(self, start=200., tick=.035):
        self.now = start
        self.tick = tick

    def __call__(self):
        current = self.now
        self.now += self.tick
        return current

    def sleep(self, interval):
        assert interval > 0
        self.now += interval


class FakeKubectl:
    def __init__(self):
        self.ops = []
        self.reservation = None
        self.probe = None
        self.cleanup_fail = False
        self.fail_create_probe = False
        self.unexpected = False
        self.bad_binding = False
        self.perturbation_hidden = False
        self.active_succeeded_pods = []

    def verify_context(self):
        self.ops.append(('verify-context', (CONTEXT,)))

    def get_json(self, *args):
        self.ops.append(('get', args))
        if args == ('namespace', NAMESPACE):
            return {'metadata': {'name': NAMESPACE}}
        if args == ('pods', '-n', NAMESPACE):
            items = []
            if self.unexpected:
                items.append({'status': {'phase': 'Pending'}})
            if self.reservation:
                items.append({'status': {'phase': 'Running'}})
            return {'items': items}
        if args == ('nodes',):
            return {'items': [{
                'metadata': {'name': n, 'labels': {'kubernetes.io/hostname': n}},
                'spec': {'taints': []},
                'status': {'allocatable': {'cpu': '2'}, 'conditions': [
                    {'type': 'Ready', 'status': 'True'}]},
            } for n in ('worker-a', 'worker-b')]}
        if args == ('pods', '--all-namespaces'):
            pods = []
            if self.reservation and not self.perturbation_hidden:
                pods.append({'spec': {'nodeName': self.reservation['spec']['nodeSelector']['kubernetes.io/hostname'],
                                      'containers': self.reservation['spec']['containers']},
                             'status': {'phase': 'Running'}})
            return {'items': pods}
        if args[:1] == ('pod',):
            name = args[1]
            if self.reservation and self.reservation['metadata']['name'] == name:
                return {'spec': {'nodeName': ('worker-b' if self.bad_binding else
                                             self.reservation['spec']['nodeSelector']['kubernetes.io/hostname'])},
                        'status': {'phase': 'Running'}}
            if self.probe and self.probe['metadata']['name'] == name:
                return {'metadata': {'creationTimestamp': '2026-11-12T00:00:00Z'},
                        'spec': {'nodeName': self.probe['spec']['nodeSelector']['kubernetes.io/hostname']},
                        'status': {'phase': 'Succeeded', 'containerStatuses': [
                            {'state': {'terminated': {'startedAt': '2026-11-12T00:00:01Z',
                                                      'finishedAt': '2026-11-12T00:00:03Z',
                                                      'exitCode': 0}}}]}}
            raise KubectlError('NotFound')
        raise AssertionError(args)

    def call(self, *args, stdin=None):
        self.ops.append(('call', args))
        if args[0] == 'create':
            manifest = json.loads(stdin)
            if manifest['metadata']['labels'].get('edgepilot-v2-role') == 'reservation':
                self.reservation = manifest
            else:
                if self.fail_create_probe:
                    raise KubectlError('API timeout after uncertain creation')
                self.probe = manifest
            return ''
        if args[0] == 'delete':
            name = args[2]
            if self.cleanup_fail:
                raise KubectlError('delete failed')
            if self.reservation and self.reservation['metadata']['name'] == name:
                self.reservation = None
            if self.probe and self.probe['metadata']['name'] == name:
                self.probe = None
            return ''
        raise AssertionError(args)


def snapshots_for_diff():
    a = {'name': 'worker-a', 'hostname_label': 'worker-a', 'allocatable_cpu_cores': 2.,
         'requested_cpu_cores': 0., 'estimated_unreserved_cpu_cores': 2.,
         'usable_for_model': True}
    b = {**a, 'name': 'worker-b', 'hostname_label': 'worker-b'}
    old = {'measurement_window_start_monotonic': 10., 'receipt_monotonic': 10.2,
           'node_inventory': [a, b]}
    fresh = {'measurement_window_start_monotonic': 12., 'receipt_monotonic': 12.2,
             'node_inventory': [{**a, 'requested_cpu_cores': .2,
                                 'estimated_unreserved_cpu_cores': 1.8}, b]}
    return old, fresh


def test_manifest_safe_reservation():
    p = reservation_manifest('epv2-test', 'worker-a', 200, 60)
    assert p['spec']['nodeSelector']['kubernetes.io/hostname'] == 'worker-a'
    assert p['spec']['containers'][0]['resources']['requests']['cpu'] == '200m'
    assert p['spec']['containers'][0]['command'] == ['sh', '-c', 'sleep 60']
    assert p['metadata']['labels']['edgepilot-v2-role'] == 'reservation'


@pytest.mark.parametrize('cpu,seconds', [(50, 60), (501, 60), (200, 100), (200, 1), (True, 60)])
def test_reservation_rejects_unsafe_args(cpu, seconds):
    with pytest.raises(ValueError):
        reservation_manifest('epv2-test', 'worker-a', cpu, seconds)


@pytest.mark.parametrize('override', [
    {'context': 'prod'}, {'namespace': 'default'}, {'blocks': 4},
    {'blocks': True}, {'competitor_cpu_m': 900},
    {'competitor_hold_seconds': 10}, {'deadline_seconds': 5.},
    {'competitor_ready_timeout_seconds': float('nan')},
])
def test_strict_config(override):
    with pytest.raises(ValueError):
        StaleConfig(**override)


def test_constructs_genuine_mixed_age_telemetry():
    old, fresh = snapshots_for_diff()
    samples = mixed_samples(old, fresh, 'worker-a')
    assert samples['worker-a'].cpu_used == 0.
    assert samples['worker-b'].cpu_used == 0.
    assert samples['worker-a'].measured_at == 10.
    assert samples['worker-b'].measured_at == 12.
    assert samples['worker-a'].received_at == 10.2
    assert samples['worker-b'].received_at == 12.2
    assert verified_request_change(old, fresh, 'worker-a', 200) == pytest.approx(.2)
    assert inventory_by_name(fresh)['worker-a']['requested_cpu_cores'] == pytest.approx(.2)


def test_stale_input_changes_actual_policy_choice():
    old, fresh = snapshots_for_diff()
    samples = mixed_samples(old, fresh, 'worker-a')
    clock = Clock(start=13., tick=.01)
    w = Workload('job', 12.5, 57.5, .05, 2.)
    cfg = StaleConfig()
    client = FakeKubectl()
    greedy = select_from_mixed(client, cfg, 'greedy', w, samples,
                               monotonic=clock, sleep=clock.sleep)
    aged = select_from_mixed(client, cfg, 'age_penalized', w, samples,
                             monotonic=clock, sleep=clock.sleep)
    uncertain = select_from_mixed(client, cfg, 'uncertainty_aware', w, samples,
                                  monotonic=clock, sleep=clock.sleep)
    assert greedy['selected'] == 'worker-a'
    assert aged['selected'] == 'worker-b'
    assert uncertain['selected'] == 'worker-b'
    assert all(x['refreshes'] == 0 for x in (greedy, aged, uncertain))


def test_bounded_refetches_after_wait_and_sees_change():
    old, fresh = snapshots_for_diff()
    clock = Clock(start=13., tick=.01)
    w = Workload('job', 12.5, 57.5, .05, 2.)
    client = FakeKubectl()
    client.reservation = reservation_manifest('epv2-stub', 'worker-a', 200, 60)
    chosen = select_from_mixed(client, StaleConfig(), 'bounded_deferral', w,
                               mixed_samples(old, fresh, 'worker-a'),
                               monotonic=clock, sleep=clock.sleep)
    assert chosen['selected'] == 'worker-b'
    assert chosen['waited'] > 0
    assert chosen['refreshes'] >= 1


def test_no_future_dated_or_misordered_telemetry():
    old, fresh = snapshots_for_diff()
    bad_old = {**old, 'receipt_monotonic': 14.}
    with pytest.raises(ValueError):
        mixed_samples(bad_old, fresh, 'worker-a')
    samples = mixed_samples(old, fresh, 'worker-a')
    clock = Clock(start=11., tick=.01)
    w = Workload('job', 10.5, 55., .05, 2.)
    with pytest.raises(ValueError, match='not received yet'):
        select_from_mixed(FakeKubectl(), StaleConfig(), 'greedy', w,
                          samples, monotonic=clock, sleep=clock.sleep)


def test_wrong_or_unknown_node_rejected():
    old, fresh = snapshots_for_diff()
    with pytest.raises(ValueError):
        mixed_samples(old, fresh, 'unknown')
    with pytest.raises(KubectlError):
        verified_request_change(old, old, 'worker-a', 200)
    with pytest.raises(KubectlError):
        usable_workers({'node_inventory': [old['node_inventory'][0]]})


def test_refuses_dirty_namespace():
    client = FakeKubectl()
    client.unexpected = True
    with pytest.raises(KubectlError, match='ZERO Pods'):
        assert_empty_namespace(client, NAMESPACE)


def test_full_fake_trial_guarantees_cleanup():
    clock = Clock(start=200., tick=.06)
    client = FakeKubectl()
    row, trace = run_stale_trial(client, 'greedy', StaleConfig(), block=1, position=1,
                                 monotonic=clock, sleep=clock.sleep)
    assert row['terminal_phase'] == 'Succeeded', row
    assert row['on_time_observed']
    assert row['competitor_placed']
    assert row['observed_target_request_delta_cores'] == pytest.approx(.2)
    assert row['selected_differs_from_fresh_reference']
    assert row['probe_cleanup_confirmed'] and row['competitor_cleanup_confirmed']
    assert client.probe is None and client.reservation is None
    assert trace['mixed_policy_input']['worker-a']['cpu_used'] == 0
    assert trace['fresh_snapshot']['node_inventory'][0]['requested_cpu_cores'] == pytest.approx(.2)


def test_failed_probe_create_still_deletes_both_exact_names():
    clock = Clock(start=200., tick=.05)
    client = FakeKubectl()
    client.fail_create_probe = True
    row, _ = run_stale_trial(client, 'greedy', StaleConfig(), block=1, position=1,
                              monotonic=clock, sleep=clock.sleep)
    assert row['terminal_phase'] == 'ProbeError'
    assert row['competitor_cleanup_confirmed']
    assert client.reservation is None
    calls = [args for op, args in client.ops if op == 'call' and args[0] == 'delete']
    assert len(calls) == 2  # exact probe name + competitor name
    assert all('--ignore-not-found=true' in args for args in calls)


def test_rejects_unverified_contender():
    clock = Clock(start=200., tick=.05)
    client = FakeKubectl()
    client.perturbation_hidden = True
    row, _ = run_stale_trial(client, 'greedy', StaleConfig(), block=1, position=1,
                              monotonic=clock, sleep=clock.sleep)
    assert row['terminal_phase'] == 'ProbeError'
    assert 'delta' in row['error']
    assert row['competitor_cleanup_confirmed']
    assert not row.get('pod_name')


def test_misplaced_reservation_aborts():
    clock = Clock(start=200., tick=.05)
    client = FakeKubectl()
    client.bad_binding = True
    row, _ = run_stale_trial(client, 'greedy', StaleConfig(), block=1, position=1,
                              monotonic=clock, sleep=clock.sleep)
    assert row['terminal_phase'] == 'ProbeError'
    assert 'mis-placed' in row['error']
    assert row['competitor_cleanup_confirmed']


def test_experiment_refuses_continue_if_cleanup_not_confirmed(tmp_path):
    clock = Clock(start=200., tick=.05)
    client = FakeKubectl()
    client.cleanup_fail = True
    with pytest.raises(KubectlError, match='cleanup not confirmed'):
        run_experiment(client, StaleConfig(), tmp_path, monotonic=clock, sleep=clock.sleep)
    assert (tmp_path / 'trials.csv').exists()


def test_complete_fake_block_exports_evidence(tmp_path):
    clock = Clock(start=200., tick=.06)
    rows = run_experiment(FakeKubectl(), StaleConfig(), tmp_path,
                           monotonic=clock, sleep=clock.sleep)
    assert len(rows) == 4
    assert (tmp_path / 'manifest.json').exists()
    assert (tmp_path / 'trials.csv').exists()
    assert (tmp_path / 'summary.json').exists()
    assert len(list((tmp_path / 'traces').glob('*.json'))) == 4
    assert all(row['competitor_cleanup_confirmed'] for row in rows)
    assert len(summarize(rows)) == 4


def test_default_dry_run_has_no_kubectl_calls(capsys, tmp_path):
    with patch('research.kube_stale_pilot.Kubectl', side_effect=AssertionError('kubectl called')):
        main(['--blocks', '1', '--output', str(tmp_path / 'unused')])
    assert 'DRY RUN' in capsys.readouterr().out
    assert not (tmp_path / 'unused').exists()


def test_no_unbounded_pod_counts():
    with pytest.raises(ValueError):
        StaleConfig(blocks=6)
    assert len(__import__('research.kube_pilot', fromlist=['order_by_block']).order_by_block(3, 7)) == 12


def test_protects_previous_run_from_overwrite(tmp_path):
    (tmp_path / 'manifest.json').write_text('original')
    client = FakeKubectl()
    with pytest.raises(FileExistsError):
        run_experiment(client, StaleConfig(), tmp_path)
    assert (tmp_path / 'manifest.json').read_text() == 'original'
    assert not any(op == 'call' for op, _ in client.ops)


def test_rejects_snapshots_with_changed_eligible_nodes():
    old, fresh = snapshots_for_diff()
    changed = {**fresh, 'node_inventory': [fresh['node_inventory'][0]]}
    with pytest.raises(ValueError, match='eligible node set changed'):
        mixed_samples(old, changed, 'worker-a')


def test_second_block_perturbs_other_worker():
    clock = Clock(start=200., tick=.06)
    client = FakeKubectl()
    row, _ = run_stale_trial(client, 'greedy', StaleConfig(blocks=2),
                              block=2, position=1, monotonic=clock, sleep=clock.sleep)
    assert row['perturbed_worker'] == 'worker-b'
    assert row['competitor_cleanup_confirmed']
    assert row['terminal_phase'] == 'Succeeded'


def test_cleanup_failure_aborts_followup_trial(tmp_path):
    clock = Clock(start=200., tick=.05)
    client = FakeKubectl()
    client.cleanup_fail = True
    with pytest.raises(KubectlError):
        run_experiment(client, StaleConfig(), tmp_path, monotonic=clock, sleep=clock.sleep)
    assert len(list((tmp_path / 'traces').glob('*.json'))) == 1
    assert (tmp_path / 'summary.json').exists()
