# EdgePilot v2 — Phase 3: engineering/research audit and release gate

**Deadline: 22 November 2026 code freeze. Final project delivery: 25 November 2026.**

This batch adds an independent **read-only, offline reproduction gate**. No new Kubernetes Pod is created. It uses the **already collected** three Phase 2 real-cluster runs and the **already collected** full fixed synthetic study. It does **not** expand the experimental design or overwrite evidence.

## 1. Install (Windows PowerShell, activated `.venv`)

Extract the ZIP into `D:\Development\edgepilot-v2` while preserving `research/` and `tests/` folder structure. These are new files; existing research modules remain unchanged.

```powershell
cd D:\Development\edgepilot-v2
python -m pytest -q
```

Expected full test result: **525 passed** (512 prior tests + 13 regression tests). Do not claim the run passed on your PC until you execute it.

## 2. Run the strict reproduction audit

Before doing this, verify all the source data paths below exist. In particular, keep **all** `traces/` folders under `wait_value_01`, `_02`, `_03`.

```powershell
python -m research.release_audit `
  --synthetic research_outputs/phase2_sensitivity_01 `
  --real research_outputs/kubernetes/phase2_real_report_01 `
  --run research_outputs/kubernetes/wait_value_01 `
  --run research_outputs/kubernetes/wait_value_02 `
  --run research_outputs/kubernetes/wait_value_03 `
  --output research_outputs/release_audit_phase3_01
```

The gate will:

1. Verify the **324 frozen synthetic case definitions** and SHA256 checksums of original CSV, trace, and plot files.
2. Rerun **648 deterministic simulations** in a disposable temporary directory, comparing the original CSV/decision traces byte-for-byte. Matplotlib raster hashes may vary across platforms, so only original plot integrity and PNG validity—not cross-platform pixel equality—are verified.
3. Independently verify the **three frozen Kubernetes runs** (seeds `20261116`, `20261117`, `20261118`), including their initial policy inputs, recording chronology, request-feasibility boundary, outcome classification, and cleanup evidence via the existing strict auditor.
4. Independently regenerate the pooled **24 trial / 12 block** report and compare its CSVs byte-for-byte and summary JSON semantically.
5. Record first-time SHA256 baselines for **every source trace** and capture Git/Python environment details. The first-time hash record cannot prove that historical data were never edited; preserve the output as a provenance artifact.
6. Write two new files: `research_outputs/release_audit_phase3_01/audit.json` and `RELEASE_GATE.md`.

**Safety:** no `kubectl` calls; no Docker calls; no Pod creation, deletion, or retries. Original evidence is never rewritten. Output directory must not already exist. On any mismatch, the program fails and does not create a misleading success report.

Inspect:

```powershell
Get-Content .\research_outputs\release_audit_phase3_01\RELEASE_GATE.md
Get-Content .\research_outputs\release_audit_phase3_01\audit.json
```

The gate uses absolute paths to cross-reference source runs in the previous Phase 2 report; the simplest reproduction is on the Windows machine with those evidence folders kept in place. Cross-machine packaging will need path normalization and provenance documentation in the Phase 4 release guide.

## 3. Engineering/research findings and boundaries

**What has been validated by existing automated tests:** scheduling-input snapshots, dispatch resource accounting in the synthetic engine, telemetry delivery/event ordering, deterministic fixtures, input consistency, matched protocol/policy variants, single-Pod validation cleanup, selective cluster execution with explicit opt-in, and offline evidence audibility.

**What the Phase 3 gate adds:** integrity of the **actual saved raw Phase 2 records**, fidelity of deterministic synthetic replay, consistency of the pooled 24 Kubernetes records, and a reproducible first-time hash manifest for all source traces.

**What remains outside this audit:** independent implementation review, hardware-separated edge hosts, randomization that eliminates all time confounding, production-grade scheduler/controller operation, actual CPU saturation measurements, and externally validated novelty. Do not represent them as completed.

### Fixed observed outcomes (do not re-label these)

| Layer | Immediate age-penalized | Bounded deferral | Interpretation |
|---|---:|---:|---|
| Synthetic unchanged capacity (108 matched cases) | 108/108 | 108/108 | Waiting adds time, not on-time benefit |
| Synthetic feasible capacity change (108) | 108/108 | 108/108 | Waiting adds time, not on-time benefit |
| Synthetic infeasible capacity change (108) | 54/108 | 84/108 | Synthetic illustrative mechanism; not independent sampling |
| Sequential kind Pods, worker perturbed (6 blocks) | 0/6 | 6/6 | Worker-dependent placement mechanism; cannot generalize |
| Sequential kind Pods, worker2 perturbed (6 blocks) | 6/6 | 6/6 | Both feasible in this stratum |
| All sequential kind blocks (12) | 6/12 | 12/12 | Descriptive +50 percentage points; **NOT a causal treatment effect** |

Bounded deferral waited an average of about **1.86 seconds** on real-kind trials. An observed timeout is **not proof of permanent CPU admission failure**; Pod `Insufficient cpu` evidence and later fresh-reference feasibility are separate observations. Kubernetes CPU **requests** are reservations, not measured CPU utilization.

**Caution about randomization:** the three listed seeds still produced the same displayed policy order within the four blocks. Do not describe them as independent assignment sequences. Worker perturbation was balanced but outcome sensitivity remained tied to worker names and deterministic selection.

## 4. Close Phase 3, then Phase 4

Once the 525 tests and reproduction gate pass:

```powershell
git add research/release_audit.py tests/test_release_audit.py README_PHASE3_NOV22.md RESEARCH_CLAIMS.md
git commit -m "test(research): add offline evidence reproduction and release gate"
git status
```

Optional, but recommended for a remote backup of the `research-v2` branch:

```powershell
git push -u origin research-v2
```

The branch must be pushed only to the repository you control; inspect `git remote -v` first if unsure.

**Phase 3 exit criterion:** full tests green, release gate green, evidence integrity recorded, reproducibility limitations explicit, no outstanding *known* critical defect. This does not prove an absence of all defects.

**Next and final phase:** 23–25 November: paper-style report, architecture and results figures, professional repository README, reproduction instructions, a 2–3 minute demonstration script, final code freeze/tag. **No additional exploratory Kubernetes trial cycles.**
