"""Offline tests; never call a real kubectl executable or mutate a cluster."""
import json
from copy import deepcopy
from unittest.mock import patch

import pytest

from research.kube_validation import (
    Kubectl, KubectlError, capture_snapshot, cpu_cores, node_inventory,
    pod_manifest, pod_observation, requested_cpu, run_smoke, samples_from_inventory,
)


def node(name='worker-a', cpu='2000m', *, ready=True, labels=True, taints=()):
    return {'metadata': {'name': name,
                         'labels': {'kubernetes.io/hostname': name} if labels else {}},
            'status': {'allocatable': {'cpu': cpu},
                       'conditions': [{'type': 'Ready', 'status': 'True' if ready else 'False'}]},
            'spec': {'taints': list(taints)}}


def pod(cpu='500m', node_name='worker-a', phase='Running'):
    return {'metadata': {'name': 'p1'}, 'spec': {'nodeName': node_name,
            'containers': [{'resources': {'requests': {'cpu': cpu}}}]},
            'status': {'phase': phase}}


@pytest.mark.parametrize(('quantity', 'expected'), [
    ('1', 1.0), ('500m', .5), ('250u', .00025), ('1e3m', 1),
    ('0.25', .25), ('10n', 1e-8), ('2k', 2000), ('1Ki', 1024),
])
def test_cpu_quantities(quantity, expected):
    assert cpu_cores(quantity) == pytest.approx(expected)


@pytest.mark.parametrize('quantity', ['-1', 'NaN', '1foo', '1.2.3', '', 'inf', True, None, '1mX'])
def test_reject_invalid_cpu_quantity(quantity):
    with pytest.raises(ValueError):
        cpu_cores(quantity)


def test_requested_cpu_container_sum_and_init_max_overhead():
    p = pod('300m')
    p['spec']['containers'].append({'resources': {'requests': {'cpu': '250m'}}})
    p['spec']['initContainers'] = [{'resources': {'requests': {'cpu': '900m'}}}]
    p['spec']['overhead'] = {'cpu': '50m'}
    assert requested_cpu(p) == pytest.approx(.95)
    p['spec']['initContainers'][0]['restartPolicy'] = 'Always'
    with pytest.raises(ValueError, match='sidecars'):
        requested_cpu(p)


def test_requested_cpu_pod_level_request():
    p = pod('200m')
    p['spec']['resources'] = {'requests': {'cpu': '1'}}
    assert requested_cpu(p) == 1


def test_inventory_subtracts_only_bound_active_pods():
    nodes = {'items': [node(), node('worker-b', '1000m')]}
    pods = {'items': [pod(), pod('300m', 'worker-a', 'Succeeded'),
                      pod('200m', None), pod('100m', 'worker-b')]}
    data = node_inventory(nodes, pods)
    assert data[0]['estimated_unreserved_cpu_cores'] == 1.5
    assert data[0]['active_pod_count'] == 1
    assert data[1]['estimated_unreserved_cpu_cores'] == .9
    assert data[1]['usable_for_model']


def test_invalid_nodes_and_oversubscribed_requests_not_hidden():
    nodes = {'items': [node('worker-a', '500m'), node('worker-b', ready=False),
                       node('worker-c', taints=[{'effect': 'NoSchedule'}]) ]}
    data = node_inventory(nodes, {'items': [pod('700m')]})
    assert data[0]['oversubscribed_requests']
    assert not data[0]['usable_for_model']
    assert not data[1]['usable_for_model']
    assert not data[2]['usable_for_model']
    assert samples_from_inventory(data, 0, 1) == {}


def test_node_inventory_rejects_duplicate_nodes_and_invalid_json():
    with pytest.raises(ValueError, match='duplicate'):
        node_inventory({'items': [node(), node()]}, {'items': []})
    with pytest.raises(ValueError, match='items'):
        node_inventory({}, {'items': []})


class FakeKubectl:
    def __init__(self):
        self.ops = []
        self.pod_statuses = []
        self.fail_cleanup = False

    def get_json(self, *args):
        self.ops.append(('get', args))
        if args[0] == 'nodes':
            return {'items': [node()]}
        if args[0] == 'pods':
            return {'items': [pod()]}
        if args[0] == 'namespace':
            return {'metadata': {'name': args[1]}}
        if args[0] == 'pod':
            return deepcopy(self.pod_statuses.pop(0))
        raise AssertionError(args)

    def call(self, *args, stdin=None):
        self.ops.append((args[0], args))
        if args[0] == 'create':
            assert json.loads(stdin)['spec']['nodeSelector'] == {'kubernetes.io/hostname': 'worker-a'}
            return ''
        if args[0] == 'delete' and self.fail_cleanup:
            raise KubectlError('simulated cleanup failure')
        return ''


def test_snapshot_is_read_only_and_has_conservative_window():
    f = FakeKubectl()
    ticks = iter((2., 2.4))
    snapshot = capture_snapshot(f, monotonic=lambda: next(ticks))
    assert snapshot['api_read_duration_seconds'] == pytest.approx(.4)
    assert [operation for operation, _ in f.ops] == ['get', 'get']
    assert snapshot['node_inventory'][0]['requested_cpu_cores'] == .5


def completed_pod(phase='Succeeded'):
    return {'metadata': {'creationTimestamp': '2026-01-01T10:00:00Z'},
            'spec': {'nodeName': 'worker-a'},
            'status': {'phase': phase, 'containerStatuses': [
                {'state': {'terminated': {'startedAt': '2026-01-01T10:00:02Z',
                                          'finishedAt': '2026-01-01T10:00:04Z',
                                          'exitCode': 0}}}]}}


def test_pod_observation_correctly_uses_server_timestamps():
    obs = pod_observation(completed_pod())
    assert obs['kubernetes_creation_to_container_start_seconds'] == 2
    assert obs['kubernetes_container_execution_seconds'] == 2


def test_pod_manifest_scoped_and_bounded():
    p = pod_manifest('epv2-smoke-test', 'worker-a')
    assert p['spec']['restartPolicy'] == 'Never'
    assert 'nodeSelector' in p['spec']
    assert 'nodeName' not in p['spec']
    assert p['spec']['containers'][0]['resources']['requests']['cpu'] == '50m'
    for n in (-1, 300, True):
        with pytest.raises(ValueError):
            pod_manifest('epv2-smoke-test', 'worker-a', cpu_m=n)


def test_smoke_one_pod_and_exact_cleanup():
    f = FakeKubectl()
    f.pod_statuses = [completed_pod()]
    snapshot = {'measurement_window_start_monotonic': 1., 'receipt_monotonic': 1.2,
                'node_inventory': node_inventory({'items': [node()]}, {'items': []})}
    ticks = iter((2., 2.1, 2.4))
    out = run_smoke(f, 'edgepilot-v2-validation', snapshot, monotonic=lambda: next(ticks))
    assert out['terminal_phase'] == 'Succeeded'
    assert out['on_time_observed']
    creates = [args for op, args in f.ops if op == 'create']
    deletes = [args for op, args in f.ops if op == 'delete']
    assert len(creates) == len(deletes) == 1
    assert deletes[0][2] == out['pod_name']


def test_no_dangerous_namespace_or_unapproved_cluster_writes():
    f = FakeKubectl()
    snapshot = {'measurement_window_start_monotonic': 1., 'receipt_monotonic': 1.2,
                'node_inventory': node_inventory({'items': [node()]}, {'items': []})}
    for namespace in ('default', 'kube-system', '', 'production'):
        with pytest.raises(ValueError, match='namespace'):
            run_smoke(f, namespace, snapshot)
    assert f.ops == []


def test_smoke_cleanup_error_exposed_not_hidden():
    f = FakeKubectl()
    f.pod_statuses = [completed_pod()]
    f.fail_cleanup = True
    snapshot = {'measurement_window_start_monotonic': 1., 'receipt_monotonic': 1.2,
                'node_inventory': node_inventory({'items': [node()]}, {'items': []})}
    ticks = iter((2., 2.1, 2.4))
    out = run_smoke(f, 'edgepilot-v2-validation', snapshot, monotonic=lambda: next(ticks))
    assert 'simulated cleanup failure' in out['cleanup_error']


def test_kubectl_requires_explicit_context():
    with patch('research.kube_validation.shutil.which', return_value='kubectl'):
        with pytest.raises(ValueError):
            Kubectl('')


def test_no_exec_without_execute_flag_in_cli():
    """The only entry point mutating the cluster is the explicit opt-in flag."""
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from research.kube_validation import main
    f = FakeKubectl()
    f.verify_context = lambda: None
    with TemporaryDirectory() as tmp, patch('research.kube_validation.Kubectl', return_value=f):
        main(['--context', 'kind-edgepilot-v2', '--output', str(Path(tmp) / 'snapshot.json')])
    assert all(op == 'get' for op, _ in f.ops)
