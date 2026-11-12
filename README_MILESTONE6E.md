# EdgePilot v2 — Milestone 6E: Evidence audit and replication protocol

## What we learned from Milestone 6D

The first real kind feasibility-boundary experiment (one sequential block) reported:

| Policy | Observed outcome | Fresh-reference CPU-request feasibility | On-time |
|---|---|---|---|
| Greedy | `TimedOutObservation` | selected worker infeasible | False |
| Age-penalized | `Succeeded` | selected worker feasible | True |
| Bounded deferral | `Succeeded` | selected worker feasible | True |
| Uncertainty-aware | `NoEligibleNode` | did not place a Pod | False |

This is an **interesting mechanism demonstration**, **not evidence of a reproducible performance advantage**. A timeout is not automatically a Kubernetes `Insufficient cpu` scheduling diagnosis: that requires a recorded `PodScheduled=False, reason=Unschedulable` message or a corresponding event.

## Scope of this package

Adds only three new files:

- `research/kube_feasibility_analysis.py` — **OFFLINE ONLY** evidence audit; never runs `kubectl` or changes the cluster.
- `tests/test_kube_feasibility_analysis.py` — additional regression tests.
- `README_MILESTONE6E.md` — these instructions.

No existing files are overwritten.

The analyzer verifies each completed M6D run's manifest, policy order, trace and CSV consistency, timestamped old/fresh snapshots, freshness boundary, reported worker feasibility, and Pod cleanup records. It saves current SHA-256 hashes for provenance, but these hashes **do not establish that the original experiment was tamper-proof**. A missing/partial/contradictory run is rejected, not silently pooled.

It reports policy-specific completion, refusal, observed timeout, recorded `Insufficient cpu` messages, disagreements with the fresh-reference decision, and descriptive block-level differences. It does not calculate a statistical significance test or confidence interval from a few sequential, non-independent trials.

## 1. Verify the package

Extract contents into `D:\Development\edgepilot-v2`, preserving `research` and `tests` folder structure. With `.venv` active:

```powershell
python -m pytest -q
```

Expected **258 passed**, after adding 44 analysis tests to the prior 214.

## 2. Audit the existing run (no Kubernetes reads/writes)

```powershell
python -m research.kube_feasibility_analysis `
  --run research_outputs/kubernetes/feasibility_pilot `
  --output research_outputs/kubernetes/feasibility_audit_initial
```

Review:

- `research_outputs/kubernetes/feasibility_audit_initial/audit.json`
- `research_outputs/kubernetes/feasibility_audit_initial/policy_summary.csv`

To check whether greedy's timeout included recorded CPU admission evidence:

```powershell
Import-Csv .\research_outputs\kubernetes\feasibility_pilot\trials.csv |
  Select-Object policy,terminal_phase,unschedulable_reason,selected_worker,selected_feasible_in_fresh_snapshot |
  Format-Table -Wrap -AutoSize
```

If the recorded `unschedulable_reason` is empty, **do not** claim the cluster conclusively diagnosed `Insufficient cpu` in that trial.

## 3. Repeat the same bounded real-cluster protocol

Important: every M6D invocation is restricted to **1–2 blocks**, and each block can create up to **8 disposable Pods** (one reservation and one probe per policy). Runs are sequential. Use `--blocks 2` so the intentionally perturbed worker alternates between both eligible workers. Each Pod executes `sleep`, but can reserve almost all CPU *requests* of a worker; run only on the disposable local kind cluster and verify an empty dedicated namespace first.

Before opting in:

```powershell
kubectl --context kind-edgepilot-v2 get nodes
kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation
python -m research.kube_feasibility_pilot --blocks 2 `
  --seed 20261113 --output research_outputs/kubernetes/feasibility_repeat_01
```

Only when nodes are `Ready`, the namespace has zero Pods, and the preview is correct:

```powershell
python -m research.kube_feasibility_pilot --blocks 2 `
  --seed 20261113 --output research_outputs/kubernetes/feasibility_repeat_01 --execute
```

The runner aborts after a trial with an execution or cleanup error; investigate and do **not** delete unrelated workloads. Do not reuse an existing output directory; experimental evidence is never overwritten by design.

## 4. Audit repetitions separately or together

After successful repetition:

```powershell
python -m research.kube_feasibility_analysis `
  --run research_outputs/kubernetes/feasibility_pilot `
  --run research_outputs/kubernetes/feasibility_repeat_01 `
  --output research_outputs/kubernetes/feasibility_audit_combined_01
```

The combined output describes **three blocks / twelve trials** (including the original one-block pilot). These are too few, and the trials too sequential, for generalizable causal conclusions. The first block also makes the perturbation-target distribution uneven; report it separately for serious comparative analysis.

For further evidence, perform an additional unique two-block run with a different seed and a new output path, e.g. `feasibility_repeat_02`, then audit those two balanced runs together. Keep workload parameters and cluster conditions fixed, record cold/warm image state, and don't claim policy superiority without adequate replication, stronger controls and an appropriate design.

## 5. Commit

When the full suite and initial audit pass:

```powershell
git add research/kube_feasibility_analysis.py tests/test_kube_feasibility_analysis.py README_MILESTONE6E.md
git commit -m "feat(research): audit real-cluster feasibility evidence and policy outcomes"
git status
```

`research_outputs/` remains ignored and should be preserved locally, backed up separately if necessary, rather than committing unreviewed generated files.

## Limits that must appear in a research report

1. The default Kubernetes scheduler performs admission after **nodeSelector**; this is a policy-directed experiment, not a custom scheduler deployed in kube-scheduler.
2. CPU-request reservations are not CPU load or utilization.
3. The 'fresh' reference is a retrospective, non-atomic API snapshot — not perfect ground truth or a policy-accessible oracle.
4. Polling causes interval-censored completion observation; an observation timeout is not always a failed container.
5. `NoEligibleNode` is a controller-level refusal and should be reported distinctly from Pod failure or scheduling timeout.
6. Blocks use sequential workloads on the same physical host, not simultaneous, independent edge nodes.
7. Bootstrap confidence intervals from a few non-independent sequential blocks would overstate certainty. Do not manufacture them.
