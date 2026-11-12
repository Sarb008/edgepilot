# EdgePilot v2 — Deadline Sprint / Phase 1: Value of Waiting

**Project freeze target:** 25 November 2026. **Phase 1 research checkpoint:** 15 November 2026.

**Release nature:** additional files only, layered onto the already-committed M6I (`8ec2147`) research branch. Existing v1/v2 implementations are not replaced. Python 3.13 compatible. No new third-party dependencies.

## Research question and a genuine stopping criterion

> Under which specified telemetry delays, capacity-change scenarios, and deadline budgets does bounded deferral outperform immediate age-aware scheduling **in on-time completion**, and when does it only add latency?

This study does NOT claim waiting is always better. **Phase 1 is done** once (1) identical-input synthetic mechanism experiments have run with full traces; (2) an opt-in, two-arm real Kubernetes pilot has produced real observations or a documented, bounded environmental failure; (3) a separate offline auditor has assessed each complete run. When bounded deferral offers no incremental benefit, report that outcome and move to Phase 2 rather than indefinitely changing conditions.

## Files added

- `research/voi_study.py` — a fixed **2 workers × 2 capacity states × 4 telemetry delays × 3 deadlines = 48**-case factorial. Runs 2 policies in each case = 96 deterministic paired policy runs. Uses the existing reliability/event simulator, unchanged.
- `research/kube_wait_value.py` — an explicit `--execute` Kubernetes pilot. Each arm initially receives the **same old two-worker snapshot within its trial**. Later snapshots are obtained only by waiting. Arm protocol, worker perturbation, and trial position are rotated. Uses default kube-scheduler plus `nodeSelector`, not a new scheduler plugin.
- `research/kube_wait_value_analysis.py` — strictly **offline** evidence auditor: checks manifest plan, exact CSV/trace agreement, timestamps, CPU-request perturbation, pre-change initial observation, policy initial decisions, cleanup confirmation, and run completeness. Records SHA-256 checksums.
- `tests/test_voi_study.py` and `tests/test_kube_wait_value.py` — simulator mechanism controls, deterministic full matrix, no-future-input checks, fake Kubernetes execution, cleanup and invalid-config tests, evidence-tampering tests.

## Step 1 — Tests

Extract this package **into** `D:\Development\edgepilot-v2`, merging `research/` and `tests/`; do not replace those directories. Keep your existing `.venv` active.

```powershell
cd D:\Development\edgepilot-v2
python -m pytest -q
```

Expected when integrated with the existing M6I code: **497 passed** (420 previously verified + 77 new tests).

## Step 2 — Deterministic mechanism study (NO Kubernetes calls)

```powershell
python -m research.voi_study --output research_outputs/voi_study_01
```

The fixed matrix deliberately includes **negative controls** (no capacity change and the other worker perturbed), varying telemetry delivery delays (0.25, 0.5, 1, 2 s) and deadlines (4, 5, 6 s). Both policy runs in each pair share identical predeclared external events and the exact same initial delivered samples (SHA-256 hash checked). Policies never receive actual/future state. This is a controlled synthetic mechanism analysis, not an external performance estimate.

**Expected local fixture result** (verify on your machine; do not copy numbers without running):

| Deterministic synthetic condition | Age-penalized immediate | Bounded deferral |
|---|---:|---:|
| 24 no-change cases | 24 on time | 24 on time |
| 24 capacity-change cases | 12 on time | 21 on time |
| All 48 predeclared cases | 36 on time | 45 on time |

The **9 beneficial cases** are from a specifically constructed informative-delay regime, not statistical evidence over independently sampled workloads. Waiting's mean additional cost in the full matrix is ~0.671 simulated seconds, even though both policies complete unchanged cases. A telemetry update that arrives too late can eliminate that benefit. No p-values or confidence intervals are appropriate for this deterministic fixed matrix.

Output (a NEW directory; overwrite is forbidden):

```
research_outputs/voi_study_01/
  manifest.json
  trials.csv
  paired.csv
  summary.json
  traces.jsonl
```

## Step 3 — Preview real-cluster experiment (NO Kubernetes calls)

```powershell
python -m research.kube_wait_value --repeats 2 --seed 20261116
```

This previews **8 workloads in 4 worker-balanced blocks**, up to **16 disposable Pods** (one 200m sleeping reservation Pod and at most one near-capacity sleeping probe Pod per trial). Each worker is perturbed twice, with each policy in both arm-order positions.

Why this specific design? Prior M6I delivered a fresh peer snapshot to the immediate age-penalized policy, so it made an informed immediate choice. This test gives **both workers the old pre-perturbation measurement at initial decision time**, forcing both policies to confront the same unknown capacity change. The immediate policy must choose; bounded deferral may wait and obtain **a new actual snapshot at a later time**, with elapsed waiting counted against its deadline. No timestamp is fabricated.

**Important limitation:** The two arms do **not** share literally identical cluster state across separate sequential Pod executions. This is a *matched-protocol*, worker-balanced pilot; only the synthetic study is exact common-input/event matched. It is not a natural network-delay benchmark or full causal comparison.

## Step 4 — Execute after checking the isolated kind cluster

You must opt in to workload creation. Check Docker Desktop is running, all nodes are `Ready`, and the dedicated namespace is empty:

```powershell
kubectl --context kind-edgepilot-v2 get nodes
kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation
```

If the namespace contains Pods, **do not execute**. Inspect; do not delete unknown workloads. If healthy and empty:

```powershell
python -m research.kube_wait_value `
  --repeats 2 `
  --seed 20261116 `
  --output research_outputs/kubernetes/wait_value_01 `
  --execute
```

The runner is restricted to the explicitly named kind context and dedicated validation namespace. It creates at most 16 Pods sequentially; uses `sleep` (not CPU-burning code); chooses the high CPU request using *pre-intervention* observations; requires an exact pre-intervention tie; checks the intervention moved 200m of Kubernetes CPU **requests**; and fails closed if the capacity boundary disappears. It tries exact-name cleanup and verifies that the namespace is empty after each trial. It refuses to overwrite any existing evidence directory.

If interrupted or any trial errors, inspect `kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation` and do not reuse an incomplete output directory. Timeouts are **censored observation outcomes**; preserve the recorded `Insufficient cpu` event evidence if it exists, but don't imply all timeouts prove permanent admission failure.

## Step 5 — Offline independent audit

Only after the full real run completes:

```powershell
python -m research.kube_wait_value_analysis `
  --run research_outputs/kubernetes/wait_value_01 `
  --output research_outputs/kubernetes/wait_value_audit_01

Import-Csv .\research_outputs\kubernetes\wait_value_audit_01\audited_trials.csv |
  Format-Table -AutoSize

kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation
```

Auditor needs no Kubernetes tools: it reads existing JSON/CSV and checks initial two-worker sample equality, original timestamps, intervention size, initial policy choices, trace correspondence, and cleanup. It will refuse an incomplete or inconsistent run.

## Step 6 — Commit source and keep evidence

```powershell
git add research/voi_study.py research/kube_wait_value.py research/kube_wait_value_analysis.py tests/test_voi_study.py tests/test_kube_wait_value.py README_PHASE1_NOV15.md SPRINT_PLAN_NOV25.md
git commit -m "feat(research): evaluate waiting versus immediate age-aware scheduling"
git status
```

`research_outputs/` remains local and gitignored. Back up raw evidence separately before wiping the kind cluster; do not commit oversized experiment data unintentionally.

## Boundaries and interpretation

- The deterministic value-of-information study isolates a *mechanism*, not a field effect or a real-systems latency estimate.
- The Kubernetes pilot measures CPU **request** placement, not CPU utilization or a production custom scheduler.
- Both kind worker nodes share one physical computer. It is not geographically distributed edge-cloud hardware.
- The policy arms execute sequentially; nonstationary image caching and host activity may bias timings.
- Bounded waiting may add time without improving any outcome. A successful wait is not by itself evidence of a better policy than an immediate age-aware baseline.
- In a failed case, distinguish no eligible decision, observed timeout, recorded unschedulability evidence, and terminal Pod failure.
- Once these tests are run and audited, **freeze Phase 1**. Any non-critical new idea belongs in post-November-25 future work.
