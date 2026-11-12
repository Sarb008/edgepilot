# EdgePilot v2 — Research Release Completion Record

## Release Information

- Release: `v2.0.0-research`
- Publication date: 12 November 2026
- Original release target: 25 November 2026
- Frozen release commit: `1f90c8874fbab7e2914b1d43f3a01c4d2427b385`
- Release URL: https://github.com/Sarb008/edgepilot/releases/tag/v2.0.0-research

The research implementation was completed and released ahead of the original target date.

The original planning checklist is preserved in [RELEASE_PLAN_ARCHIVE.md](RELEASE_PLAN_ARCHIVE.md).

## 1. Implementation and Testing

- [x] Validated workload, telemetry, and resource-state models.
- [x] Deterministic event-driven scheduling simulation.
- [x] Greedy, age-penalized, bounded-deferral, and uncertainty-aware policies.
- [x] Telemetry impairment and reliability experiments.
- [x] Controlled Kubernetes validation infrastructure.
- [x] Worker-selection and tie-breaking analysis.
- [x] Phase 3 checkpoint: 525 passing automated tests.
- [x] Final release: 530 passing automated tests, including five additional figure-integrity tests.

## 2. Synthetic Evaluation

- [x] Fixed experimental sensitivity matrix.
- [x] 324 matched synthetic cases.
- [x] 648 policy runs.
- [x] Unchanged-capacity negative controls.
- [x] Changed-but-feasible negative controls.
- [x] Changed-and-infeasible capacity scenarios.
- [x] Deterministic replay and comparison against preserved results.
- [x] Final synthetic results figure.

Final on-time completion counts:

| Condition | Immediate age-aware | Bounded deferral |
|---|---:|---:|
| Unchanged | 108/108 | 108/108 |
| Changed, remains feasible | 108/108 | 108/108 |
| Changed, becomes infeasible | 54/108 | 84/108 |
| Combined | 270/324 | 300/324 |

These results describe the configured deterministic cases, not a random sample of production workloads.

## 3. Kubernetes Evaluation

- [x] Controlled local kind environment.
- [x] 24 sequential Pod trials.
- [x] 12 worker-balanced comparison blocks.
- [x] Three fixed experimental runs.
- [x] Preserved trial-level evidence.
- [x] Worker-stratified analysis.
- [x] CPU-request feasibility evaluation.
- [x] Final Kubernetes results figure.

Final observed outcomes:

| Perturbed worker | Immediate age-aware | Bounded deferral |
|---|---:|---:|
| First worker | 0/6 | 6/6 |
| Second worker | 6/6 | 6/6 |
| Combined | 6/12 | 12/12 |

Mean intentional waiting introduced by bounded deferral: approximately 1.863 seconds.

All observed differences occurred in the lexically first worker stratum. Worker identity and deterministic tie-breaking remain important interpretation limitations.

## 4. Reproducibility and Evidence

- [x] Offline synthetic replay-and-compare audit.
- [x] Verification of saved Kubernetes trial records.
- [x] Independent aggregation of saved trial evidence.
- [x] Evidence checksum inventory.
- [x] Local research evidence archive created.
- [x] Local archive SHA-256 recorded.
- [ ] Public evidence package privacy review.
- [ ] Public evidence package publication.
- [ ] Off-device evidence backup independently confirmed.

Original local evidence archive:

`EdgePilot-v2-evidence-2026-11-12.zip`

Recorded SHA-256:

`F537BFB8AB3CE0B92768C921BAE4FD320F191FE2031305DB35958D95605B9E0E`

The offline audit verifies the consistency of the preserved evidence and deterministic synthetic replay. It does not constitute an independent rerun of the live Kubernetes experiments or establish scientific generalizability.

## 5. Manuscript and Documentation

- [x] Research manuscript draft.
- [x] Research manuscript PDF and editable source.
- [x] Final research figures.
- [x] Research claims ledger.
- [x] Reproducibility instructions.
- [x] Technical demonstration guide.
- [x] Research summary for application preparation.
- [x] Frozen GitHub research release.
- [ ] Final manuscript author metadata review.
- [ ] Final documentation polish and link verification.

The manuscript is a publicly available research draft. It has not been submitted for peer review or accepted for publication.

## 6. Scope Boundaries

The completed release does not claim:

- A novel scheduling algorithm.
- General causal superiority of bounded deferral.
- Statistically established population-level improvement.
- Production-scale Kubernetes performance.
- Actual CPU-saturation performance improvement.
- Validation across physically independent edge devices.
- Replacement of Kubernetes' native scheduler.

These limitations are intentional and documented.

## 7. Future Research

Potential future investigations include:

- Independent physical worker environments.
- Randomized or counterbalanced worker-selection policies.
- Additional workload distributions.
- Actual application execution and resource contention.
- Larger-scale experiments.
- Comparison against stronger scheduling baselines.
- Further literature review and independent expert evaluation.

These extensions are not required for the completed v2.0.0-research release.

## Final Status

The EdgePilot v2 research implementation and its planned exploratory evaluation are complete.

Remaining activities concern documentation, evidence distribution, backup, and research communication rather than additional experimental development.

The original research release tag and experimental evidence must remain unchanged.