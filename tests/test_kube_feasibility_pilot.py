"""Unit tests use ONLY fake clocks and fake Kubernetes clients (never a real cluster)."""
import json
from pathlib import Path

import pytest

from research.kube_feasibility_pilot import (
    FeasibilityConfig, _unschedulable_reason, high_request_probe_manifest,
    main, request_from_old_capacity, run_experiment, run_trial, summarize,
    verify_boundary, run_probe,
)
from research.kube_stale_pilot import NAMESPACE
from research.kube_validation import KubectlError


class Clock:
    def __init__(self):
        self.value = 100.

    def __call__(self):
        self.value += .02
        return self.value

    def sleep(self, duration):
        self.value += duration


class FakeCluster:
    """Two 2-core kind workers. 200m competitor makes a 1900m request infeasible."""
    def __init__(self):
        self.ops = []
        self.competitor = None
        self.probe = None
        self.cleanup_failed = False
        self.fail_probe_create = False
        self.namespace_pods = False
        self.overwrite_perturbation = False
        self.return_wrong_binding = False
        self.force_probe_pending = False

    def verify_context(self):
        self.ops.append(('verify-context',))

    def get_json(self, *args):
        self.ops.append(('get', *args))
        if args == ('namespace', NAMESPACE):
            return {'metadata': {'name': NAMESPACE}}
        if args == ('pods', '-n', NAMESPACE):
            return {'items': ([{'status': {'phase': 'Running'}}] if
                              self.namespace_pods or self.competitor or self.probe else [])}
        if args == ('nodes',):
            return {'items': [{
                'metadata': {'name': n, 'labels': {'kubernetes.io/hostname': n}},
                'spec': {'taints': []},
                'status': {'allocatable': {'cpu': '2'},
                           'conditions': [{'type': 'Ready', 'status': 'True'}]},
            } for n in ('worker-a', 'worker-b')]}
        if args == ('pods', '--all-namespaces'):
            pods = []
            if self.competitor and not self.overwrite_perturbation:
                pods.append({'spec': {
                    'nodeName': self.competitor['spec']['nodeSelector']['kubernetes.io/hostname'],
                    'containers': self.competitor['spec']['containers']},
                    'status': {'phase': 'Running'}})
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
                if self.competitor and (
                        self.competitor['spec']['nodeSelector']['kubernetes.io/hostname'] == expected):
                    reserved = int(self.competitor['spec']['containers'][0]['resources']['requests']['cpu'][:-1])
                if reserved + req > 2000 or self.force_probe_pending:
                    return {'metadata': {'creationTimestamp': '2026-11-12T00:00:00Z'},
                            'spec': {'nodeName': None},
                            'status': {'phase': 'Pending', 'conditions': [{
                                'type': 'PodScheduled', 'status': 'False', 'reason': 'Unschedulable',
                                'message': 'Insufficient cpu'}]}}
                return {'metadata': {'creationTimestamp': '2026-11-12T00:00:00Z'},
                        'spec': {'nodeName': expected},
                        'status': {'phase': 'Succeeded', 'containerStatuses': [{
                            'state': {'terminated': {'startedAt': '2026-11-12T00:00:01Z',
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
                    raise KubectlError('connection reset after uncertain create')
                self.probe = manifest
            else:
                raise AssertionError(role)
            return ''
        if args[0] == 'delete':
            if self.cleanup_failed:
                raise KubectlError('delete not accepted')
            name = args[2]
            if self.competitor and self.competitor['metadata']['name'] == name:
                self.competitor = None
            if self.probe and self.probe['metadata']['name'] == name:
                self.probe = None
            return ''
        raise AssertionError(args)


@pytest.mark.parametrize('kwargs', [
    {'context': 'prod'}, {'namespace': 'default'}, {'blocks': 3},
    {'blocks': True}, {'seed': '2'}, {'deadline_seconds': 5},
    {'deadline_seconds': float('nan')}, {'competitor_cpu_m': 600},
    {'competitor_cpu_m': True}, {'margin_cpu_m': 250},
    {'margin_cpu_m': 200}, {'sleep_seconds': 9},
    {'poll_interval_seconds': 0},
])
def test_config_blocks_unsafe_input(kwargs):
    with pytest.raises(ValueError):
        FeasibilityConfig(**kwargs)


def test_default_config_and_model_boundary():
    cfg = FeasibilityConfig()
    target = {'estimated_unreserved_cpu_cores': 2.}
    peer = {'estimated_unreserved_cpu_cores': 2.}
    assert request_from_old_capacity(target, peer, cfg) == 1900
    old = {'node_inventory': [dict(target, name='worker-a'), dict(peer, name='worker-b')]}
    fresh = {'node_inventory': [dict(target, name='worker-a',
                                     estimated_unreserved_cpu_cores=1.8,
                                     requested_cpu_cores=.2, usable_for_model=True),
                               dict(peer, name='worker-b', requested_cpu_cores=0,
                                    usable_for_model=True)]}
    old['node_inventory'][0].update(requested_cpu_cores=0, usable_for_model=True)
    old['node_inventory'][1].update(requested_cpu_cores=0, usable_for_model=True)
    r = verify_boundary(old, fresh, 'worker-a', 'worker-b', 1900, cfg)
    assert r == {'stale_target_feasible_in_old_snapshot': True,
                 'stale_target_feasible_in_fresh_snapshot': False,
                 'fresh_peer_feasible': True}


@pytest.mark.parametrize('free,peer', [(0.3, 2.), (2., 1.7), (float('nan'), 2.), (13., 13.)])
def test_invalid_or_unsafe_boundary(free, peer):
    with pytest.raises(KubectlError):
        request_from_old_capacity({'estimated_unreserved_cpu_cores': free},
                                  {'estimated_unreserved_cpu_cores': peer}, FeasibilityConfig())


def test_manifest_validates_and_only_sleeps():
    m = high_request_probe_manifest('epv2-test-123', 'worker-a', 1900, 2)
    c = m['spec']['containers'][0]
    assert c['command'] == ['sh', '-c', 'sleep 2']
    assert c['resources']['requests']['cpu'] == '1900m'
    assert c['resources']['limits']['cpu'] == '1900m'
    assert m['spec']['nodeSelector']['kubernetes.io/hostname'] == 'worker-a'
    assert m['metadata']['labels']['edgepilot-v2-role'] == 'feasibility-probe'


@pytest.mark.parametrize('requested_m,seconds', [(1, 2), (12001, 2), (1900, 0), (1900, 6), (True, 2)])
def test_manifest_rejects_bad_inputs(requested_m, seconds):
    with pytest.raises(ValueError):
        high_request_probe_manifest('epv2-test-123', 'worker-a', requested_m, seconds)


def test_dry_run_has_no_cluster_access(capsys):
    main(['--blocks', '1'])
    assert 'DRY RUN' in capsys.readouterr().out


def test_unschedulable_reason_bounded():
    assert _unschedulable_reason({'status': {'conditions': [{
        'type': 'PodScheduled', 'status': 'False', 'reason': 'Unschedulable',
        'message': 'A' * 1000}]}}) == 'A' * 500
    assert _unschedulable_reason({'status': {}}) is None


def test_greedy_selects_infeasible_old_target_and_times_out():
    client, clock = FakeCluster(), Clock()
    row, trace = run_trial(client, 'greedy', FeasibilityConfig(), block=1, position=1,
                           monotonic=clock, sleep=clock.sleep)
    assert row['probe_cpu_m'] == 1900
    assert row['selected_worker'] == 'worker-a'
    assert row['reference_worker'] == 'worker-b'
    assert row['selected_feasible_in_fresh_snapshot'] is False
    assert row['terminal_phase'] == 'TimedOutObservation'
    assert row['unschedulable_reason'] == 'Insufficient cpu'
    assert not row['on_time_observed']
    assert row['probe_cleanup_confirmed'] and row['competitor_cleanup_confirmed']
    assert client.probe is None and client.competitor is None
    assert trace['mixed_policy_input']['worker-a']['cpu_used'] == 0.


@pytest.mark.parametrize('policy', ['age_penalized', 'bounded_deferral'])
def test_age_aware_or_deferred_selects_peer_and_succeeds(policy):
    client, clock = FakeCluster(), Clock()
    row, trace = run_trial(client, policy, FeasibilityConfig(), block=1, position=1,
                           monotonic=clock, sleep=clock.sleep)
    assert row['selected_worker'] == 'worker-b'
    assert row['selected_feasible_in_fresh_snapshot'] is True
    assert row['terminal_phase'] == 'Succeeded'
    assert row['on_time_observed']
    assert client.probe is None and client.competitor is None
    assert len(trace['mixed_policy_input']) == 2


def test_uncertainty_aware_can_safely_decline_very_tight_request():
    client, clock = FakeCluster(), Clock()
    row, _ = run_trial(client, 'uncertainty_aware', FeasibilityConfig(),
                       block=1, position=1, monotonic=clock, sleep=clock.sleep)
    assert row['terminal_phase'] in ('NoEligibleNode', 'Succeeded')
    assert row['competitor_cleanup_confirmed']
    assert client.probe is None and client.competitor is None


def test_wrong_binding_or_missing_reservation_aborts_safely():
    for attribute in ('return_wrong_binding', 'overwrite_perturbation'):
        client, clock = FakeCluster(), Clock()
        setattr(client, attribute, True)
        row, _ = run_trial(client, 'greedy', FeasibilityConfig(), block=1,
                           position=1, monotonic=clock, sleep=clock.sleep)
        assert row['terminal_phase'] == 'ProbeError'
        assert row.get('error')
        assert row['competitor_cleanup_confirmed']
        assert client.competitor is None


def test_uncertain_create_still_attempts_exact_probe_delete():
    client, clock = FakeCluster(), Clock()
    client.fail_probe_create = True
    row, _ = run_trial(client, 'age_penalized', FeasibilityConfig(), block=1,
                       position=1, monotonic=clock, sleep=clock.sleep)
    assert row['terminal_phase'] == 'ProbeError'
    assert row['probe_cleanup_confirmed']
    assert row.get('error')
    assert client.probe is None
    assert client.competitor is None
    assert any(op[0] == 'call' and op[1] == 'delete' and 'epv2-feas-job-' in op[3]
               for op in client.ops)


def test_fails_closed_when_namespace_not_empty(tmp_path):
    client = FakeCluster()
    client.namespace_pods = True
    with pytest.raises(KubectlError):
        run_experiment(client, FeasibilityConfig(), tmp_path/'out')
    assert client.competitor is None
    assert not (tmp_path/'out').exists()


def test_rejects_output_overwrite(tmp_path):
    client = FakeCluster()
    out = tmp_path/'out'
    out.mkdir()
    (out/'trials.csv').write_text('old')
    with pytest.raises(FileExistsError):
        run_experiment(client, FeasibilityConfig(), out)


def test_one_block_outputs_all_records_and_cleans_every_pod(tmp_path):
    client, clock = FakeCluster(), Clock()
    rows = run_experiment(client, FeasibilityConfig(), tmp_path/'out',
                          monotonic=clock, sleep=clock.sleep)
    assert len(rows) == 4
    assert set(r['policy'] for r in rows) == set(['greedy', 'age_penalized',
                                                 'uncertainty_aware', 'bounded_deferral'])
    assert (tmp_path/'out'/'manifest.json').is_file()
    assert (tmp_path/'out'/'trials.csv').is_file()
    assert len(list((tmp_path/'out'/'traces').glob('*.json'))) == 4
    assert client.probe is None and client.competitor is None
    summary = summarize(rows)
    assert next(r for r in summary if r['policy'] == 'greedy')['fresh_snapshot_infeasible_choices'] == 1
    assert any(r['policy'] == 'age_penalized' and r['on_time_observed'] for r in rows)


def test_cleanup_failure_stops_before_next_trial(tmp_path):
    client, clock = FakeCluster(), Clock()
    client.cleanup_failed = True
    with pytest.raises(KubectlError):
        run_experiment(client, FeasibilityConfig(), tmp_path/'out',
                       monotonic=clock, sleep=clock.sleep)
    assert (tmp_path/'out'/'trials.csv').exists()
    assert len(list((tmp_path/'out'/'traces').glob('*.json'))) == 1
