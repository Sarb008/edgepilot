from dataclasses import replace
import math
import pytest

from research.advanced import Impairment, ResearchSimulator
from research.models import NodeState, Workload
from research.policies import BoundedDeferral, Greedy
from research.reliability import (
    BackgroundLoad, NodeOutage, ReliabilityConfig, ReliabilitySimulator,
)
from research.reliability_study import run_reliability_study, scenarios
from research.study import Condition, generate, policies


def node(name='edge', cap=2, used=0):
    return NodeState(name, 0, cap, used)


def job(name='a', arrival=0, duration=4, deadline=20, cpu=1):
    return Workload(name, arrival, deadline, cpu, duration)


def simulate(jobs=None, nodes=None, config=None, policy=None, interval=1, delay=0):
    s = ReliabilitySimulator(nodes or [node()], jobs if jobs is not None else [job()],
                             policy or Greedy(), telemetry_interval=interval,
                             telemetry_delay=delay, reliability=config)
    return s, s.run()


def test_nominal_matches_existing_engine_for_all_policies():
    for policy in (Greedy(), BoundedDeferral(2, .75)):
        nodes, jobs, impairment = generate(1001, Condition('tiny', jobs=30))
        old = ResearchSimulator(nodes, jobs, policy, impairment=impairment)
        new = ReliabilitySimulator(nodes, jobs, policy, impairment=impairment)
        assert old.run() == new.run()
        for k, value in old.metrics().items():
            assert new.metrics()[k] == pytest.approx(value)


def test_outage_preempts_and_restarts_after_recovery():
    cfg = ReliabilityConfig(outages=(NodeOutage('edge', 2, 5),), max_restarts=1,
                            restart_backoff=.5)
    s, result = simulate(config=cfg, interval=.5)
    assert result['a'].on_time
    assert result['a'].start == 5.5
    assert result['a'].finish == 9.5
    assert s.metrics()['outage_preemptions'] == 1
    assert s.metrics()['recovered_jobs'] == 1
    assert s.actual['edge'] == 0


def test_no_ghost_completion_from_preempted_attempt():
    cfg = ReliabilityConfig(outages=(NodeOutage('edge', 1, 2),))
    s, result = simulate([job(duration=4)], config=cfg)
    assert result['a'].finish == 6.5
    assert [event for event in s.trace if event[1] == 'complete'] == [(6.5, 'complete', 'a')]


def test_restart_budget_exhaustion_is_terminal():
    cfg = ReliabilityConfig(outages=(NodeOutage('edge', 1, 2),), max_restarts=0)
    s, result = simulate(config=cfg)
    assert result['a'].status == 'failed_outage'
    assert s.metrics()['outage_job_failure_rate'] == 1
    assert s.actual['edge'] == 0


def test_outage_known_to_engine_not_policy():
    cfg = ReliabilityConfig(outages=(NodeOutage('edge', 0, 4),))
    s, result = simulate([job(arrival=1)], config=cfg, interval=2)
    assert result['a'].status == 'capacity_failed'
    assert s.placement_failures == 1
    assert s.observed['edge'].measured_at >= 0


def test_background_increase_causes_deterministic_preemption():
    cfg = ReliabilityConfig(background=(BackgroundLoad('edge', 1, 1.5),),
                            max_restarts=0)
    s, result = simulate([job('a', cpu=1), job('b', cpu=1)], config=cfg, interval=1)
    assert result['b'].status == 'failed_interference'
    assert result['a'].on_time is False  # only 0.5 available after preemption, so a preempts too
    assert s.metrics()['interference_preemptions'] == 2
    assert s.actual['edge'] == pytest.approx(1.5)
    assert s.metrics()['job_cpu_utilization'] >= 0


def test_background_decrease_frees_actual_cpu_not_immediate_telemetry():
    cfg = ReliabilityConfig(background=(BackgroundLoad('edge', 2, 0),))
    s, result = simulate([job(arrival=1, cpu=1)], nodes=[node(used=2)], config=cfg,
                         interval=5)
    assert result['a'].status == 'rejected'  # last sample saw no spare CPU
    assert s.actual['edge'] == 0


def test_dispatch_delay_checks_actual_capacity_at_dispatch():
    cfg = ReliabilityConfig(dispatch_delay=1.5, background=(BackgroundLoad('edge', 1, 2),))
    s, result = simulate(config=cfg)
    assert result['a'].status == 'capacity_failed'
    assert s.metrics()['placement_failure_rate'] == 1


def test_dispatch_delay_counts_toward_deadline():
    cfg = ReliabilityConfig(dispatch_delay=2)
    _, result = simulate([job(deadline=5)], config=cfg)
    assert result['a'].status == 'deadline_missed'


def test_retry_after_placement_failure_can_recover():
    cfg = ReliabilityConfig(outages=(NodeOutage('edge', 0.5, 1.5),),
                            dispatch_delay=1, max_placement_retries=1,
                            retry_backoff=.5)
    s, result = simulate(config=cfg)
    assert result['a'].on_time
    assert result['a'].start == 2.5
    assert s.metrics()['placement_retries'] == 1


def test_external_change_does_not_overcommit():
    cfg = ReliabilityConfig(background=(BackgroundLoad('edge', 2, 2),))
    s, _ = simulate([job('a', duration=10), job('b', duration=10)], config=cfg)
    assert all(v >= -1e-9 for v in s.job_cpu_area.values())
    assert s.actual['edge'] <= s.nodes['edge'].cpu_capacity + 1e-9


def test_utilization_excludes_downtime_and_separates_background():
    cfg = ReliabilityConfig(outages=(NodeOutage('edge', 1, 3),), max_restarts=0)
    s, result = simulate([], nodes=[node(used=1)], config=cfg)
    # With no jobs and inferred horizon 0, no time integration occurs.
    assert s.metrics()['cpu_utilization'] == 0
    s = ReliabilitySimulator([node(used=1)], [], Greedy(), horizon=4, reliability=cfg)
    s.run()
    m = s.metrics()
    assert m['cpu_utilization'] == pytest.approx(.25)
    assert m['background_cpu_utilization'] == pytest.approx(.25)
    assert m['node_downtime_fraction'] == pytest.approx(.5)


def test_same_timestamp_completion_precedes_outage():
    cfg = ReliabilityConfig(outages=(NodeOutage('edge', 4, 6),), max_restarts=0)
    s, result = simulate(config=cfg)
    assert result['a'].on_time
    assert s.metrics()['outage_preemptions'] == 0


def test_out_of_order_telemetry_does_not_override_newest():
    cfg = ReliabilityConfig(background=(BackgroundLoad('edge', 1, 1),))
    impairment = Impairment(delays={('edge', 0): 5, ('edge', 1): 0})
    s = ReliabilitySimulator([node()], [job(arrival=6)], Greedy(),
                             impairment=impairment, reliability=cfg)
    s.run()
    assert s.observed['edge'].measured_at >= 5


def test_invalid_fault_definitions():
    with pytest.raises(ValueError):
        NodeOutage('edge', 3, 3)
    with pytest.raises(ValueError):
        BackgroundLoad('edge', 0, float('nan'))
    with pytest.raises(ValueError):
        ReliabilityConfig(dispatch_delay=-1)
    with pytest.raises(ValueError):
        ReliabilityConfig(max_restarts=True)
    with pytest.raises(ValueError):
        ReliabilityConfig(outages=(NodeOutage('edge', 1, 4), NodeOutage('edge', 3, 6)))
    with pytest.raises(ValueError):
        ReliabilityConfig(background=(BackgroundLoad('edge', 1, 0), BackgroundLoad('edge', 1, 1)))
    with pytest.raises(ValueError):
        ReliabilitySimulator([node()], [], Greedy(), reliability=ReliabilityConfig(
            background=(BackgroundLoad('edge', 1, 3),)))
    with pytest.raises(ValueError):
        ReliabilitySimulator([node()], [], Greedy(), reliability=ReliabilityConfig(
            outages=(NodeOutage('missing', 1, 2),)))


def test_reliability_is_deterministic():
    cfg = ReliabilityConfig(outages=(NodeOutage('edge', 1, 3),),
                            background=(BackgroundLoad('edge', 4, 1),))
    def run():
        s, results = simulate([job('a'), job('b', arrival=2)], config=cfg)
        return results, s.trace, s.metrics()
    assert run() == run()


def test_stress_study_pairs_and_counts():
    a = run_reliability_study([1, 2], base=Condition('tiny', jobs=10))
    b = run_reliability_study([1, 2], base=Condition('tiny', jobs=10))
    assert a == b and len(a) == len(scenarios()) * 2 * len(policies())
    assert all(0 <= r['on_time_rate'] <= 1 for r in a)
    assert all(r['job_cpu_utilization'] >= 0 for r in a)


def test_no_faults_job_results_independent_of_traces():
    cfg = ReliabilityConfig()
    s, result = simulate([job('a'), job('b', arrival=6)], config=cfg)
    assert all(r.on_time for r in result.values())
    assert s.metrics()['preemption_rate'] == 0
    assert s.metrics()['placement_retries'] == 0


def test_single_use():
    s = ReliabilitySimulator([node()], [job()], Greedy())
    s.run()
    with pytest.raises(RuntimeError):
        s.run()


def test_bounded_deferral_reserves_known_dispatch_latency():
    w = job(deadline=5, duration=4)
    from research.models import TelemetrySample
    sample = TelemetrySample('edge', 0, 0, 2, 0)
    policy = BoundedDeferral(5, 0.0, placement_latency=0.5)
    decision = policy.decide(w, 0, {'edge': sample}, 0)
    # Fresh telemetry at arrival means placement can start immediately.
    assert decision.node_id == 'edge'
    later = policy.decide(w, 0.1, {'edge': sample}, .1)
    assert later.wait_until == pytest.approx(.5)
    assert BoundedDeferral(5, 0.0, placement_latency=0).decide(w, .1, {'edge': sample}, .1).wait_until == 1
    with pytest.raises(ValueError):
        BoundedDeferral(1, 1, placement_latency=float('nan'))


def test_adjacent_outages_held_out_for_validation():
    with pytest.raises(ValueError):
        ReliabilityConfig(outages=(NodeOutage('edge', 1, 3), NodeOutage('edge', 3, 5)))


def test_outage_restart_waits_for_recovery_event_not_future_knowledge():
    cfg = ReliabilityConfig(outages=(NodeOutage('edge', 1, 7),), max_restarts=1)
    s, outcome = simulate([job(deadline=20)], config=cfg)
    assert outcome['a'].on_time
    assert (1, 'await_recovery', 'a') in s.trace
    assert outcome['a'].start == 7.5
    assert s.metrics()['recovered_jobs'] == 1


def test_outage_longer_than_deadline_reports_miss_not_restart_exhaustion():
    cfg = ReliabilityConfig(outages=(NodeOutage('edge', 1, 9),), max_restarts=2)
    s, result = simulate([job(deadline=9)], config=cfg)
    assert result['a'].status == 'deadline_missed'
    assert s.metrics()['outage_job_failure_rate'] == 0
    assert s.metrics()['not_on_time_rate'] == 1


def test_cli_manifest_contains_verified_csv_hashes(tmp_path):
    import hashlib
    import json
    from research.run_reliability import main

    output = tmp_path / 'study'
    main(['--seeds', '2', '--bootstrap', '30', '--output', str(output), '--no-plots'])
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['synthetic_only'] is True
    assert manifest['seed_count'] == 2
    assert len(manifest['scenarios']) == 5
    assert manifest['trials_sha256'] == hashlib.sha256((output / 'trials.csv').read_bytes()).hexdigest()
    assert manifest['paired_deltas_sha256'] == hashlib.sha256((output / 'paired_deltas.csv').read_bytes()).hexdigest()


def test_recovery_during_observed_stale_telemetry_is_not_oracle():
    # The policy cannot inspect outage schedules; the simulator enforces actual
    # offline state even when a delivered snapshot advertises spare capacity.
    class SpyPolicy:
        def __init__(self):
            self.seen = []
        def decide(self, workload, now, samples, waited):
            from research.policies import Decision
            self.seen.append((now, tuple((s.measured_at, s.received_at) for s in samples.values())))
            return Decision(node_id='edge') if 'edge' in samples else None

    spy = SpyPolicy()
    cfg = ReliabilityConfig(outages=(NodeOutage('edge', 1, 3),))
    s, result = simulate([job(arrival=2)], config=cfg, policy=spy, interval=10)
    assert result['a'].status == 'capacity_failed'
    assert spy.seen == [(2, ((0, 0),))]
    assert s.placement_failures == 1
