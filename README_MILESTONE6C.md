# EdgePilot v2 — Milestone 6C: Controlled Real-Cluster Stale-Telemetry Pilot

## What changes since M6B

Your first real sequential policy pilot finished eight out of eight Pods on time.
Its policy means were 5.38 s (greedy), 5.48 s (age-penalized), 6.02 s
(uncertainty-aware), and 8.86 s (bounded deferral), with only two observations
per policy. This is a valuable functioning end-to-end probe, **not** evidence of
causal policy superiority. All workers shared one Kubernetes-snapshot age,
meaning the age-aware policies had little reason to differ from greedy.

Milestone 6C remedies a *measurement* issue, not a presumed performance issue.
It introduces genuine, timestamped, deliberately withheld per-worker telemetry
and a small, verified Kubernetes CPU-request perturbation on one worker.
Unlike synthetic simulations, the old and fresh observations are collected
from the local kind cluster. However, it is still an exploratory controlled
**CPU-request** experiment (not an actual CPU-utilization stress test).

### One trial, chronologically

1. Refuse to proceed unless the explicitly named validation namespace is empty.
2. Capture a real **old** global snapshot of CPU reservations and query timings.
3. Pick one usable kind worker. Create one disposable sleeping competitor Pod
   requesting 200 millicores, pinned via `nodeSelector` to that worker.
4. Confirm it is Running and actually bound to the expected worker.
5. Capture a real **fresh** snapshot and verify that the target worker's
   observed reserved CPU increased by the requested 0.2 cores. If the cluster
   changed in another way, abort instead of treating it as matched data.
6. Feed each policy a *mixed-age* observation: **old** data for the perturbed
   worker; **fresh** data for the other worker. These are real observation
   timestamps. The policy never receives the perturbed worker's fresh data at
   initial decision time. The harness retains the fresh snapshot ONLY for
   retrospective reference and evidence.
7. Immediate policies act on the mixed input. Bounded deferral waits within its
   hard maximum and may request a new **real** snapshot before deciding.
8. Submit one tiny BusyBox `sleep 2` probe Pod to the chosen worker. Measure
   local client-monotonic arrival-to-observed-completion, separate from the
   Kubernetes timestamp diagnostics. Delete the exact probe Pod by name.
9. Delete the exact competitor Pod by name, waiting for deletion confirmation.
   Save trial CSV and the two captured snapshots in a JSON trace. Fail closed
   and stop all subsequent trials if cleanup or integrity checks fail.

No shell commands are constructed from untrusted input. Cluster context and
namespace are fixed. There are no `kubectl delete --all`, force deletes,
`kubectl top` claims, workload CPU-burning loops, or operations on v1 resources.

### IMPORTANT scientific limitations

- **CPU requests, not actual CPU usage.** A sleeping competitor reserves
  CPU *in Kubernetes accounting* while consuming negligible CPU cycles. It
  should not be described as CPU-saturation/CPU-interference evidence.
- Both kind workers share one machine. Their advertised 12-core allocations
  cannot be interpreted as independent physical edge hosts.
- We deliberately withhold an observation, which is an **experimentally
  imposed information delay**, not a measured natural network/telemetry delay.
- The two global API snapshots are not atomic, and the reference snapshot is
  not a true instantaneous ground truth. We guard against one obvious
  inconsistency by verifying the target's CPU-request delta.
- The reference worker is greedy's choice using the fresh snapshot. It is
  **not** the oracle for actual earliest deadline completion or best outcome.
- Pod startup, image caching, poll frequency, controller time, and host
  contention can dominate these short jobs. Each policy runs sequentially;
  randomized/rotated order reduces but does not eliminate temporal confounding.
- `on_time_observed` measures successful terminal Pod **observation** by the
  deadline, including polling; it is not the exact completion timestamp.
- Competition setup and the old/fresh snapshot capture occur **before** the
  workload's arrival clock starts. Their costs are excluded from per-workload
  end-to-end timing; deliberate policy waiting after arrival IS included.
- If all policies still finish before the deadline, that is an informative
  result and does NOT justify concluding one policy is superior.
- The old sample is actually known to the experiment harness; it is
  deliberately *the only target sample shown to the scheduling policy*.
  This is a controlled stale-information treatment, not naturally occurring
  distributed telemetry.

## Installation

Extract the zip over `D:\Development\edgepilot-v2`, merging folders. The package
adds only these three files (no existing source files are replaced):

- `research/kube_stale_pilot.py`
- `tests/test_kube_stale_pilot.py`
- `README_MILESTONE6C.md`

Run from an activated Python 3.13 venv:

```powershell
cd D:\Development\edgepilot-v2
python -m pytest -q
# Expected after M6B: 178 passed.

# Dry run: NO Kubernetes reads or writes.
python -m research.kube_stale_pilot --blocks 1

# Verify your real-cluster context and truly empty dedicated namespace.
kubectl --context kind-edgepilot-v2 get nodes
kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation

# Explicit opt-in: four policy trials; up to EIGHT disposable Pods, sequentially.
python -m research.kube_stale_pilot --blocks 1 --execute

# If that succeeds, another distinct run can use 2 blocks (16 Pods maximum).
# ALWAYS give a new output path to avoid overwriting past trial evidence.
python -m research.kube_stale_pilot --blocks 2 --seed 20261113 --execute `
  --output research_outputs/kubernetes/stale_pilot_2blocks

# Inspect CSV; verify no experiment Pods remain.
Import-Csv .\research_outputs\kubernetes\stale_pilot\trials.csv | Format-Table
kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation
```

CLI safety: `--blocks` is limited to 1–3 (at most 24 Pods: one competitor and
one probe per trial); competitor requests are limited to 100–500m; hold time
45–90 seconds; probe CPU request 10–200m, and deadline 10–120 seconds.
The default is read-only dry-run. A non-empty validation namespace, an
unconfirmed competitor request increase, wrong worker binding, or cleanup
failure aborts the run. Files already present in the requested output folder
prevent overwriting results. A run aborted due to external interruption may
leave orphaned Pods if Kubernetes becomes unavailable; inspect the namespace
and remove only positively identified `epv2-stale-*` Pods if needed.

## Result files

`research_outputs/kubernetes/stale_pilot/`

- `manifest.json` — exact config, policy execution order, limits, warnings.
- `trials.csv` — real Pod outcomes, intended wait, withheld/fresh ages, target
  reservation delta, selected worker, fresh-reference worker, cleanup checks.
- `summary.json` — exploratory per-policy counts, rates and mean durations.
- `traces/block*_position*_*.json` — both time-stamped observations, the exact
  TelemetrySample fields passed into the policy, and the decision/result.

Do not commit generated outputs. Source files and tests are committed below.

```powershell
git add research/kube_stale_pilot.py tests/test_kube_stale_pilot.py README_MILESTONE6C.md
git commit -m "feat(research): add controlled Kubernetes stale-telemetry and request-contention pilot"
git status
```

## Next validation steps

1. Inspect the actual per-policy node choices and whether different outcomes
   are caused by intentional stale data, waiting, or unrelated Pod startup.
2. Repeat with randomized order and isolated **calibration vs evaluation** runs,
   report all failures and cleanup outcomes.
3. Extend into real node-capacity contention, failures, and telemetry update
   transport delays only after separately validating the safety and measurement
   assumptions. Do not invent or backdate real source timestamps.
4. Preserve the v1/v2 distinction, experimental configs, and negative findings
   in the future research paper.
