import csv
from dataclasses import replace
from pathlib import Path
import pytest

from research.benchmark import export
from research.figures import plot_deltas
from research.run_study import main
from research.statistics import paired_bootstrap, summarize_paired
from research.study import Condition, conditions, generate, run_study


def test_predeclared_conditions_unique_and_have_baseline():
    cs = conditions()
    assert len(cs) == 7
    assert len({c.name for c in cs}) == len(cs)
    assert cs[0].name == 'baseline'


def test_seeded_generation_deterministic():
    assert generate(123, Condition('test')) == generate(123, Condition('test'))


def test_telemetry_change_keeps_same_workloads():
    original = Condition('original', jobs=8)
    modified = replace(original, name='modified', delay_base=5, drop_probability=0.5)
    na, wa, ia = generate(44, original)
    nb, wb, ib = generate(44, modified)
    assert na == nb and wa == wb and ia != ib


def test_all_drop_rate_one():
    _, _, imp = generate(1, Condition('dropped', jobs=3, drop_probability=1))
    assert imp.drops and not imp.delays


@pytest.mark.parametrize('field,value', [
    ('jobs', 0), ('delay_base', -1), ('delay_jitter', float('nan')),
    ('drop_probability', 1.1), ('telemetry_interval', 0),
    ('arrival_start', 100), ('slack_multiplier', -1),
])
def test_invalid_conditions(field, value):
    with pytest.raises(ValueError):
        replace(Condition('check'), **{field: value})


def test_paired_study_count_and_repeatability():
    cs = [Condition('tiny', jobs=6)]
    rows = run_study([2, 3], cs)
    assert len(rows) == 8
    assert rows == run_study([2, 3], cs)
    assert all(0 <= r['on_time_rate'] <= 1 for r in rows)
    assert set(r['policy'] for r in rows) == {
        'greedy', 'age_penalized', 'uncertainty_aware', 'bounded_deferral'
    }


def test_duplicate_seed_rejected():
    with pytest.raises(ValueError, match='unique'):
        run_study([2, 2], [Condition('tiny', jobs=3)])


def test_bootstrap_constant_and_deterministic():
    assert paired_bootstrap([2, 2, 2], samples=40) == (2, 2, 2)
    assert paired_bootstrap([-3, 0, 4], samples=200) == paired_bootstrap([-3, 0, 4], samples=200)


def test_bootstrap_bad_inputs():
    with pytest.raises(ValueError):
        paired_bootstrap([])
    with pytest.raises(ValueError):
        paired_bootstrap([1, float('nan')])
    with pytest.raises(ValueError):
        paired_bootstrap([1, 2], samples=0)


def test_known_paired_differences():
    data = [
        {'condition': 'c', 'seed': seed, 'policy': p, 'on_time_rate': value}
        for seed in (1, 2, 3)
        for p, value in [('greedy', 0.4), ('bounded_deferral', 0.5)]
    ]
    s = summarize_paired(data, bootstrap_samples=50)
    target = next(r for r in s if r['policy'] == 'bounded_deferral')
    assert target['delta_percentage_points'] == pytest.approx(10)
    assert target['ci95_lower_pp'] == pytest.approx(10)
    assert target['ci95_upper_pp'] == pytest.approx(10)


def test_missing_pairs_rejected():
    rows = [
        {'condition': 'c', 'seed': 1, 'policy': 'greedy', 'on_time_rate': 0.5},
        {'condition': 'c', 'seed': 2, 'policy': 'greedy', 'on_time_rate': 0.6},
        {'condition': 'c', 'seed': 1, 'policy': 'bounded_deferral', 'on_time_rate': 0.7},
    ]
    with pytest.raises(ValueError, match='unpaired'):
        summarize_paired(rows)


def test_duplicate_trial_rejected():
    row = {'condition': 'c', 'seed': 1, 'policy': 'greedy', 'on_time_rate': 0.5}
    with pytest.raises(ValueError, match='duplicate'):
        summarize_paired([row, row])


def test_export_summary_and_plot(tmp_path):
    rows = run_study([1, 2], [Condition('tiny', jobs=6)])
    summary = summarize_paired(rows, bootstrap_samples=50)
    export(summary, tmp_path / 'summary.csv')
    with (tmp_path / 'summary.csv').open(newline='', encoding='utf-8') as f:
        assert len(list(csv.DictReader(f))) == 4
    plot_deltas(summary, tmp_path / 'figure.png')
    assert (tmp_path / 'figure.png').stat().st_size > 1000


def test_cli_small_study(tmp_path):
    # Use the CLI runner on one-few conditions by replacing the exported study driver.
    # The full 7-condition CLI is also intentionally small with two seeds.
    main(['--seeds', '2', '--bootstrap', '25', '--output', str(tmp_path), '--no-plots'])
    assert (tmp_path / 'trials.csv').exists()
    assert (tmp_path / 'paired_deltas.csv').exists()
