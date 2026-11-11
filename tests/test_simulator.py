import pytest
from research.models import NodeState, Workload
from research.policies import Greedy, BoundedDeferral
from research.simulator import Simulator


def node(cap=2, used=0):
    return NodeState('edge', 0, cap, used)


def job(name='a', arrival=0, deadline=10, duration=2, cpu=1):
    return Workload(name, arrival, deadline, cpu, duration)


def test_greedy_completes():
    s = Simulator([node()], [job()], Greedy())
    assert s.run()['a'].on_time
    assert s.actual['edge'] == 0


def test_initial_delayed_telemetry_causes_rejection():
    s = Simulator([node()], [job()], Greedy(), telemetry_delay=1)
    assert s.run()['a'].status == 'rejected'


def test_deferral_can_wait_for_first_delivery():
    s = Simulator([node()], [job()], BoundedDeferral(2, 2), telemetry_delay=1)
    r = s.run()['a']
    assert r.on_time and r.start == 2


def test_no_deferral_past_deadline_budget():
    s = Simulator([node()], [job(deadline=2, duration=2)], BoundedDeferral(5, 0), telemetry_delay=1)
    assert s.run()['a'].status == 'rejected'


def test_stale_observation_does_not_override_actual_capacity():
    jobs = [job('a', 0, 10, 5, 2), job('b', 1, 10, 2, 2)]
    s = Simulator([node()], jobs, Greedy(), telemetry_interval=10)
    r = s.run()
    assert r['a'].on_time
    assert r['b'].status == 'capacity_failed'
    assert s.actual['edge'] == 0


def test_completion_releases_capacity_before_arrival_at_same_time():
    s = Simulator([node(1)], [job('a', 0, 5, 2), job('b', 2, 6, 2)], Greedy(), telemetry_interval=2)
    r = s.run()
    assert all(v.on_time for v in r.values())


def test_no_telemetry_future_leakage():
    s = Simulator([node()], [job()], Greedy(), telemetry_delay=3)
    assert s.run()['a'].status == 'rejected'


def test_repeated_runs_identical():
    def execute():
        s = Simulator([node()], [job()], BoundedDeferral(2, 1), telemetry_delay=1)
        return s.run(), s.trace
    assert execute() == execute()


def test_duplicate_ids_rejected():
    with pytest.raises(ValueError):
        Simulator([node(), node()], [], Greedy())
    with pytest.raises(ValueError):
        Simulator([node()], [job(), job()], Greedy())


def test_invalid_policy_wait_rejected():
    class Bad:
        def decide(self, workload, now, samples, waited):
            from research.policies import Decision
            return Decision(wait_until=now)
    with pytest.raises(ValueError):
        Simulator([node()], [job()], Bad()).run()
