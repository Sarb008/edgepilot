# EdgePilot v2: Hard-stop research sprint (12–25 Nov 2026)

**Nonnegotiable project release date: Wednesday, 25 November 2026.**
**Google Student Researcher application target: Saturday, 28 November 2026**, ahead of the user's stated 4 December deadline. Verify all application requirements independently before submission.

| Dates | Required output | Exit criterion |
|---|---|---|
| Nov 12–15 | **Phase 1 — Value-of-waiting mechanism** (this package) | Fixed matched-input simulator sweep; actual bounded vs immediate common-old-snapshot kind pilot (or documented blocked execution); preserved traces and offline audit |
| Nov 16–19 | **Phase 2 — Consolidated empirical validation** | One preregistered, bounded set of repeat trials and critical sensitivity axes (telemetry age, request margin, deadlines) with both favorable AND unfavorable findings included; reproducible CSV and plots |
| Nov 20–22 | **Phase 3 — Engineering/research audit and code freeze** | Full tests, event-order/no-leak checks, safe Kubernetes cleanup, methodological limitations, runnable instructions; no critical reproducibility blockers |
| Nov 23–25 | **Phase 4 — Final release** | Paper-style report with architecture, methods, results (synthetic vs kind explicitly separated), negative controls, limitations, polished README, demo script, tracked release tag |
| Nov 26–28 | **Application** | Research CV + concise project summary + application material review and submit before user-supplied December deadline |

## Resource boundary

College exams take precedence. On exam-heavy days, complete *zero or one bounded verification task*. No new systems, frameworks, expensive cloud setup, extra hardware, or continuous monitoring is required for the November release. Keep all raw data. Freeze experimental hypotheses and condition grids **before** repeated live runs. A failed hypothesis is a publishable *finding only if well-supported*; it is not a reason to keep running unplanned tests until a success appears.

## Stop conditions

- **Phase 1** ends once the mechanism study and the corresponding two-arm prospective pilot have either produced auditable results or a precisely documented external blocker.
- **Phase 2** ends after the predefined matrix and statistical analysis; power limitations and negative results are *disclosed*, not hidden.
- **Phase 3** freezes new features on Nov 22; allow only critical correctness fixes afterward.
- **Phase 4** publishes the research package Nov 25. Journal acceptance and verified originality are beyond our control and are not claimed.

**Avoid milestone-letter inflation**: use four named phases, one final release and one application. No infinite 6J/6K chain.
