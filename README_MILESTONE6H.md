# EdgePilot v2 — Milestone 6H: tie-break-controlled matched-input ablation

## Why this milestone exists

Milestone 6G replayed 16 *historically recorded* Kubernetes mixed-age telemetry inputs. All 16 produced an exact tie in the greedy policy's highest reported free CPU capacity. A lexical worker-name tie-break determined greedy's placement, and reversing it changed retrospective fresh-reference feasibility in all 16 inputs. This means earlier live outcomes cannot be attributed solely to telemetry freshness.

M6H quantifies the tie-breaking confound using a new **offline, analytical ablation**. It does not add real Kubernetes trials or claim new on-time results.

## Files to add

- `research/kube_tie_ablation.py`: strictly offline tie ablation, worker-stratified analysis, CSV/JSON export.
- `tests/test_kube_tie_ablation.py`: tests for matched-input grouping, exact uniform-tie expectation, different worker strata, corrupt records, no-cluster access, export isolation, and evidence tampering.
- `README_MILESTONE6H.md`: this documentation.

The package **does not overwrite any prior module**.

## Install

Extract the archive into `D:\Development\edgepilot-v2`, merging `research` and `tests` folders. With the existing venv active:

```powershell
cd D:\Development\edgepilot-v2
python -m pytest -q
```

Expected in the released package: **368 tests passing** (337 prior + 31 new).

## Run the no-Pod experiment

```powershell
python -m research.kube_tie_ablation `
    --run research_outputs/kubernetes/feasibility_repeat_01 `
    --run research_outputs/kubernetes/balanced_replication_01/run_01 `
    --output research_outputs/kubernetes/tie_ablation_01
```

This makes **zero** Kubernetes calls; it reads only previously recorded, M6G-validated evidence. It refuses to overwrite an existing output directory. It writes:

- `summary.json`: limitations, original evidence manifests and checksum references, aggregate results.
- `input_ablation.csv`: one row per original recorded input, with alternatives scored against a later fresh reference.
- `worker_strata.csv`: separate results for each perturbed worker.

Preview:

```powershell
Get-Content .\research_outputs\kubernetes\tie_ablation_01\summary.json
Import-Csv .\research_outputs\kubernetes\tie_ablation_01\worker_strata.csv | Format-Table -AutoSize
```

## Four comparison arms

1. **Lexical greedy**: the existing greedy policy, with lexical choice on an exact maximum-capacity tie.
2. **Reverse lexical greedy**: the alternate worker on that exact tie; **hypothetical** selection only.
3. **Uniform tie greedy (analytical)**: exactly 50% probability per tied worker. Rather than running a pseudo-random sample, report the **exact expected number of retrospective fresh-infeasible selections**. This has no variance from a simulated coin toss and is not a real deployment.
4. **Immediate age-penalized**: replay the actual scheduling code on that same recorded mixed-age snapshot. Scores initial decisions, **not** the later execution outcome.

The bounded-deferral policy initially **waited** on these inputs, so its eventual selection is not reconstructed. The uncertainty-aware policy initially **rejected** them. Neither action is mislabeled a CPU-infeasible selection.

## Expected interpretation of the previously reported 16-input audit

Provided the underlying evidence is exactly the 16 inputs reported in M6G, we expect 16 exact two-way greedy ties, with one fresh-reference feasible worker per input. A hypothetical uniform tie is therefore **expected to pick the fresh-infeasible worker on 8 of 16 recorded inputs**. Balanced exposure can give lexical and reverse greedy the same aggregate rate, even though each fails for opposite perturbed-worker strata. The replayed age-penalized policy selected a fresh-feasible worker on all 16 inputs in M6G.

These are **selection-feasibility scores**, not Kubernetes failure rates, on-time rates, independent samples, statistical confidence intervals, or evidence that waiting helped. No real Pod is created by this module.

### Important threat to validity

The recording still comes from a sequence of live kind trials on two containers sharing one computer. Every Pod was scheduled by default kube-scheduler using fixed node selection, not an external custom scheduler. CPU-request admission is distinct from actual CPU use. The experiment setup deliberately makes stale capacity equal for the two workers; these results do not generalize to arbitrary heterogeneous clusters.

## Recommended next experimental step

If M6H confirms the expected split, build a **prospective live tie-controlled randomized experiment**, rotating greedy tie preferences independently of the perturbed worker and controlling the deadline and workload. Separately test the *incremental* benefit of waiting against an immediate age-aware policy; do not infer it from static initial-decision replays. Pre-register the policy ordering, seeds, rejection treatment, and primary endpoint before running more Pods.

## Commit after verifying results

```powershell
git add research/kube_tie_ablation.py tests/test_kube_tie_ablation.py README_MILESTONE6H.md
git commit -m "feat(research): quantify greedy tie-break bias on matched Kubernetes traces"
git status
```
