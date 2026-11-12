# EdgePilot v2 — Windows reproducibility and evidence map

**Evidence status:** User-run Phase 3 gate passed on 12 November 2026. The instructions below replay already recorded evidence, without executing additional Kubernetes experiments. Keep results separate from simulation.

## System used for the reported kind pilot

Windows + PowerShell + Python 3.13.7 virtual environment, Docker Desktop / WSL 2, `kind` Kubernetes v1.37.0; one control-plane node and two ready Docker-backed worker nodes. All kind nodes shared a single physical machine. Recorded real-CPU-request measurements are not actual CPU-utilization measurements.

## A. Verify the codebase

```powershell
cd D:\Development\edgepilot-v2
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
python --version
python -m pytest -q
```

At the frozen Phase 3 checkpoint, **525 tests passed**. The final v2.0.0-research release includes five additional figure-integrity tests, for a total of **530 passing tests**.

## B. Verify original saved evidence (no Kubernetes access)

All following directories were already produced and audited on the user's machine; **do not overwrite them**:

- `research_outputs/phase2_sensitivity_01/` — 324 matched synthetic cases / 648 runs; CSVs, traces, figure, manifest.
- `research_outputs/kubernetes/wait_value_01/` — live Kubernetes run, seed 20261116.
- `research_outputs/kubernetes/wait_value_02/` — live Kubernetes run, seed 20261117.
- `research_outputs/kubernetes/wait_value_03/` — live Kubernetes run, seed 20261118.
- `research_outputs/kubernetes/phase2_real_report_01/` — 24 trials pooled into 12 worker-balanced blocks.
- `research_outputs/release_audit_phase3_01/` — Phase 3 replay and checksum gate.

To create **a new** independent offline replay:

```powershell
python -m research.release_audit `
  --synthetic research_outputs/phase2_sensitivity_01 `
  --real research_outputs/kubernetes/phase2_real_report_01 `
  --run research_outputs/kubernetes/wait_value_01 `
  --run research_outputs/kubernetes/wait_value_02 `
  --run research_outputs/kubernetes/wait_value_03 `
  --output research_outputs/release_audit_phase4_check_01
```

The gate should report the same 324 synthetic cases / 24 observed real Pod trials. Use a new directory each time; the audit intentionally rejects overwrites.

## C. Produce figures from authentic local CSVs

```powershell
python -m research.final_figures `
  --synthetic research_outputs/phase2_sensitivity_01 `
  --real research_outputs/kubernetes/phase2_real_report_01 `
  --output research_outputs/final_figures_01

start .\research_outputs\final_figures_01\synthetic_conditions.png
start .\research_outputs\final_figures_01\kubernetes_worker_strata.png
```

The generator refuses to operate if frozen source counts, worker strata or expected records disagree; it writes an `evidence_summary.json` with SHA-256 hashes of the CSV files actually read. It never contacts Kubernetes or generates new Pod measurements.

## D. Optional local cluster status (read-only; not needed for evidence replay)

```powershell
kubectl --context kind-edgepilot-v2 get nodes
kubectl --context kind-edgepilot-v2 get pods -n edgepilot-v2-validation
```

The completed validation trials had no remaining Pods in their namespace. Do not delete unknown resources if the namespace is not empty. No additional live experiment is required to reproduce the saved evidence of the 12 November 2026 research release.

## E. Keep the original evidence

`research_outputs/` is intentionally Git-ignored because raw run data can be bulky, environment-specific, and contain local paths. **Ignored is not the same as backed up.** Preserve the original source directories and audit manifest in a separate private archive. If publicly sharing evidence, inspect for local addresses, user identifiers, credentials, and environment metadata first. Upload only reviewed and appropriate artifacts.

For independent replication, document the specific Git commit (`git rev-parse HEAD`), package versions (`python -m pip freeze`), `kind version`, `kubectl version --client`, `docker info` (scrub local metadata), and the evidence manifest from the audit. A git commit and a passing test suite do not prove absence of all errors.

## F. Exact reproducibility boundaries

- **Guaranteed by completed gate on the user's machine:** deterministic synthetic replay and strict re-audit of the saved three-run Kubernetes evidence, subject to the gate's documented scope.
- **Not guaranteed:** identical Pod latencies on another PC; identical matplotlib PNG bytes across systems; independence of sequential trial outcomes; retrospective raw-file immutability prior to the first audit; production deployments or new hardware generalization.
