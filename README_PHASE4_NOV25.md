# EdgePilot v2 — Phase 4 final research release package

This package is designed to be **merged into** the existing `research-v2` project after its Phase 3 commit `6a8cfd5`. It does not replace existing modules or rewrite old experiments.

Files included:

- `research/final_figures.py`: offline, fail-closed figure generator from frozen audited CSVs.
- `tests/test_final_figures.py`: five additional validation tests (baseline 525 → expected 530).
- `docs/research-v2/PAPER_DRAFT.md` and `docs/research-v2/EdgePilot_v2_Research_Draft.docx`: manuscript-style draft, with literature references, two figures, separate synthetic/kind evidence and explicit limitations.
- `docs/research-v2/figures/*.png`: *preview figures*, derived from the verified values reported in the user's terminal; authoritative regeneration uses original local CSV files.
- `docs/research-v2/README_RESEARCH_V2.md`: project/research README without overwriting the repository's original README.
- `docs/research-v2/REPRODUCIBILITY.md`, `DEMO_SCRIPT.md`, `RELEASE_CHECKLIST.md`, `GOOGLE_RESEARCH_SUMMARY.md`.

## Step 1 — Merge + verify

```powershell
cd D:\Development\edgepilot-v2
python -m pytest -q
```

**Expected:** 530 tests pass if all five new tests are detected and no old code changed. If tests fail, do not commit.

## Step 2 — Generate figures from ACTUAL local saved evidence

```powershell
python -m research.final_figures `
  --synthetic research_outputs/phase2_sensitivity_01 `
  --real research_outputs/kubernetes/phase2_real_report_01 `
  --output research_outputs/final_figures_01
```

**Expected:** strict input count verification, two PNGs, a JSON summary recording input SHA-256. The generator does NOT contact Kubernetes or modify prior data.

## Step 3 — Check paper and docs

Read `docs/research-v2/PAPER_DRAFT.md` and its DOCX rendering. Ensure author, institution and exact authorship of contributions are accurate. The paper is a **draft**; it is not an accepted publication. Its references are grounded to publisher/project websites, but originality has not been independently established.

## Step 4 — Commit

```powershell
git add research/final_figures.py tests/test_final_figures.py README_PHASE4_NOV25.md docs/research-v2
git commit -m "docs(research): finalize evidence-grounded paper and reproducibility package"
git status
```

Do **not** add `research_outputs/` automatically. It is intentionally Git-ignored. Back up original experimental records separately.

## Step 5 — Release freeze, no need for new pods

Run the existing Phase 3 `research.release_audit` command again with a **new** output directory, confirm a clean working tree, and inspect the remote before `git push -u origin research-v2`. Release tag only after completing the final checklist. Do not claim final-release status merely by extracting this archive; local integration and QA are still required.

## Manuscript refinements, 12 November 2026

- Retitled the paper in the Markdown, DOCX and PDF versions for formal academic presentation.
- Clarified Windows/WSL 2/Docker Desktop host scheduling jitter in the internal-validity paragraph.
- Updated the Sparrow citation to the SOSP 2013 proceedings PDF; the existing Berkeley mirror was intermittently inaccessible.
- **Before public release:** Verify the author/affiliation placeholder and credit, confirm that the manuscript describes only completed work, and keep the paper marked a draft. These revisions do not establish research novelty or peer-review acceptance.
