# EdgePilot v2 — Phase 2: fixed validation (deadline: 19 November 2026)

**Do not extend this phase to search for positive results.** Freeze the conditions below before reading repeat-run results. Preserve both successful and failed Pod trials. This adds **two modules and one test file** to the existing research package; no original modules are replaced.

## Goal and scope

Question: *Under what controlled capacity, telemetry-latency and deadline conditions does bounded waiting provide information worth its opportunity cost relative to immediately dispatching with an age-penalized policy?*

The primary outcome is **observed on-time completion**. Secondary outcomes include fresh-reference request feasibility, timeout/refusal classifications, waiting, and dispatch overhead. Two distinct layers are reported **separately**:

1. **Fixed deterministic simulator matrix (324 matched cases, 648 policy runs).** Factorial: two workers × three change states (`unchanged`, `feasible_change`, `infeasible_change`) × telemetry delivery delays (0.25, 0.75, 2.0 seconds) × deadline timestamps (4, 5, 6 seconds) × max-wait limits (0.5, 1.5, 3 seconds) × known dispatch latency (0.2, 0.6 seconds). The initial observation and exogenous events are identical in both arms. **Not iid sampling; no p-values/CIs.** Workload arrival = 1.2 s and duration = 2 s. Fully specified per-case traces and SHA256 checksums are saved.
2. **Real local-kind crossover replication.** Two already-implemented policies, one worker perturbation per two-arm block. Your completed `wait_value_01` is the first run. Predeclare **exactly two further complete runs**, each `--repeats 2` and using different seeds `20261117` and `20261118`. The cluster runner allows a maximum of 16 disposable sleeping Pods per run; two new runs mean **at most 32 Pods**, sequentially, with verified per-trial cleanup. This is a within-protocol comparison, **not identical microstates or independent hardware**. All trials are retained; partial runs must be documented rather than silently excluded.

**Predeclared analysis:** Audit each run first, reject mismatched parameters, incomplete runs, duplicate paths/seeds, missing or tampered traces, absent cleanup, or changed worker identities; then produce one paired-block row per block, worker strata, means of intentional wait, exact win/loss/tie counts, and checksums. No causal estimates or confidence intervals from sequential single-host observations.

## 1. Extract and run the simulator study

From your activated PowerShell terminal in `D:\Development\edgepilot-v2`:

```powershell
python -m pytest -q
python -m research.phase2_sensitivity --output research_outputs/phase2_sensitivity_01
```

Outputs: `trials.csv`, `paired.csv`, `strata.csv`, `traces.jsonl`, `manifest.json`, `summary.json`, and `sensitivity.png`.

**Interpretation warning:** The fixed lexical worker preference makes `worker-a` sensitive to perturbation and `worker-b` less sensitive. Report worker-level strata; a positive aggregate is NOT evidence of tie-break-independent superiority. A feasible background change can leave the chosen node schedulable, and waiting then costs time without improving on-time completion. The snapshot is *delivered* telemetry, not secretly refreshed resource state.

## 2. Preflight for each live run

Before executing a run: ensure Docker Desktop and `kind-edgepilot-v2` are running, two workers are `Ready`, and the dedicated `edgepilot-v2-validation` namespace is empty:

```powershell
kubectl --context kind-edgepilot-v2 get nodes
kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation
```

The namespace is expected to report no Pods. Do **not** proceed if unexpected Pods exist. Every execution requires `--execute` explicitly and writes to a new output directory. If a run fails, inspect the namespace and preserve partial files rather than reusing the same output directory.

### Live run 2 (predeclared)

```powershell
python -m research.kube_wait_value --repeats 2 --seed 20261117 --output research_outputs/kubernetes/wait_value_02
python -m research.kube_wait_value --repeats 2 --seed 20261117 --output research_outputs/kubernetes/wait_value_02 --execute
```

### Live run 3 (predeclared)

Verify the cluster and namespace again, then:

```powershell
python -m research.kube_wait_value --repeats 2 --seed 20261118 --output research_outputs/kubernetes/wait_value_03
python -m research.kube_wait_value --repeats 2 --seed 20261118 --output research_outputs/kubernetes/wait_value_03 --execute
```

These runs use the **same deadline (`20s`) and workload/container/CPU-request configuration** as run 1. Do not retune based on trial outcomes. If one is interrupted, record that and consult the partial trace; don't silently substitute a favorable seed.

## 3. Offline pooled evidence report

After the two new runs complete:

```powershell
python -m research.phase2_real_report `
  --run research_outputs/kubernetes/wait_value_01 `
  --run research_outputs/kubernetes/wait_value_02 `
  --run research_outputs/kubernetes/wait_value_03 `
  --output research_outputs/kubernetes/phase2_real_report_01

Import-Csv .\research_outputs\kubernetes\phase2_real_report_01\worker_strata.csv | Format-Table -AutoSize
```

**Expected size, if all three runs succeed:** 3 runs × 4 paired blocks/run × 2 policies/block = **24 observed Kubernetes trials / 12 balanced blocks**. This count is a study *plan*, not a claimed achieved result. The first eight are already completed. The report never creates Pods or calls Kubernetes. It refuses to overwrite a prior report.

## 4. Phase 2 exit checklist (by 19 November)

- Predeclared full synthetic matrix completed, raw traces and plots saved.
- Both remaining balanced Kubernetes crossover runs attempted under the frozen protocol, or their blockers documented honestly.
- Strict offline analysis completed; raw evidence preserved unmodified.
- Report explicitly distinguishes simulator from Kubernetes, CPU requests from utilization, **observational timeout** from proof of permanent admission failure, and single-host kind from physical edge clusters.
- Any missing runs, worker-order bias, startup-image caching, controller execution costs, or negative findings recorded as limitations; **no unplanned repeats to chase significance**.
- Full tests pass; this batch is committed to `research-v2`.

## Commit

```powershell
git add research/phase2_sensitivity.py research/phase2_real_report.py tests/test_phase2.py README_PHASE2_NOV19.md
git commit -m "feat(research): preregister Phase 2 sensitivity and audit pooled kind trials"
git status
```

## Hard stop after Phase 2

Nov 20–22: correctness and reproducibility audit, code freeze 22 Nov. Nov 23–25: final paper-style report, figures, README, reproducibility guide, demo and release. **Final release 25 November 2026**. Exams take precedence; no new infrastructure is needed for this phase.
