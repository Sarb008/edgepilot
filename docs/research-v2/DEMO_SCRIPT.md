# EdgePilot v2 — 3-minute research demonstration (Windows)

**Precondition:** original evidence directories exist; Phase 3 gate passed. Do not run new live Kubernetes experiments as part of the demonstration. Open a terminal in `D:\Development\edgepilot-v2` with `.venv` active.

### 0:00–0:25 — Problem and research question

> “Schedulers often see resource telemetry after the measured state has changed. A quick decision can therefore choose a worker that looked feasible but no longer has the required CPU-request capacity. EdgePilot v2 studies whether waiting briefly for a new measurement is worth the lost deadline budget.”

Show `docs/research-v2/README_RESEARCH_V2.md` and the architecture diagram in that file.

### 0:25–0:55 — Policy and architecture

> “We distinguish actual node state from measured and delivered telemetry; the simulator does not expose future information to policies. Our main comparison is immediate age-aware dispatch against bounded waiting. The wait policy must respect remaining deadline slack, including dispatch cost.”

Open `research/models.py`, `research/policies.py`, and `research/phase2_sensitivity.py`. Point at `measured_at`, `received_at`, `Workload.deadline`, and the bounded wait condition.

### 0:55–1:25 — Verified automated tests

Run:

```powershell
python -m pytest -q
```

Say: “At our Phase 3 gate, 525 tests passed; this release adds five figure-integrity tests. Tests cover models, event behavior, simulated failures, evidence validation and safe Kubernetes experiment operations.” Do not imply tests verify a novel algorithm or real-world generalization.

### 1:25–2:00 — Synthetic results, including negative controls

Open `research_outputs/final_figures_01/synthetic_conditions.png` or the frozen `research_outputs/phase2_sensitivity_01/sensitivity.png`.

> “The fixed 324-case synthetic matrix is not a population sample. For unchanged or still-feasible capacity, both policies completed 108 out of 108. For deliberately infeasible changes, immediate age-aware finished 54 of 108 and bounded deferral 84 of 108. Waiting costs time and is not universally preferable.”

### 2:00–2:35 — Real Kubernetes and confounding

Open `research_outputs/final_figures_01/kubernetes_worker_strata.png` and `research_outputs/kubernetes/phase2_real_report_01/worker_strata.csv`.

> “On a local three-node kind cluster, two Docker-backed workers shared one physical host. In 24 sequential Pod trials across 12 blocks, age-aware completed six jobs on time and bounded deferral 12. Bounded waiting averaged 1.86 additional seconds. The entire difference came from one worker perturbation stratum, so we call this a controlled observation, not causal proof.”

### 2:35–3:00 — Reproducibility and limitation

Show `research_outputs/release_audit_phase3_01/RELEASE_GATE.md` and `RESEARCH_CLAIMS.md`.

> “The release audit reproduced all 324 simulated cases and rechecked all 24 Kubernetes records. CPU requests are not actual CPU utilization, and sequential single-host tests do not generalize to physical edge-cloud hardware. The defensible contribution is an auditable framework and a conditional demonstration of the value-of-freshness tradeoff.”

**If asked why not a custom scheduler:** Kubernetes uses the default `kube-scheduler`, and the experimental harness chooses a node by `nodeSelector`. State this openly; a production plugin is future work.

**If asked why not p-values:** the simulator's fixed factorial is engineered and the real Kubernetes arms are small, sequential, temporally dependent, and not independent randomized observations.

**If asked whether age-awareness alone suffices:** the earlier matched-input audit showed a strong age-aware baseline, but the main common-old-input pilot exposed cases in which a *subsequent actually delivered* snapshot changed the feasible choice. This is the bounded-wait mechanism—not evidence that it always helps.
