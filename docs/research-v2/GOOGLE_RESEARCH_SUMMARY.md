# Google Student Researcher 2027 — EdgePilot research summary

**Longer project description (CV, statement, application material — edit for truth and space):**

I developed EdgePilot v2, a research-oriented Python framework for studying how stale resource telemetry affects deadline-constrained workload placement. The project compares immediate age-aware scheduling with a bounded-wait strategy that can trade deadline slack for updated information. I implemented a deterministic event-driven simulation, controlled Kubernetes-in-Docker experiments, evidence auditors and regression tests. In a predeclared 324-case synthetic matrix, bounded waiting improved on-time completion only in cases where resource changes made an old placement infeasible; no on-time gain appeared in unchanged or still-feasible controls. In a separate local kind pilot spanning 24 sequential Pod trials and 12 worker-balanced blocks, bounded waiting completed 12/12 observed jobs on time versus 6/12 for immediate age-aware placement, at roughly 1.86 seconds of additional waiting per block. The result is **descriptive**, affected by worker selection and limited to a single-host local cluster. I documented negative results, tie-break confounds and reproducibility limits, and implemented an audit that independently reproduced the synthetic runs and verified the saved Kubernetes evidence.

**Short resume bullet (accurate):**

- Built EdgePilot v2, a deadline-aware stale-telemetry scheduling research framework (Python, Kubernetes/kind); implemented four policy baselines, deterministic simulation, **525 validated tests** at Phase 3, and reproducible CPU-request feasibility studies spanning **324 synthetic cases and 24 local Pod trials**; audited outcome/latency tradeoffs and documented limitations.

**30-second interview explanation:**

> “My research question was whether waiting for fresh telemetry can be more valuable than immediately dispatching a deadline-sensitive job. I implemented a simulator that separates measured, delivered and actual capacity state, then validated the mechanism with small, controlled Kubernetes experiments. The interesting part is the tradeoff: waiting sometimes avoids a stale infeasible choice, but costs deadline time and can hurt under outages. I also discovered that naive worker-name tie-breaking distorted baseline results, so I added matched-input audits and stronger age-aware comparisons. The work is a reproducible research prototype, not a production scheduler or published paper.”

**Do not write:** “improved Kubernetes scheduling by 50%”, “invented a novel scheduling algorithm”, “deployed production cloud scheduler”, “proved statistical significance”, “published research paper”, or “measured CPU utilization” unless independent evidence later substantiates each claim.

**Application timeline:** Project release target 25 November; application target by 28 November. Official India Student Researcher 2027 listing says to submit **before 4 December 2026** and notes rolling review. Official link: https://www.google.com/about/careers/applications/jobs/results/125423735539671750-student-researcher-2027
