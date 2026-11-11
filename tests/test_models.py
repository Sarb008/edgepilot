import pytest

from research.models import NodeState, TelemetrySample, Workload


def test_node_available_cpu():
    assert NodeState('edge-1', 0, 4, 1.5).available_cpu == 2.5


def test_telemetry_age_includes_delivery_delay():
    sample = TelemetrySample('edge-1', 2, 5, 4, 1)
    assert sample.age_at(8) == 6
    assert sample.available_cpu == 3


def test_future_telemetry_is_inaccessible():
    sample = TelemetrySample('edge-1', 2, 5, 4, 1)
    with pytest.raises(ValueError, match='not yet been received'):
        sample.age_at(4)


def test_invalid_telemetry_order():
    with pytest.raises(ValueError, match='precedes'):
        TelemetrySample('edge-1', 5, 4, 4, 1)


@pytest.mark.parametrize('capacity,used', [(0, 0), (2, 3), (2, -1), (float('inf'), 1)])
def test_invalid_capacity(capacity, used):
    with pytest.raises(ValueError):
        NodeState('edge-1', 0, capacity, used)


def test_workload_creation():
    workload = Workload('job-1', 0, 10, 1, 2)
    assert workload.duration == 2


@pytest.mark.parametrize('deadline', [0, -1])
def test_invalid_deadline(deadline):
    with pytest.raises(ValueError):
        Workload('job-1', 0, deadline, 1, 2)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_numeric_input(value):
    with pytest.raises(ValueError):
        NodeState('edge-1', value, 4, 1)
    with pytest.raises(ValueError):
        Workload('job-1', 0, 10, value, 2)


def test_immutable():
    node = NodeState('edge-1', 0, 4, 1)
    with pytest.raises(AttributeError):
        node.cpu_used = 2


def test_invalid_identifiers():
    with pytest.raises(ValueError):
        NodeState(' ', 0, 4, 1)
    with pytest.raises(ValueError):
        Workload('', 0, 10, 1, 2)


def test_invalid_telemetry_query_time():
    sample = TelemetrySample('edge-1', 0, 0, 4, 1)
    with pytest.raises(ValueError):
        sample.age_at(float('nan'))
