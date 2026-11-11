import pytest
from research.advanced import ResearchSimulator, Impairment
from research.baselines import AgePenalized, UncertaintyAware
from research.benchmark import compare, export, scenario
from research.models import NodeState, TelemetrySample, Workload
from research.policies import BoundedDeferral, Greedy


def n(cap=2):
    return NodeState('edge', 0, cap, 0)


def w(name='job', arrival=0, deadline=10, duration=2, cpu=1):
    return Workload(name, arrival, deadline, cpu, duration)


def test_telemetry_wakes_before_timeout():
    s = ResearchSimulator([n()], [w()], BoundedDeferral(5, 1.1), telemetry_interval=10, telemetry_delay=1)
    r = s.run()['job']
    assert r.start == 1 and r.on_time


def test_no_future_telemetry():
    s = ResearchSimulator([n()], [w()], Greedy(), telemetry_delay=2)
    assert s.run()['job'].status == 'rejected'


def test_drop_prevents_early_delivery():
    s = ResearchSimulator([n()], [w()], Greedy(), impairment=Impairment(drops=frozenset({('edge', 0)})))
    assert s.run()['job'].status == 'rejected'


def test_capacity_accounting_and_failure():
    s = ResearchSimulator([n(1)], [w('a', duration=5), w('b', arrival=1)], Greedy(), telemetry_interval=10)
    result = s.run()
    assert result['a'].on_time and result['b'].status == 'capacity_failed'
    assert s.actual['edge'] == 0 and s.metrics()['placement_failure_rate'] == 0.5


def test_completion_before_same_timestamp_arrival():
    s = ResearchSimulator([n(1)], [w('a', duration=2), w('b', arrival=2)], Greedy(), telemetry_interval=2)
    assert all(r.on_time for r in s.run().values())


def test_stale_out_of_order_sample_cannot_replace_newer():
    impairment = Impairment(delays={('edge', 0): 4, ('edge', 1): 0})
    s = ResearchSimulator([n()], [w(arrival=5)], Greedy(), impairment=impairment)
    s.run()
    assert s.observed['edge'].measured_at >= 4


def test_determinism():
    def run():
        s = ResearchSimulator([n()], [w()], BoundedDeferral(3, 1), telemetry_delay=1)
        return s.run(), s.trace, s.metrics()
    assert run() == run()


def test_baselines_only_choose_delivered_candidates():
    sample = TelemetrySample('edge', 0, 1, 2, 0)
    for policy in (AgePenalized(0.1), UncertaintyAware(0.1)):
        assert policy.decide(w(), 2, {'edge': sample}, 0).node_id == 'edge'
        assert policy.decide(w(cpu=3), 2, {'edge': sample}, 0) is None


def test_uncertainty_reserve_blocks_stale_capacity():
    sample = TelemetrySample('edge', 0, 0, 2, 0)
    assert UncertaintyAware(1).decide(w(cpu=1), 2, {'edge': sample}, 0) is None


def test_invalid_impairment():
    with pytest.raises(ValueError):
        Impairment(delays={('edge', 0): float('nan')})


def test_paired_benchmark_deterministic(tmp_path):
    a, b = compare(range(2)), compare(range(2))
    assert a == b and len(a) == 8
    export(a, tmp_path / 'paired.csv')
    assert (tmp_path / 'paired.csv').read_text().count('\n') == 9


def test_scenario_shared_inputs():
    assert scenario(42) == scenario(42)


def test_single_use():
    s = ResearchSimulator([n()], [w()], Greedy())
    s.run()
    with pytest.raises(RuntimeError):
        s.run()
