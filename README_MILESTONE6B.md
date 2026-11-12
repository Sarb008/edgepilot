# EdgePilot v2 — Milestone 6B: Real-Kubernetes Sequential Policy Pilot

## Scope and honest limits

Milestone 6A measured a real disposable kind Pod successfully, with ten more
successful single-Pod replications on the user's local cluster. M6B introduces a
**sequential matched-workload policy pilot**; it is NOT a genuine simultaneous
paired experiment, a custom Kubernetes scheduler, or a field validation of
independent edge/cloud hosts. The default Kubernetes scheduler binds Pods;
EdgePilot selects a hostname through `nodeSelector`.

There are four policies: greedy, age-penalized, bounded deferral, and
uncertainty-aware. Each trial uses the same CPU request, image, command,
container duration and end-to-end deadline. Each trial captures its own
read-only Kubernetes snapshot. Bounded deferral can repeatedly refresh
telemetry until a known deadline budget or maximum wait expires. It can never
see samples from the future. Decisions, selected node, node actually used,
telemetry read count, intentional wait, Pod startup, execution and cleanup are
written to CSV.

**Do not misinterpret this pilot:**

1. Two kind workers share one physical host. The 12 CPU-core allocatable
   values are separately advertised for each containerized node and cannot be
   added into 24 independent physical CPU cores.
2. A shared snapshot assigns the same conservative measurement age to every
   node. Age-penalized ranking can therefore **collapse to greedy ranking**
   when age penalties are identical. Real policy differentiation requires
   explicitly designed per-node stale telemetry and/or controlled contention.
3. CPU reservations from Pod requests are **not CPU utilization** or an actual
   measurement of free CPU cycles. Memory pressure, Pod startup and image
   cache differences can influence completion outcomes.
4. `--blocks 4` rotates all four policies through every execution position,
   but sequential trials are not independent or necessarily identically
   distributed. We do **not** report causal performance gains or inferential
   confidence intervals from this small pilot.
5. Kubernetes timestamps are retained as separately measured Pod diagnostics;
   the local monotonic end-to-end clock includes policy/controller overhead and
   polling delay. Do not combine unrelated clock domains.
6. Known placement startup budget defaults to 20 seconds, an illustrative
   round-number assumption derived from the first observed ~21-second startup;
   it is NOT a calibrated invariant or a future-state observation.

## Install

Copy the following **new files** into the repository (do not replace existing
Milestone 1–6A files):

- `research/kube_smoke_analysis.py`
- `research/kube_pilot.py`
- `tests/test_kube_pilot.py`
- `README_MILESTONE6B.md`

PowerShell commands, from `D:\Development\edgepilot-v2` with `.venv` active:

```powershell
python -m pytest -q
# Expected: 146 passed, given all Milestone 6A files are already installed.

# First analyze ten real observations. Read-only, no kubectl access.
python -m research.kube_smoke_analysis

# Dry run: reads nothing from cluster and creates NO Pod.
python -m research.kube_pilot --blocks 2

# Inspect cluster and namespace before opting in.
kubectl --context kind-edgepilot-v2 get nodes
kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation

# Pilot: creates 8 small Pods sequentially, maximum one at a time.
# Every Pod is created in edgepilot-v2-validation with a unique name and
# cleanup is attempted before proceeding. This is an explicit opt-in.
python -m research.kube_pilot --blocks 2 --execute

# For a more position-balanced follow-up, optional:
python -m research.kube_pilot --blocks 4 --seed 20261113 --execute --output research_outputs/kubernetes/policy_pilot_4blocks
```

## Artifacts

Smoke summary (read-only):

- `research_outputs/kubernetes/smoke_summary.json`
- `research_outputs/kubernetes/smoke_samples.csv`

Pilot:

- `research_outputs/kubernetes/policy_pilot/manifest.json` — settings, block
  ordering, and limitations.
- `research_outputs/kubernetes/policy_pilot/trials.csv` — per-trial actual
  results. Updated after each completed trial.
- `research_outputs/kubernetes/policy_pilot/summary.json` — descriptive
  per-policy rates and observed terminal medians.

`research_outputs/` should remain Git-ignored. Inspect individual Pods in
`trials.csv`; `terminal_phase='Succeeded'` and `on_time_observed=True` are
recorded separately. Deliberate waiting, scheduling/read overhead, Pod start
and execution are recorded with different metrics.

The live pilot is constrained to context `kind-edgepilot-v2` and namespaces
matching `edgepilot-v2-validation[-suffix]`; it refuses to run when a
nonterminal Pod already exists in the namespace. It creates at most 20 Pods,
sequentially, and aborts additional trials if cleanup fails. Dry-run requires
no `kubectl` and never mutates Kubernetes. A `KeyboardInterrupt` during a Pod
trial still attempts cleanup via `finally`, but always inspect the namespace
before rerunning after any interrupted experiment.

## Follow-up research

M6C should add *controlled, actually observed per-node telemetry impairments*,
randomized order, load perturbations, separate calibration/validation cohorts,
a named interference mechanism, and stronger experiment integrity checks.
The outcome can favor any policy; retain negative findings.

## Commit once verified

```powershell
git add research/kube_smoke_analysis.py research/kube_pilot.py tests/test_kube_pilot.py README_MILESTONE6B.md
git commit -m "feat(research): add real Kubernetes policy pilot and smoke metrics"
git status
```
