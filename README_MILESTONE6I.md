# EdgePilot v2 — Milestone 6I: prospective tie-break crossover

**Status:** Source implementation and fake-cluster unit tests; **not yet executed on the user's kind cluster**. Existing experiments and metrics are unchanged. New files only.

## Motivation

M6H retrospectively examined 16 historical mixed-age inputs, all having exact two-worker CPU-capacity ties. Lexicographic greedy had 8/16 fresh-infeasible initial selections; reverse-lexicographic greedy also had 8/16, but under the opposite worker perturbation. Age-penalized had 0/16 retrospectively infeasible selections. These were **offline decision feasibility calculations**, not Kubernetes outcomes for reverse greedy.

M6I prospectively executes both greedy tie strategies to determine whether this symmetry also appears in observed kind scheduling. It additionally compares immediate age-penalized selection with bounded deferral to expose the cost of waiting under the same *protocol*.

## Arms and design

- `greedy_lex` — chooses the lexically first name only among workers tied for maximum reported available CPU.
- `greedy_reverse` — chooses the lexically last name **only among exact maximum-capacity ties**; otherwise chooses the same highest-CPU candidate as ordinary greedy.
- `age_penalized` — immediate, existing freshness/age-penalized policy, no deliberate wait.
- `bounded_deferral` — existing policy and bounded telemetry-refresh loop.

One repeat has **2 blocks × 4 arms = 8 sequential workload trials**. Block 1 perturbs the first named eligible worker; block 2 the other. Arm order is seeded and rotated independently of which worker is perturbed. A second repeat (`--repeats 2`) creates 4 blocks / 16 workload trials, and balances each arm across all four within-block positions. The protocol limits execution to **at most 32 disposable Pods** (one reservation + one workload Pod per trial), one at a time. Default is `--repeats 1` / at most 16 Pods.

Each trial captures an actual old Kubernetes resource-request snapshot, chooses a workload CPU request from *old* reported capacity, creates a 200m sleeping reservation Pod on one worker, and captures a fresh snapshot to verify the admission boundary. It withholds the new observation for the perturbed worker while supplying the fresh peer observation. **It never lies about measurement timestamps.** Exact two-way maximum-capacity ties are required; absence causes a fail-closed trial abort with recorded evidence and cleanup. Decision-making never receives the fresh evaluator-only target value. Bounded deferral may subsequently refresh telemetry while waiting.

A workload requests ~11.8 cores on the current kind configuration, but its container **sleeps**; CPU requests are Kubernetes scheduler reservations, not actual CPU consumption. We do not claim to have implemented a replacement for the default Kubernetes scheduler: placement is directed using a Pod `nodeSelector`, with the default scheduler enforcing admission.

**Critical limitations:** Policy arms have distinct real snapshots collected sequentially; they are not simultaneous trials on exactly the same telemetry. Fresh treatment feasibility is retrospective and observed timeouts are censored observations, not definitive proof of permanent admission failure. This is a small single-host kind pilot, not a field trial, a latency-tail estimate, or causal evidence about real edge devices.

## Setup (Windows PowerShell)

Extract this ZIP into `D:\Development\edgepilot-v2`, preserving `research/` and `tests/` directories and the root README. Do not replace original directories wholesale.

```powershell
cd D:\Development\edgepilot-v2
python -m pytest -q
```

Expected after correct extraction: **420 passed** (368 previous + 52 new fake-cluster tests). No additional pip dependencies.

### Dry run (no cluster I/O)

```powershell
python -m research.kube_tie_crossover --repeats 1 --seed 20261115
```

The plan prints eight sequential trials and at most 16 disposable Pods. It does not read or write Kubernetes or create output files.

### Confirm cluster and namespace before executing

```powershell
kubectl --context kind-edgepilot-v2 get nodes
kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation
```

Both `edgepilot-v2-worker` and `edgepilot-v2-worker2` must be Ready and the dedicated validation namespace must contain **no Pods**. Docker Desktop and the kind cluster must be running.

### Execute real-cluster pilot (explicit opt-in)

```powershell
python -m research.kube_tie_crossover `
    --repeats 1 `
    --seed 20261115 `
    --output research_outputs/kubernetes/tie_crossover_01 `
    --execute
```

Creates up to 16 disposable Pods sequentially, only in `kind-edgepilot-v2` / `edgepilot-v2-validation`. Uses real CPU-request admission and Pod results. Every attempted Pod is deleted by exact name, and namespace emptiness is verified before proceeding. It refuses to overwrite any existing evidence path and stops on unexpected errors or cleanup uncertainty. **If it stops, inspect `kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation` before any further trial.** Never delete unknown Pods or delete the namespace as a shortcut.

Outputs: `manifest.json`, `trials.csv`, `summary.json`, and JSON traces for every attempted trial. Failed/incomplete evidence remains for diagnosis and must not be treated as a complete experimental run.

### Audit (offline, no cluster I/O)

```powershell
python -m research.kube_tie_crossover_analysis `
    --run research_outputs/kubernetes/tie_crossover_01 `
    --output research_outputs/kubernetes/tie_crossover_audit_01

Import-Csv .\research_outputs\kubernetes\tie_crossover_audit_01\worker_strata.csv |
    Format-Table -AutoSize

kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation
```

The auditor verifies a complete seeded plan, trace/CSV correspondence, the constructed stale/fresh request boundary, strict two-way ties, announced initial actions, selected-worker feasibility at the treatment snapshot, and cleanup confirmations. It computes SHA-256 checksums of evidence. It **does not** run Pods, infer counterfactual outcomes or generate misleading confidence intervals from a tiny sample.

After verification, commit the source only:

```powershell
git add research/kube_tie_crossover.py research/kube_tie_crossover_analysis.py tests/test_kube_tie_crossover.py README_MILESTONE6I.md
git commit -m "feat(research): add prospective tie-controlled Kubernetes crossover and offline audit"
git status
```

`research_outputs/` should remain ignored as generated evidence. Keep the raw local evidence for reproducibility; do not present it as independent hardware validation.

## What to evaluate next

1. Do the *observed* greedy tie variants switch which perturbed worker produces `Insufficient cpu` and timeouts, under a balanced design?
2. How much waiting does bounded deferral incur relative to immediate age-penalized scheduling, and is there any observed completion benefit?
3. Are results sensitive to actual telemetry freshness, CPU-request margins, and deadline budget? Those parameters should be declared **before** executing future sweeps, not cherry-picked afterward.
4. Broader external validity still requires a realistic workload, independently resource-constrained nodes, naturally delayed telemetry and repeated randomized trials.
