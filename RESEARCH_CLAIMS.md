# EdgePilot v2 — Frozen claims and interpretation ledger

**Status:** working research evidence; internal engineering evaluation, not peer reviewed. Prepared for the 25 November 2026 final project release.

## Research question

Under what controlled workload-deadline, telemetry-freshness and changing CPU-request-capacity conditions is a bounded delay for fresher telemetry worth its opportunity cost compared with immediate, age-aware placement?

## Evidence streams must stay separate

**Synthetic**: a deterministic research simulator; a fixed factorial of 324 matched cases / 648 policy runs. It can isolate configured mechanisms but does not estimate performance prevalence in a real deployment. Configured factors include worker perturbed, capacity change, telemetry delay, deadlines, maximum waiting, and known dispatch latency.

**Real local kind**: 24 sequential, controlled single-host Kubernetes Pod trials across 12 blocks in three fixed runs. Two Docker-backed worker nodes share one physical PC. Pod scheduling uses the normal kube-scheduler together with node selection, not a deployed custom scheduler. Heavy CPU *requests* enforce admission boundaries, while containers sleep instead of exercising CPU at scale.

## The defensible positive result

In the controlled kind feasibility-boundary experiment, bounded deferral completed **12/12** observed jobs within the deadline versus **6/12** for immediate age-aware placement, with an average extra intentional waiting time of approximately **1.86 seconds**. All six differences were observed when the lexically first worker was perturbed; the other six blocks were ties. This demonstrates a real *mechanism* under this setup: waiting can allow a later CPU-request snapshot to avoid a placement that appeared feasible on stale information.

**Never label this a statistically demonstrated 50-percentage-point general improvement.** Sequential arms do not share identical physical microstates; the design is not a randomized, independently replicated multi-host field study. Recorded `TimedOutObservation` indicates a missed observation deadline, not necessarily permanent unschedulability.

## The defensible negative result

When simulated capacity was unchanged or changed without making a placement infeasible, waiting provided **no on-time completion benefit** (108/108 vs 108/108 in each control stratum), and cost extra waiting time. Prior fixed synthetic reliability stress tests also found bounded deferral harmful under some node-outage and external-contention conditions. Do not omit these unfavorable conditions.

## Critical confound already audited

Matched-input replay found that lexical and reverse greedy tie-breaking each selected a fresh-infeasible worker on **8/16** earlier inputs, but for opposite worker perturbations. Random tie-breaking was evaluated analytically, not deployed. Name order materially affects the naive greedy baseline; compare claims with a stronger age-aware immediate baseline and report per-worker strata.

## What is *not* established

- General causal superiority of bounded waiting, or statistical significance of the real-cluster trial rates.
- Independent physical edge devices, external cloud service/network telemetry, or broad deployment generalizability.
- Measured CPU utilization, energy savings, throughput advantage, or real controller restart reliability.
- Deployment of a production-grade Kubernetes custom scheduler.
- Novelty against the published research literature, peer review, patents, or paper acceptance.

## Research-paper framing

The contribution is a **controlled experimental study and reproducible reference implementation** of a deadline-sensitive value-of-information tradeoff in the presence of stale capacity telemetry. The conclusion should be conditional: limited waiting can avoid an infeasible CPU-request placement when timely informative telemetry arrives, but the extra waiting may waste deadline budget and provide no benefit when the information is redundant, late, or faults dominate.

The final report must explicitly separate synthetic results, kind observations, limitations, and future multi-host validation. Do not imply that previously fabricated or simulated data were obtained from live Kubernetes.
