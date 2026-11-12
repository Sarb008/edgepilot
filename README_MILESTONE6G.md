# EdgePilot v2 — Milestone 6G: Matched-input initial-decision audit

## Status and motivation

The Milestone 6F Kubernetes pilot (four balanced blocks) returned greedy on-time 2/4, age-penalized 4/4, bounded-deferral 4/4, and uncertainty-aware 0/4 (four refusals). Both greedy timeouts occurred when the lexicographically first worker was perturbed; the other worker's perturbation did not cause greedy timeouts. This raises a concrete possibility that deterministic name-based tie-breaking, rather than simply a general stale-telemetry effect, materially shapes the comparison.

M6G adds a **read-only** post-hoc diagnostic. It does **not** launch Pods and it does **not** change any existing scheduling policy. Its objective is to make decisions on genuinely *identical recorded telemetry* comparable without confounding them with sequential trial timing.

## New files

- `research/kube_decision_audit.py`: validates complete evidence using the M6E/M6F auditing machinery; reconstructs workload arrival from recorded per-worker telemetry ages; replays each of the four policies' **initial** action against the exact same stored mixed-age input; checks the logged immediate-policy choice; tests a *reverse-lexicographic top-capacity tie-break sensitivity* for greedy; exports CSV and JSON.
- `tests/test_kube_decision_audit.py`: independent regression tests, malformed timestamp/value inputs, offline execution guarantee, evidence/output safety, reproducible balanced fake-cluster run.
- `README_MILESTONE6G.md`: this guide.

## Installation and execution — Windows PowerShell

Extract this archive into `D:\Development\edgepilot-v2` and merge folders. Only the three new files are installed.

```powershell
cd D:\Development\edgepilot-v2
python -m pytest -q
```

**Expected:** `337 passed` when all previous milestones are present.

Run the **offline** audit of the two existing balanced runs:

```powershell
python -m research.kube_decision_audit `
    --run research_outputs/kubernetes/feasibility_repeat_01 `
    --run research_outputs/kubernetes/balanced_replication_01/run_01 `
    --output research_outputs/kubernetes/decision_audit_01
```

No Docker or Kubernetes cluster is required for this command. It refuses incomplete/unbalanced/incompatible inputs, overlapping source/output paths, or output overwrite.

Review the results:

```powershell
Get-Content .\research_outputs\kubernetes\decision_audit_01\summary.json
Import-Csv .\research_outputs\kubernetes\decision_audit_01\replayed_decisions.csv |
  Where-Object { $_.replayed_policy -eq 'greedy' } |
  Format-Table run_index,block,observed_policy,perturbed_worker,initial_selected_worker,greedy_exact_top_tie_count,greedy_reverse_lexicographic -AutoSize
```

The report has **16 historical input traces and 64 initial decisions** if all four balanced blocks are present. That is **not 64 independent policy trials**.

After the tests and audit succeed:

```powershell
git add research/kube_decision_audit.py tests/test_kube_decision_audit.py README_MILESTONE6G.md
git commit -m "feat(research): audit matched-input decisions and greedy tie sensitivity"
git status
```

## Interpretation: what this CAN and CANNOT show

### Supported diagnostic statements

- Which initial action (select, wait, refuse) each policy would make for the **same recorded telemetry** within each historical trial.
- Which initial worker choices would have satisfied the **recorded fresh Kubernetes CPU-request accounting**. This is an evaluator-only retrospective diagnostic; the fresh capacity data never reaches the replay policy.
- Whether greedy had an **exact tie for maximum reported unreserved CPU**. The existing greedy policy resolves exact ties by lexical node name. The audit computes the alternate worker chosen by reversing only this lexical priority, *without deploying it*.
- Whether a logged immediate-policy selection agrees with the source-algorithm replay; inconsistent evidence causes rejection.
- Whether bounded deferral's initial decision was to **wait**, but **not** which later node it would select. We cannot invent its future telemetry refreshes.

### Unsupported conclusions

- A counterfactual Pod completion under the alternate choice. No counterfactual Pods are created; fresh-reference feasibility is not a completion prediction.
- That bounded waiting is more effective than age-aware immediate scheduling. This requires real controlled experiments where later telemetry updates and deadlines differ.
- Generalizable causal effects, field latency, physical CPU saturation, or a statistical significance claim from the four blocks.
- Treating the 64 policy-by-trace replayed actions as 64 independent Kubernetes observations. There are only 16 distinct input traces and 16 real Pod-directed policy trials in this audit input.

## Next experimental milestone

Use the decision audit to pre-register a **node-ID tie-break counterbalance and freshness-timing contrast**. To isolate waiting, construct cases in which only a *later* delivered measurement resolves the uncertainty, while explicitly matching deadline budget and first-action data. Preserve sequential-run order randomization and worker exposure. Record the entire actual refresh timeline rather than trying to reconstruct it from a final outcome. Do not retrofit a favorable hypothesis from previous runs.
