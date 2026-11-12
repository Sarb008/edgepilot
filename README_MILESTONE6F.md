# EdgePilot v2 — Milestone 6F: balanced Kubernetes replication and stratified audit

## Grounded finding from Milestone 6E

The three-block audit combined 12 trials, with 2 perturbations of `edgepilot-v2-worker` and 1 of `edgepilot-v2-worker2`. On-time counts were greedy **1/3**, age-penalized **3/3**, bounded deferral **3/3**, and uncertainty-aware **0/3** (three deliberate refusals). Two greedy timeouts had recorded `Insufficient cpu` scheduling evidence. This supports a controlled *mechanism demonstration*, not a general causal effect. The first one-block pilot is **unbalanced**, and the Kubernetes nodes are containers sharing one host.

## What's added

- `research/kube_balanced_replication.py`: default **dry run**; with `--execute`, orchestrates **one or two full two-block M6D experiments**. Every full run perturbs both eligible workers once. At most **32 disposable Pods**, sequentially, across two runs. The cluster context and namespace are locked to the disposable `kind-edgepilot-v2` context and `edgepilot-v2-validation` namespace.
- `research/kube_stratified_analysis.py`: strictly **offline**. Reuses the M6E evidence auditor, rejects incomplete or inconsistent run evidence, requires equal numbers of blocks for both workers, reports per-worker policy outcomes and *descriptive* per-block differences versus greedy. Does not contact Kubernetes.
- `tests/test_kube_balanced_replication.py`: validation and failure-safety tests, including complete fake-Kubernetes runs, interrupted-run preservation, and no-overwrite behavior.

## Safety and interpretation

1. **No cluster calls or files on dry run.** `--execute` is required for all live work.
2. Check `kubectl --context kind-edgepilot-v2 get nodes` and `kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation` before execution. Both workers must be Ready and the dedicated namespace must be empty.
3. One two-block run = **8 policy trials**, up to **16 sequential disposable Pods** (one CPU reservation sleeper and, when placed, one workload sleeper for each trial). Two runs = up to 32 Pods. CPU requests near capacity do **not** imply CPU utilization; these Pods sleep.
4. The runner calls the already-tested M6D experiment and evidence auditor for each run, checks actual namespace emptiness, and stops after an error or cleanup uncertainty. Existing output directories are **never overwritten**. A partial run is preserved for inspection, not resumed automatically.
5. All Pods are bound by node selectors and scheduled by default kube-scheduler. This is not a production custom scheduler or an edge hardware study. Snapshots are non-atomic Kubernetes API request accounting.
6. Balanced worker exposure is necessary but insufficient: policy order is sequential and the first worker may be favored by greedy tie-breaking. Do **not** interpret small-sample descriptive percentages as causal effects. Age-penalized versus bounded deferral remains unresolved.

## 1. Extract and verify

Extract the ZIP into `D:\Development\edgepilot-v2` with folder structure intact. No existing Python files are replaced. Then:

```powershell
cd D:\Development\edgepilot-v2
python -m pytest -q
```

Expected: **296 passed** (258 earlier + 38 new).

## 2. Analyze the already existing balanced two-block run — no cluster needed

Do **not** include `feasibility_pilot` (the one-block initial run) in this *balanced* analysis:

```powershell
python -m research.kube_stratified_analysis `
  --run research_outputs/kubernetes/feasibility_repeat_01 `
  --output research_outputs/kubernetes/worker_strata_previous
```

The offline report writes `summary.json`, `worker_strata.csv`, and `block_pairs.csv` in a new directory.

## 3. Preview one NEW balanced run

```powershell
python -m research.kube_balanced_replication `
  --repeats 1 `
  --seed 20261114 `
  --output research_outputs/kubernetes/balanced_replication_01
```

This prints a fixed plan: **8 policy trials, up to 16 Pods**, across two blocks. **No Kubernetes reads/writes** occur in dry-run mode.

## 4. Run (explicitly opt in)

After confirming the kind cluster is healthy, and the validation namespace contains zero Pods:

```powershell
kubectl --context kind-edgepilot-v2 get nodes
kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation

python -m research.kube_balanced_replication `
  --repeats 1 `
  --seed 20261114 `
  --output research_outputs/kubernetes/balanced_replication_01 `
  --execute
```

The runner writes: `replication_manifest.json`, `run_01/manifest.json`, `run_01/trials.csv`, `run_01/traces/`, and `stratified_report/`. It preserves every record and fails closed if evidence validation or cleanup fails. Never rerun into an existing output directory; choose a different name for any additional replication.

## 5. Audit the two balanced runs together — offline

This excludes the unbalanced initial `feasibility_pilot` from the balanced comparison:

```powershell
python -m research.kube_stratified_analysis `
  --run research_outputs/kubernetes/feasibility_repeat_01 `
  --run research_outputs/kubernetes/balanced_replication_01/run_01 `
  --output research_outputs/kubernetes/worker_strata_combined_02

Import-Csv .\research_outputs\kubernetes\worker_strata_combined_02\worker_strata.csv |
  Format-Table -AutoSize
```

Expected **16 trials / 4 blocks**, with **2 perturbations per worker** if the experiment completes and workers stay consistent. These results are still exploratory: not independent cluster samples or unbiased randomized counterfactuals.

## 6. Commit after the test suite passes

```powershell
git add research/kube_balanced_replication.py research/kube_stratified_analysis.py tests/test_kube_balanced_replication.py README_MILESTONE6F.md
git commit -m "feat(research): add balanced Kubernetes replication and worker-stratified audit"
git status
```

Keep `research_outputs/` outside Git as before. Preserve/back up original JSON/CSV traces separately if planning a paper or reproducible release.

## Next research problem

The balanced design helps expose **worker-identity/tie-break confounding**. The next protocol should explicitly vary or randomize deterministic tie-breaking, compare age-aware dispatch against bounded waiting under identical deadline budgets, record scheduler-unschedulable intervals (not just poll timeouts), and measure impact of multiple independent Kubernetes node configurations before asserting external validity. Do not tune settings simply to produce an improvement.
