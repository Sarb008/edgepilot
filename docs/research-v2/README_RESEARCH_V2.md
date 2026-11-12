# EdgePilot v2 — research evaluation and reproducibility

**Project:** Deadline-aware scheduling with bounded deferral under stale telemetry in edge–cloud-style systems.  
**Research branch:** `research-v2` — preserve the original EdgePilot v1 materials.  
**Evidence freeze:** Phase 3 release gate, commit `6a8cfd5` (user-confirmed).  
**Research release:** v2.0.0-research, published 12 November 2026.

## The research question

Can deliberately waiting a bounded amount for fresher resource telemetry avoid an infeasible placement **often enough to justify its deadline cost**, compared with scheduling immediately using telemetry age?

The result is conditional. Waiting is useful only when fresh measurements are informative and arrive within the remaining completion budget. Fixed negative controls show no on-time benefit under unchanged or still-feasible capacity. Disruption studies also identify cases in which waiting hurts.

## Verified measurements at Phase 3 checkpoint

| Evidence | Immediate age-aware | Bounded deferral | What it means |
|---|---:|---:|---|
| Fixed simulator, unchanged capacity (108) | 108 | 108 | No on-time benefit from waiting |
| Fixed simulator, feasible capacity change (108) | 108 | 108 | No on-time benefit from waiting |
| Fixed simulator, infeasible capacity change (108) | 54 | 84 | Mechanism demonstrated in engineered cases |
| Local kind, worker 1 perturbed (6 blocks) | 0 | 6 | Worker-dependent observation |
| Local kind, worker 2 perturbed (6 blocks) | 6 | 6 | Outcomes tied |
| Local kind, combined (12 blocks) | 6 | 12 | Descriptive; not causal/generalizable |

Bounded deferral added **1.863 seconds of intentional waiting per real-cluster block on average**. The kind study contains **24 sequential Pod trials / 12 blocks / 3 fixed runs**. The Phase 3 checkpoint passed **525 automated tests** and a replay-and-compare reproducibility audit. The final research release adds five figure-integrity tests, bringing the total to **530 passing automated tests**.

## Design overview

```text
Controlled workloads + deadlines
             |
             v
Received telemetry (measured_at, received_at)
             |
      +------+---------------------+
      |                            |
      v                            v
Immediate age-aware          Bounded deferral
select using observed        wait only within
resource age                 slack / cap
      |                            |
      |                        fresh delivered
      |                         telemetry
      +-------------+--------------+
                    |
                    v
      Decision: worker / wait / refuse
                    |
        +-----------+-----------+
        |                       |
        v                       v
Deterministic simulator   Opt-in local kind
(full event traces)       default scheduler +
                           nodeSelector
        |                       |
        +-----------+-----------+
                    v
       Preserved CSV/JSON + offline audits
```

**Safety:** the live runner is explicitly opt-in with `--execute`, restricted to a disposable kind context and namespace; it checks cleanup between trials. The offline analyses never create Pods.

## Files to read first

- `docs/research-v2/PAPER_DRAFT.md` — complete manuscript-style report, results, citations, limitations.
- `docs/research-v2/REPRODUCIBILITY.md` — exact Windows commands and evidence paths.
- `docs/research-v2/DEMO_SCRIPT.md` — 2–3 minute demonstration and expected questions.
- `docs/research-v2/RELEASE_CHECKLIST.md` — historical release gates and final completion record.
- `RESEARCH_CLAIMS.md` — frozen research-claims ledger from Phase 3.

## Minimum verification

```powershell
cd D:\Development\edgepilot-v2
.\.venv\Scripts\Activate.ps1
python -m pytest -q
python -m research.final_figures --synthetic research_outputs/phase2_sensitivity_01 --real research_outputs/kubernetes/phase2_real_report_01 --output research_outputs/final_figures_01
```

If PowerShell blocks activation in a new session, use `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` or invoke `.\.venv\Scripts\python.exe` directly. Never delete or regenerate the frozen raw evidence directories to make plots.

## What this project does *not* claim

No deployed custom kube-scheduler, no real multi-host network-staleness measurements, no CPU-utilization measurement from the request-based admission tests, no independent edge hardware, no statistical significance/causal effect for sequential kind Pod runs, and no established peer-reviewed novelty. See `PAPER_DRAFT.md` §6 and `RESEARCH_CLAIMS.md`.

**Repository origin:** https://github.com/Sarb008/edgepilot. To publish v2, push the `research-v2` branch after reviewing the remote. Never assume local commits are already available on GitHub.
