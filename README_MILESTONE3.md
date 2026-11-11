# EdgePilot v2 — Milestone 3

Copy only these files into the existing repo:
- research/advanced.py
- research/baselines.py
- research/benchmark.py
- tests/test_advanced.py

Do not replace existing research/models.py, research/policies.py or research/simulator.py.

Run:
```
python -m pytest -q
python -m research.benchmark
```

Expected: 40 passing tests (27 existing + 13 new), and research_outputs/paired_baselines.csv.

Methodological notes:
- Every policy sees the same workload, node, delay and drop scenario for a given seed.
- Scheduler observes only delivered samples; actual capacity is checked separately.
- Out-of-order older samples cannot replace newer observed measurements.
- This is a synthetic, deterministic simulator, not a Kubernetes measurement.
- Metrics currently distinguish deadline_missed from rejected and capacity_failed; all three are failures for on_time_rate.
- No statistical significance or performance superiority is claimed.
- Current limitations: no external interference/restarts, fixed CPU-only workload model, simple uncertainty heuristic, no bootstrap confidence intervals, and no Kubernetes validation.
- research_outputs is generated data; add it to .gitignore if you do not want benchmark CSV files committed.
