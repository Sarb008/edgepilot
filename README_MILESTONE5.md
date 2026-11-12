# EdgePilot v2 — Milestone 5: Reliability Stress Testing

**Scope:** Additional synthetic, policy-paired tests of whether bounded waiting is useful when workloads face dispatch/admission latency, node outages, competing CPU reservations, and full restarts. This batch **does not** assert a performance improvement and does not produce Kubernetes measurements.

## Integration (Windows PowerShell)

1. Use the existing `D:\Development\edgepilot-v2` repository with the `research-v2` branch, an activated Python 3.13 virtual environment, and Milestone 4 committed.
2. Extract the archive at the repository root, preserving `research/` and `tests/`. **One existing file is intentionally updated:** `research/policies.py` to make bounded deferral reserve a *known* dispatch latency. The other five files are additions.
3. Run:

   ```powershell
   python -m pytest -q
   python -m research.run_reliability --seeds 30 --bootstrap 2000
   ```

4. The output folder is `research_outputs/reliability/`. Make sure `research_outputs/` is listed in `.gitignore`.

## Files

- `research/reliability.py`: Validated outage/background-event configuration, event-driven reliable simulator with dispatch latency, bounded restarts, capacity checks, and job/CPU accounting.
- `research/reliability_study.py`: Five predeclared fault conditions, matched over the same workload/telemetry streams and four policies.
- `research/run_reliability.py`: CLI runner, raw trial CSV, paired estimates, figure, reproducibility manifest.
- `research/policies.py`: Updated bounded-deferral policy with optional `placement_latency=0` by default (backward compatible).
- `tests/test_reliability.py`: Regression tests for zero-fault parity, duplicate events, event precedence, no ghost completion, capacity invariants, and fault attribution.
- `README_MILESTONE5.md`: This document.

## Event semantics (explicit model assumptions)

At identical timestamps, process (1) completion, (2) outage start, (3) competing background CPU update, (4) outage end, (5) measurement, (6) telemetry delivery, (7) workload arrival, (8) wake/retry/restart, and (9) delayed dispatch. Events are deterministic and single-threaded, not real-time parallel operations.

- **Truth vs observation:** Scheduling policies receive only the latest *already delivered* `TelemetrySample`. An outage is hidden from the policy unless indirectly reflected through measured CPU; node-health telemetry is **not yet modeled**. At actual dispatch, the simulator checks whether the node is online and whether **real** CPU capacity is sufficient.
- **Telemetry:** A sample captures actual reserved CPU at measurement time, travels through the pre-existing delay/drop model, and cannot replace a newer measurement after arriving out of order.
- **Dispatch latency:** The policy selects a node at decision time, but actual admission occurs after `dispatch_delay`. Bounded deferral uses the same *known* latency in its latest-admissible-decision calculation. It does **not** know future telemetry or node outages.
- **Outages:** The node becomes unavailable and all active jobs on it are preempted. A preempted job awaits a **recovery event**; the simulator does not schedule a restart based on knowing the future recovery timestamp. At recovery, it may be queued for a full restart after `restart_backoff`, subject to deadline and restart limits.
- **Competing CPU:** Background demand is exogenous. If a change would exceed physical CPU capacity, running jobs are preempted, **most recently started first**, until the invariant is restored. Background demand is not counted as completed EdgePilot workload.
- **Retries:** Placement failures are counted and may be retried after `retry_backoff`, up to `max_placement_retries`. A preemption consumes one restart allowance. Restarts lose prior execution progress, do not use checkpoints, and repeat the full declared `duration`. No hidden retries occur.
- **Event validity:** Overlapping or touching outage intervals on the same node, negative/nonfinite values, unknown nodes, and background CPU demands above node capacity are rejected.
- **Job results:** `completed` means finished on or before the deadline. `deadline_missed` means no on-time result was achieved within the model's feasible timeline, `rejected` means the scheduler returned no placement, `capacity_failed` means admission retries were exhausted, and `failed_outage`/`failed_interference` mean the allowed **restart** count was exhausted. Thus `deadline_miss_rate` is an explicit status category, while `not_on_time_rate = 1 - on_time_rate` includes **all failures**.
- **CPU utilization:** The stress simulator reports job CPU utilization, background CPU utilization, total CPU utilization, and downtime separately. CPU during node outages is not counted as useful occupancy; the denominator remains nominal node capacity over the whole horizon.

## Reproducible design

The stress study uses five fixed conditions: `nominal`, `dispatch_latency`, `node_outages`, `external_contention`, and `combined_stress`. Each `(seed, condition)` produces **the same exogenous trace** for greedy, age-penalized, uncertainty-aware, and bounded-deferral policies. Policies may cause different realized load or different preemptions; the background and outage schedules are never selected based on the results.

By default, 30 seeds (2000–2029) × five scenarios × four policies = **600 trials**. Metrics are exported per trial. Paired per-seed percentage-point on-time deltas and percentile bootstrap intervals compare against greedy. These are exploratory intervals, without multiple-testing correction, and do not establish causal validity beyond the simulation assumptions.

## Run / commit commands

```powershell
cd D:\Development\edgepilot-v2
python -m pytest -q
python -m research.run_reliability --seeds 30 --bootstrap 2000

git add research/policies.py research/reliability.py research/reliability_study.py research/run_reliability.py tests/test_reliability.py README_MILESTONE5.md
git commit -m "feat(research): add reliability stress simulation and paired study"
git status
```

**Interpretation warning:** The initial stress results in this batch can be worse for bounded deferral, especially under outages and interference. That is a valid negative result, not something to hide or automatically tune away. Real-system (e.g. Kubernetes) validation and independently reviewed fault and deadline models remain future work.
