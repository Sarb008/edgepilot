# EdgePilot v2 — Milestone 4: Paired sensitivity experiments

Add these files **without replacing** the existing Milestone 1–3 code:

- `research/study.py`
- `research/statistics.py`
- `research/figures.py`
- `research/run_study.py`
- `tests/test_study.py`

## Commands (PowerShell; from repo root with `.venv` active)

```powershell
python -m pytest -q
python -m research.run_study --seeds 30 --bootstrap 2000
```

Generated results (do not commit automatically):

- `research_outputs/sensitivity/trials.csv`: condition × seed × policy observations
- `research_outputs/sensitivity/paired_deltas.csv`: mean policy differences vs greedy and percentile paired bootstrap intervals
- `research_outputs/sensitivity/paired_deltas.png`: plot of policy differences

Add `research_outputs/` to your `.gitignore` to prevent committing generated files.

## Methods and meaning

- **Question:** Under which controlled synthetic conditions, if any, does bounded waiting improve the on-time completion fraction relative to immediate greedy placement?
- **Conditions:** Seven predeclared *one-factor-at-a-time* scenarios: baseline; lower/higher workload density; fresh/severely impaired telemetry; tighter/looser deadline slack. All other condition parameters remain fixed.
- **Pairing:** All four policies receive the *same* generated jobs, nodes, and impairment events for a given condition and seed. Workloads and telemetry use **independent seeded RNG streams**, so changing telemetry impairment does not change the job trace.
- **Fixed policies:** Greedy, AgePenalized(0.3), UncertaintyAware(0.3), BoundedDeferral(max_wait=2, freshness_threshold=0.75). No within-evaluation tuning.
- **Main measure:** on-time completion fraction over **all submitted jobs**. Failures due to admission rejection, deadline miss, or capacity failure all count as not on-time. Status-specific rates and CPU utilization are also exported. `mean_start_wait` is conditional on starting and therefore is *not* a population-wide waiting-time metric.
- **Uncertainty:** Differences are computed *per paired seed*, then bootstrap resampling **scenario seeds** yields descriptive percentile 95% intervals. Do not resample individual jobs as if independent. Intervals are not adjusted for multiple conditions/policies, do not prove causal effects in deployed systems, and are not p-values.
- **Reporting:** Prefer effect size, uncertainty interval, per-condition behavior, and methodological caveats; do not claim a win if the interval spans zero. Even an interval excluding zero is only evidence *within this synthetic model*.

## Limitations / priority for next milestone

This is a simplified simulation with abstract CPU units, fixed execution duration, fixed 3-node capacities, no CPU contention among admitted jobs, no process restarts, no external workload interference, no network partitions, no stale non-CPU signals, and no measured Kubernetes traces. A production or paper claim requires a model audit, validation against measured traces, meaningful cost/fairness metrics, and robustness across realistic distributions. One-factor-at-a-time scenarios do not reveal interaction effects (for example, load × freshness). The study is intentionally exploratory and not confirmatory.

The seed range defaults to `1000..1029`, different from Milestone 3 (`0..9`), to reduce accidental tuning to the pilot output. For a formal holdout design, register a test range and freeze the parameters before inspecting that range's results.
