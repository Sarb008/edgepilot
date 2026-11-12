# EdgePilot v2 — Milestone 6D: Real Kubernetes request-feasibility boundary

## Why this experiment exists

Milestone 6C passed 178/178 tests and ran four Kubernetes trials. Under the
controlled, deliberately withheld telemetry input, greedy selected the stale
worker while the other policies chose the fresh-reference worker. All four
probe Pods nevertheless finished on time, because the probe reserved only **50
millicores** and each worker had around **11.9 cores unreserved**. A change in
which worker is selected did not imply a change in Kubernetes admission or
completion outcomes.

Milestone 6D introduces an explicit **CPU-request feasibility boundary**. It
keeps the M6C timestamped, mixed-age observation protocol and all existing
source files intact. It installs only a new experiment module, its isolated
fake-cluster tests, and this README. **No real cluster was accessed during the
local 214-test verification.** The first live run is yours.

### Experimental sequence (each trial)

1. Verify the context is EXACTLY `kind-edgepilot-v2`, verify that the dedicated
   namespace `edgepilot-v2-validation` is empty, and capture an OLD real
   two-worker snapshot.
2. From the OLD target-worker available CPU, *before any competitor is placed*,
   set the probe request to `floor(old available millicores) - 100m`. The old
   peer must have at least 50m spare headroom for this request. If no safe
   controlled boundary exists, **abort**, not fudge the observation.
3. Place one `sleep 60` competitor Pod with a **200m CPU request** on the target
   worker; confirm the Pod is Running on that exact worker.
4. Capture a FRESH real snapshot and verify the target's requested CPU increased
   by 200m. Require a true boundary: the fixed probe request must fit the old
   target, **not** the fresh target, and **still** fit the fresh peer.
5. Construct the mixed-age policy input: old target sample, fresh peer sample.
   The experiment harness keeps the fresh target only for retrospective
   reference; **policies do not receive that value**. Samples use their actual
   monotonically measured/received timestamps; no timestamp is fabricated.
6. If necessary, let the old sample age in *real elapsed time* to at least 1
   second before the workload arrives. Recheck the competitor is still Running.
   This preparation is **excluded** from per-job latency and recorded.
7. Invoke one of the four actual research policies. Bounded deferral may refresh
   telemetry after an actual wait. A policy may choose a worker or safely
   decline an extremely tight request.
8. If a node was chosen, create a single sleeping probe Pod requesting the
   OLD-derived CPU amount; do not burn CPU. Record Kubernetes Pending /
   Unschedulable conditions, final phase, on-time status, choice, deferral,
   and observed elapsed time. Give each trial a **20-second** job deadline by
   default (including any policy waiting and Pod startup).
9. Delete the exact probe Pod (if creation was attempted), then the exact
   competitor Pod. Abort subsequent trials unless deletion was accepted and
   the validation namespace is observed empty.

Each Pod's CPU **limit** is set equal to its CPU **request** for validation.
The experiment may request approximately 11.8 out of an advertised 12 CPU
cores on one kind worker, depending on real observed reservations. This is a
large *reservation*, not CPU saturation: the Pod simply executes `sleep 2`.
Because the worker nodes are Docker containers sharing the same computer,
this does not represent independent edge hardware.

## Installation and verification

Extract the package over `D:\Development\edgepilot-v2` **without replacing
any existing directory**. It contains:

- `research/kube_feasibility_pilot.py`
- `tests/test_kube_feasibility_pilot.py`
- `README_MILESTONE6D.md`

From the project's activated virtual environment:

```powershell
cd D:\Development\edgepilot-v2
python -m pytest -q
# Expected with M6C installed: 214 passed

# No cluster read/write:
python -m research.kube_feasibility_pilot --blocks 1
```

### Real-cluster opt-in (local disposable kind only)

Ensure Docker Desktop is running. Confirm the namespace is empty:

```powershell
kubectl --context kind-edgepilot-v2 get nodes
kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation
```

Then explicitly opt in:

```powershell
python -m research.kube_feasibility_pilot --blocks 1 --execute
```

This performs **four sequential policy trials, up to eight disposable Pods**.
The default output folder is
`research_outputs/kubernetes/feasibility_pilot/`. The experiment refuses to
overwrite an existing nonempty output folder.

Only if the first study succeeds, optionally rotate the perturbed worker on a
second block in a separate output folder:

```powershell
python -m research.kube_feasibility_pilot --blocks 2 --seed 20261113 --execute `
  --output research_outputs/kubernetes/feasibility_pilot_2blocks
```

**Important:** Do not run these two studies concurrently. All Pods are
sequential, and no existing Pods in the designated namespace may be present.
If a study crashes or is interrupted, inspect the namespace, distinguish the
`epv2-feas-*` experiment Pods from all others, and do not issue broad/force
cluster deletes. Check the exact Pod names before manual cleanup.

### Results / evidence

- `manifest.json`: experiment configuration, randomized/rotated order,
  limitations, and claimed measurement class.
- `trials.csv`: selected and reference worker, CPU request, old/new
  feasibility (CPU-request **estimate**, not guaranteed admission), wait,
  deadline status, unschedulable reason, and cleanup flags.
- `summary.json`: *descriptive*, per-policy on-time, fresh-snapshot
  infeasible selection, disagreement and observed timeout counts.
- `traces/*.json`: true old and fresh Kubernetes API snapshots; precise
  timestamped mixed samples, and post-treatment decisions/results.

A Pod remaining Pending because of insufficient requested CPU is a valid
**negative outcome**, not a runtime crash. The result distinguishes an
Unschedulable status from a timed-out Pod observation. In the same way, an
uncertainty-aware policy declining the very tight request is a meaningful
conservative decision, **not automatically a bug**. A fresh-reference worker
is the greedy policy's pick on the fresh inventory, not an oracle for lowest
runtime or an experimental proof of what 'should' happen.

## Scientific caveats

This study intentionally creates a treatment in which a large CPU request
fits one worker before a 200m reservation and no longer fits afterward. That
is a **designed feasibility-boundary stress test**. It is not representative
prevalence of stale placements in normal production clusters. It does not
measure real CPU consumption, container restart recovery, natural telemetry
transport delays, or the causal benefit of bounded deferral over all workloads.

Trials are sequential on one host; image caching, API polling, host activity,
and startup variability remain possible confounders. Small sample sizes and
constructed conditions do not justify superiority/causality claims. An
on-time percentage within this experiment must not be mixed with previous
synthetic studies or the 50m M6C pilot as though the workloads were matched.

## Commit after success

```powershell
git add research/kube_feasibility_pilot.py tests/test_kube_feasibility_pilot.py README_MILESTONE6D.md
git commit -m "feat(research): add controlled Kubernetes CPU-request feasibility boundary pilot"
git status
```

Do not commit `research_outputs/` or virtual-environment files.

## Research direction after this

Audit real scheduler Unschedulable reasons and verify that policy decisions
predict actual admission. Then compare a *predeclared mix* of normal and
boundary workloads, analyze all four policies including refusals, and add
separately measured telemetry publication/delivery delay. Only after that
consider multi-host or cloud edge comparisons, calibrated simulator tests,
and formal paper claims.
