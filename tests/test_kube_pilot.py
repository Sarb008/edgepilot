"""Offline, no real kubectl, Docker, or cluster mutations."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from research.kube_pilot import (CONTEXT, POLICIES, PilotConfig, choose_node, main,
                                 order_by_block, policy_for, run_pilot, run_trial, summarize)
from research.kube_smoke_analysis import (METRICS, describe, quantile_nearest_rank, read_smokes)
from research.kube_validation import KubectlError
from research.models import Workload


class Clock:
    def __init__(self, start=50., increment=.02):
        self.time = start
        self.increment = increment

    def __call__(self):
        value = self.time
        self.time += self.increment
        return value

    def sleep(self, duration):
        assert duration >= 0
        self.time += duration


class FakeClient:
    def __init__(self):
        self.ops = []
        self.fail_cleanup = False
        self.fail_create = False
        self.active = False
        self.usable = True
        self.pod_phase = 'Succeeded'

    def get_json(self, *args):
        self.ops.append(('get', args))
        if args[:1] == ('nodes',):
            taints = [] if self.usable else [{'effect': 'NoSchedule'}]
            return {'items': [{
                'metadata': {'name': name, 'labels': {'kubernetes.io/hostname': name}},
                'spec': {'taints': taints},
                'status': {'allocatable': {'cpu': '2'},
                           'conditions': [{'type': 'Ready', 'status': 'True'}]},
            } for name in ('worker-a', 'worker-b')]}
        if args == ('pods', '--all-namespaces'):
            return {'items': []}
        if args == ('pods', '-n', 'edgepilot-v2-validation'):
            return {'items': ([{'status': {'phase': 'Pending'}}] if self.active else [])}
        if args[:1] == ('namespace',):
            return {'metadata': {'name': args[1]}}
        if args[:1] == ('pod',):
            return {
                'metadata': {'creationTimestamp': '2026-11-12T00:00:00Z'},
                'spec': {'nodeName': 'worker-a'},
                'status': {'phase': self.pod_phase, 'containerStatuses': [
                    {'state': {'terminated': {'startedAt': '2026-11-12T00:00:01Z',
                                              'finishedAt': '2026-11-12T00:00:03Z',
                                              'exitCode': 0 if self.pod_phase == 'Succeeded' else 1}}}]},
            }
        raise AssertionError(args)

    def call(self, *args, stdin=None):
        self.ops.append((args[0], args))
        if args[0] == 'create':
            assert args[1:4] == ('-n', 'edgepilot-v2-validation', '-f')
            manifest = json.loads(stdin)
            assert manifest['spec']['nodeSelector']['kubernetes.io/hostname'] in ('worker-a', 'worker-b')
            if self.fail_create:
                raise KubectlError('simulated create uncertainty')
        if args[0] == 'delete':
            assert args[1:3] == ('pod', args[2])
            assert args[-2:] == ('--wait=true', '--timeout=15s')
            if self.fail_cleanup:
                raise KubectlError('simulated cleanup failure')
        return ''


@pytest.mark.parametrize('kwargs', [
    {'context': 'production'}, {'namespace': 'default'}, {'blocks': 0},
    {'blocks': 6}, {'blocks': True}, {'cpu_m': 201}, {'cpu_m': 0},
    {'sleep_seconds': 11}, {'deadline_seconds': 121}, {'deadline_seconds': 0},
    {'max_wait_seconds': 16}, {'telemetry_refresh_seconds': 0},
    {'placement_budget_seconds': 46}, {'poll_interval_seconds': 9},
])
def test_config_rejects_risky_inputs(kwargs):
    with pytest.raises(ValueError):
        PilotConfig(**kwargs)


def test_randomized_plan_balances_all_positions_in_four_blocks():
    plan = order_by_block(4, 42)
    assert len(plan) == 16
    assert len(set((b, p) for b, _, p in plan)) == 16
    assert all(sorted([o for b, o, p in plan if p == policy]) == [1, 2, 3, 4]
               for policy in POLICIES)
    assert plan == order_by_block(4, 42)
    assert plan != order_by_block(4, 7)


def test_policy_factory_uses_existing_implementations():
    cfg = PilotConfig()
    for name in POLICIES:
        assert hasattr(policy_for(name, cfg), 'decide')
    with pytest.raises(ValueError):
        policy_for('no-such-policy', cfg)


def test_dry_run_never_instantiates_client(capsys):
    with patch('research.kube_pilot.Kubectl', side_effect=AssertionError('must not use cluster')):
        main(['--blocks', '1'])
    assert 'DRY RUN' in capsys.readouterr().out


def test_choose_node_uses_a_received_snapshot_and_eligible_worker():
    fake = FakeClient()
    clock = Clock()
    cfg = PilotConfig()
    start = clock()
    w = Workload('j1', start, start + cfg.deadline_seconds, .05, 2.)
    selection = choose_node(fake, 'greedy', cfg, w, monotonic=clock, sleep=clock.sleep)
    assert selection['node']['name'] == 'worker-a'
    assert selection['reads'] == 1
    assert selection['age'] >= 0
    assert all(op == 'get' for op, _ in fake.ops)


def test_bounded_deferral_refreshes_and_obeys_wait_cap():
    fake = FakeClient()
    clock = Clock()
    cfg = PilotConfig(max_wait_seconds=.8, freshness_threshold_seconds=0.,
                      placement_budget_seconds=0., telemetry_refresh_seconds=.25)
    start = clock()
    w = Workload('j1', start, start + cfg.deadline_seconds, .05, 2.)
    selection = choose_node(fake, 'bounded_deferral', cfg, w, monotonic=clock, sleep=clock.sleep)
    assert selection['reads'] >= 2
    assert selection['wait'] > 0
    assert selection['wait'] <= cfg.max_wait_seconds
    assert selection['node']['name'] == 'worker-a'


def test_no_usable_node_does_not_submit_a_pod():
    fake = FakeClient()
    fake.usable = False
    clock = Clock()
    row = run_trial(fake, 'greedy', PilotConfig(), block=1, order=1,
                    monotonic=clock, sleep=clock.sleep)
    assert row['terminal_phase'] == 'NoEligibleNode'
    assert not row['on_time_observed']
    assert not any(op == 'create' for op, _ in fake.ops)


@pytest.mark.parametrize('policy', POLICIES)
def test_trial_executes_at_most_one_pod_and_cleans_exact_name(policy):
    fake = FakeClient()
    clock = Clock()
    row = run_trial(fake, policy, PilotConfig(), block=1, order=1,
                    monotonic=clock, sleep=clock.sleep)
    assert row['terminal_phase'] == 'Succeeded'
    assert row['on_time_observed']
    creates = [args for op, args in fake.ops if op == 'create']
    deletes = [args for op, args in fake.ops if op == 'delete']
    assert len(creates) == len(deletes) == 1
    assert row['pod_name'] in deletes[0]
    assert row['cleanup_attempted_and_accepted']
    assert row['node_selected'] == 'worker-a'
    assert row['node_actual'] == 'worker-a'


def test_failed_create_attempt_still_triggers_exact_cleanup():
    fake = FakeClient()
    fake.fail_create = True
    clock = Clock()
    row = run_trial(fake, 'greedy', PilotConfig(), block=1, order=1,
                    monotonic=clock, sleep=clock.sleep)
    assert row['terminal_phase'] == 'ProbeError'
    assert not row['on_time_observed']
    assert row['cleanup_attempted_and_accepted']
    assert row['pod_name'] in next(args for op, args in fake.ops if op == 'delete')


def test_cleanup_failure_exposed_and_aborts_following_trials(tmp_path):
    fake = FakeClient()
    fake.fail_cleanup = True
    clock = Clock()
    with pytest.raises(KubectlError, match='cleanup not confirmed'):
        run_pilot(fake, PilotConfig(blocks=2), tmp_path,
                  monotonic=clock, sleep=clock.sleep)
    assert len(list(tmp_path.glob('trials.csv'))) == 1
    assert (tmp_path / 'trials.csv').read_text(encoding='utf-8').count('\n') == 2


def test_preexisting_live_pod_aborts_without_new_pods(tmp_path):
    fake = FakeClient()
    fake.active = True
    clock = Clock()
    with pytest.raises(KubectlError, match='active Pods'):
        run_pilot(fake, PilotConfig(blocks=1), tmp_path,
                  monotonic=clock, sleep=clock.sleep)
    assert not any(op == 'create' for op, _ in fake.ops)


def test_trial_plan_writes_all_rows_and_manifest(tmp_path):
    fake = FakeClient()
    clock = Clock()
    rows = run_pilot(fake, PilotConfig(blocks=1), tmp_path,
                     monotonic=clock, sleep=clock.sleep)
    assert len(rows) == 4
    assert all(r['terminal_phase'] == 'Succeeded' for r in rows)
    assert (tmp_path / 'trials.csv').is_file()
    assert len((tmp_path / 'trials.csv').read_text(encoding='utf-8').splitlines()) == 5
    manifest = json.loads((tmp_path / 'manifest.json').read_text(encoding='utf-8'))
    assert len(manifest['plan']) == 4
    assert 'not simultaneous or causal' in manifest['measurement_class']
    assert all(r['trials'] == 1 for r in summarize(rows))


def test_smoke_analysis_realistic_files_and_missing_values(tmp_path):
    for index in range(1, 11):
        smoke = {'terminal_phase': 'Succeeded', 'on_time_observed': True,
                 'cleanup_attempted_and_accepted': True, 'node_name': 'worker-a',
                 'end_to_end_observed_seconds': float(index),
                 'kubernetes_creation_to_container_start_seconds': float(index) + .5,
                 'kubernetes_container_execution_seconds': 2.,
                 'estimated_telemetry_age_at_decision_seconds': .2,
                 'submit_api_elapsed_seconds': None}
        (tmp_path / f'smoke_{index}.json').write_text(
            json.dumps({'context': CONTEXT, 'smoke': smoke}), encoding='utf-8')
    rows = read_smokes(tmp_path)
    result = describe(rows)
    assert result['sample_count'] == 10
    assert result['successful'] == result['observed_on_time'] == 10
    assert result['metrics']['end_to_end_observed_seconds']['median'] == 5.5
    assert result['metrics']['end_to_end_observed_seconds']['p95_nearest_rank'] == 10
    assert result['metrics']['submit_api_elapsed_seconds']['count'] == 0


def test_smoke_analysis_rejects_bad_data_and_missing_files(tmp_path):
    with pytest.raises(ValueError, match='no smoke files'):
        read_smokes(tmp_path)
    (tmp_path / 'smoke_1.json').write_text('{"smoke":{"end_to_end_observed_seconds":-1}}', encoding='utf-8')
    with pytest.raises(ValueError, match='invalid end_to_end'):
        read_smokes(tmp_path)
    assert quantile_nearest_rank([], .95) is None
    assert quantile_nearest_rank([1, 2, 3], .95) == 3
    with pytest.raises(ValueError):
        quantile_nearest_rank([1, 2], 0)


def test_placement_mismatch_never_counted_as_on_time():
    class MisplacedClient(FakeClient):
        def get_json(self, *args):
            pod = super().get_json(*args)
            if args[:1] == ('pod',):
                pod['spec']['nodeName'] = 'worker-b'
            return pod
    fake = MisplacedClient()
    clock = Clock()
    row = run_trial(fake, 'greedy', PilotConfig(), block=1, order=1,
                    monotonic=clock, sleep=clock.sleep)
    assert row['terminal_phase'] == 'Succeeded'
    assert row['placement_mismatch']
    assert not row['on_time_observed']
