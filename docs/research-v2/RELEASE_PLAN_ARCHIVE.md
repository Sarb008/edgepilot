# EdgePilot v2 — November 25 hard release checklist

**Scope frozen:** no new algorithm families, no fresh Kubernetes trial cycles, no moving finish date to chase positive results. Critical defects uncovered by the release gate take priority over cosmetic tasks.

## 12 November — Phase 3 complete

- [x] 525 automated tests passed on Windows.
- [x] Phase 3 offline gate passed: 324 synthetic cases / 648 policy runs; 24 real Pod trials / 12 blocks.
- [x] Code committed: `6a8cfd5` on `research-v2`, clean Git status.
- [x] Frozen evidence ledger identifies favorable findings, negative results, confounds and limitations.

## 13–18 November — Paper and documentation QA

- [ ] Integrate this package; run the five new figure-integrity tests and full suite (expected 530 if no test conflicts).
- [ ] Generate final figures **from real saved CSVs** and compare labels/values to the paper.
- [ ] Review `PAPER_DRAFT.md` and DOCX for author list, exact phrasing, unsupported novelty claims and literature metadata.
- [ ] Verify every paper table against the frozen `RELEASE_GATE.md` and CSVs; report never calls a timeout permanent failure.
- [ ] Cross-check documentation commands on Windows; note that `.venv` activation may require a process-scoped execution policy.

## 19–22 November — Code and evidence freeze

- [ ] Re-run full tests and release audit in a **new** output directory.
- [ ] Review `git diff` for secrets, accidental experiment results, generated venv files and unknown local paths.
- [ ] Keep `research_outputs` backed up privately; do not treat ignored results as Git-protected.
- [ ] Confirm `git status` is clean; push a reviewed `research-v2` branch to the intended remote.
- [ ] Code freeze after documented critical fixes, no new evaluation campaigns.

## 23–25 November — Final deliverables

- [ ] Final manuscript with correct authors/affiliations, two figures, honest limitations and verified references.
- [ ] Complete project README, reproducibility guide, 3-minute demo and paper source/docs.
- [ ] Verified archived evidence index and reproducibility manifest; public raw evidence only after privacy review.
- [ ] Create and push an annotated *release tag* only after verifying branch/remote and clean tests, e.g. `v2.0-research-2026-11-25`.
- [ ] Declare the project **released on 25 November**; leftover high-risk external validation is labeled future work, not silently claimed complete.

## Application window: 26–28 November

- [ ] Tailor a one-page research-oriented CV and an accurate EdgePilot description.
- [ ] Apply to the official Google Student Researcher 2027 India listing before its stated **4 December** deadline; target **28 November** or earlier because review is rolling.
- [ ] Do not claim peer-reviewed publication, novel algorithm or production Kubernetes deployment.

## Final red flags: automatic stop before public claims

Unsupported result changes; source/hash mismatches; an unexpected test failure; missing trial outcomes; claims that CPU requests equal utilization; claims of causal 50-point improvement; undisclosed single-host kind limitations; public disclosure of credentials/identifiable metadata. Fix documentation/critical correctness first; otherwise release accurately with limitations.
