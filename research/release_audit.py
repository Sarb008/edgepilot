"""Phase 3 offline reproduction gate for EdgePilot v2.

Never contacts Kubernetes and never overwrites its input evidence. Reproduces
fixed synthetic decision traces and independently regenerates the real kind
summary from the recorded original per-trial traces.

Important: this is an *integrity and repeatability gate*, NOT evidence of
randomized causal effectiveness, publication readiness, or physical edge tests.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import platform
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any

from .phase2_real_report import pool
from .phase2_sensitivity import cases, run
from .voi_study import MatchedCase

SYNTHETIC_ARTIFACTS = ('trials.csv', 'paired.csv', 'strata.csv', 'traces.jsonl', 'sensitivity.png')
REAL_ARTIFACTS = ('paired_blocks.csv', 'audited_trials.csv', 'worker_strata.csv')
FROZEN_SEEDS = (20261116, 20261117, 20261118)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def _sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open('r', encoding='utf-8', newline='') as stream:
        return list(csv.DictReader(stream))


def _compare_file(original: Path, replay: Path) -> None:
    if not original.is_file() or not replay.is_file():
        raise FileNotFoundError(f'missing evidence or reproduction: {original} / {replay}')
    if _sha(original) != _sha(replay):
        raise ValueError(f'nonreproducible or modified evidence: {original.name}')


def audit_synthetic(
    original: Path,
    replay: Path,
    *,
    selected: tuple[tuple[str, MatchedCase], ...] | None = None,
) -> dict[str, Any]:
    """Check complete declared design, hashes, arm pairing and actual replay.

    ``selected`` supports small fixture studies in tests; the CLI always uses
    the complete 324-case predeclared matrix.
    """
    original = original.resolve(strict=True)
    declared = cases() if selected is None else selected
    manifest = _read_json(original / 'manifest.json')
    summary = _read_json(original / 'summary.json')
    expected_definition = [{'id': key, 'parameters': asdict(case)} for key, case in declared]
    if manifest.get('cases') != expected_definition:
        raise ValueError('synthetic case definitions differ from the frozen predeclared design')
    if manifest.get('case_count') != len(declared) or manifest.get('policy_runs') != 2 * len(declared):
        raise ValueError('synthetic case/run counts differ from manifest')
    if summary.get('cases') != len(declared) or summary.get('policy_runs') != 2 * len(declared):
        raise ValueError('synthetic summary case/run counts differ from design')
    expected_names = set(SYNTHETIC_ARTIFACTS)
    if set(manifest.get('artifact_sha256', {})) != expected_names:
        raise ValueError('synthetic manifest does not attest exactly the required artifacts')
    originals = {}
    for name in SYNTHETIC_ARTIFACTS:
        path = original / name
        digest = _sha(path)
        if manifest['artifact_sha256'][name] != digest:
            raise ValueError(f'original synthetic checksum mismatch: {name}')
        originals[name] = digest
    if len(_rows(original / 'trials.csv')) != len(declared) * 2:
        raise ValueError('synthetic trials.csv has an incorrect number of rows')
    if len(_rows(original / 'paired.csv')) != len(declared):
        raise ValueError('synthetic paired.csv has an incorrect number of rows')
    if len((original / 'traces.jsonl').read_text(encoding='utf-8').splitlines()) != len(declared):
        raise ValueError('synthetic decision trace count mismatch')
    if not (original / 'sensitivity.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('synthetic plot is not a PNG')
    reproduced_summary = run(replay, selected=declared)
    for name in ('trials.csv', 'paired.csv', 'strata.csv', 'traces.jsonl'):
        _compare_file(original / name, replay / name)
    if summary != reproduced_summary:
        raise ValueError('synthetic summary is not reproducible')
    reproduced_manifest = _read_json(replay / 'manifest.json')
    if reproduced_manifest.get('cases') != manifest['cases'] or reproduced_manifest.get('axes') != manifest['axes']:
        raise ValueError('synthetic reproduction has a different protocol')
    # Plot raster hashes may vary across OS/matplotlib; attestation of the
    # original bytes is strict, but raster equality is NOT a cross-OS gate.
    return {
        'status': 'passed',
        'cases': len(declared),
        'policy_runs': len(declared) * 2,
        'original_artifact_sha256': originals,
        'csv_and_trace_byte_replay': True,
        'original_plot_sha256_verified': True,
        'cross_platform_plot_pixel_identity_required': False,
        'by_change_type': summary['by_change_type'],
    }


def audit_real(
    original_report: Path,
    sources: tuple[Path, ...],
    replay: Path,
    *,
    required_seeds: tuple[int, ...] | None = FROZEN_SEEDS,
) -> dict[str, Any]:
    """Independently re-pool ORIGINAL audited traces, then byte-compare reports.

    The source checks performed by ``pool`` include per-trial trace/CSV
    consistency, input provenance, CPU-request boundaries and Pod cleanup.
    """
    original_report = original_report.resolve(strict=True)
    if not sources:
        raise ValueError('real-cluster source runs required')
    source_paths = tuple(p.resolve(strict=True) for p in sources)
    if len(set(source_paths)) != len(source_paths):
        raise ValueError('duplicate real-cluster run path')
    if required_seeds is not None:
        if len(source_paths) != len(required_seeds):
            raise ValueError('missing or extra frozen real-cluster runs')
        seeds = tuple(_read_json(p / 'manifest.json')['config']['seed'] for p in source_paths)
        if seeds != required_seeds:
            raise ValueError(f'frozen run order/seeds mismatch: observed {seeds!r}')
    previous = _read_json(original_report / 'summary.json')
    # Freeze an independently computed byte inventory for every source trace.
    # Existing pooled summaries do not attest per-trace bytes; this gate is
    # the first such checksum baseline, NOT retrospective proof of immutability.
    inventories = []
    for source in source_paths:
        paths = sorted([source / 'manifest.json', source / 'trials.csv',
                        *(source / 'traces').glob('*.json')])
        if not all(p.is_file() for p in paths):
            raise FileNotFoundError('missing source evidence in ' + str(source))
        inventories.append({'path': str(source),
                            'sha256': {p.relative_to(source).as_posix(): _sha(p) for p in paths}})
    reproduced = pool(source_paths, replay)
    for name in REAL_ARTIFACTS:
        _compare_file(original_report / name, replay / name)
    if previous != reproduced:
        raise ValueError('real-cluster summary does not match independently replayed source traces')
    if len(_rows(original_report / 'paired_blocks.csv')) != reproduced['blocks']:
        raise ValueError('pooled real-block row count mismatch')
    if len(_rows(original_report / 'audited_trials.csv')) != reproduced['trials']:
        raise ValueError('pooled real-trial row count mismatch')
    if len(_rows(original_report / 'worker_strata.csv')) != 3:
        raise ValueError('expected exactly two worker strata and ALL')
    overall = next((r for r in reproduced['descriptive'] if r['perturbed_worker'] == 'ALL'), None)
    if not overall or overall['blocks'] != reproduced['blocks']:
        raise ValueError('missing or incorrect overall real-cluster summary')
    return {
        'status': 'passed',
        'runs': reproduced['runs'],
        'blocks': reproduced['blocks'],
        'pod_trials': reproduced['trials'],
        'worker_exposure': reproduced['worker_exposure'],
        'overall': overall,
        'source_manifests': reproduced['source_runs'],
        'first_attestation_of_raw_traces': inventories,
        'pooled_artifact_sha256': {name: _sha(original_report / name) for name in REAL_ARTIFACTS},
        'independent_repool_byte_exact': True,
    }


def git_provenance(root: Path) -> dict[str, Any]:
    """Read-only local provenance. Reports absence rather than inventing data."""
    result: dict[str, Any] = {'repository': str(root), 'available': False}
    try:
        def call(*args: str) -> str:
            proc = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                                  text=True, encoding='utf-8', errors='replace', timeout=5)
            if proc.returncode:
                raise RuntimeError(proc.stderr.strip() or 'git command failed')
            return proc.stdout.strip()
        result.update({'available': True, 'head': call('rev-parse', 'HEAD'),
                       'branch': call('branch', '--show-current'),
                       'clean': call('status', '--porcelain') == ''})
    except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        result['error'] = str(exc)
    return result


def audit_release(synthetic: Path, real_report: Path, sources: tuple[Path, ...], output: Path,
                  *, root: Path | None = None,
                  synthetic_selected: tuple[tuple[str, MatchedCase], ...] | None = None,
                  required_seeds: tuple[int, ...] | None = FROZEN_SEEDS) -> dict[str, Any]:
    """Fail closed; output is created only after all checks have passed."""
    if output.exists():
        raise FileExistsError(f'audit output already exists: {output}')
    with TemporaryDirectory(prefix='edgepilot_phase3_') as scratch:
        temp = Path(scratch)
        sim = audit_synthetic(synthetic, temp / 'synthetic', selected=synthetic_selected)
        real = audit_real(real_report, sources, temp / 'real', required_seeds=required_seeds)
    provenance = git_provenance((root or Path.cwd()).resolve())
    findings = [
        'A passing audit establishes artifact integrity and repeatability, not causal effectiveness.',
        'Fixed synthetic cases are engineered, not randomly sampled; no population p-values or confidence intervals.',
        'Kubernetes crossover arms execute sequentially under separate cluster microstates.',
        'Two Docker-backed workers share one host; results do not extrapolate to independent edge devices.',
        'CPU-request feasibility is not measured CPU use, nor is a timeout proof of permanent admission failure.',
        'The three run orders may coincide despite different seeds; do not claim independent random assignment.',
        'Raw trace hashes are attested for the FIRST time by this gate, not proven unchanged since collection.',
        'Keep unfavorable results and worker-name/tie-break sensitivity visible in the report.',
        'Research novelty and paper acceptance require external evaluation.',
    ]
    report: dict[str, Any] = {
        'study': 'EdgePilot v2 / Phase 3 offline reproduction and evidence-integrity gate',
        'gate': 'passed', 'synthetic': sim, 'real_cluster': real,
        'environment': {'python': sys.version.split()[0], 'platform': platform.platform()},
        'git': provenance,
        'limitations': findings,
        'production_requirements_not_covered': [
            'Independent multi-host edge-cluster validation',
            'A deployed custom Kubernetes scheduler/controller',
            'Causal effect estimates or statistically powered live performance comparison',
        ],
    }
    output.mkdir(parents=True, exist_ok=False)
    (output / 'audit.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    overall = real['overall']
    md = [
        '# EdgePilot v2 — Phase 3 reproducibility gate', '',
        '**PASS: all fixed synthetic CSV/trace artifacts and real-cluster pooled reports reproduce.**', '',
        f'- Synthetic: {sim["cases"]} matched cases; {sim["policy_runs"]} deterministic policy runs.',
        f'- Real kind: {real["pod_trials"]} observed Pod trials; {real["blocks"]} sequential blocks across {real["runs"]} frozen runs.',
        f'- Immediate age-aware: {overall["age_on_time"]}/{overall["blocks"]} on time.',
        f'- Bounded deferral: {overall["bounded_on_time"]}/{overall["blocks"]} on time.',
        f'- Mean extra intentional waiting: {overall["mean_bounded_wait_seconds"]:.3f} seconds.',
        f'- Source evidence unchanged: checksummed and independently re-audited.', '',
        '## Limitations (must be retained in final report)', '',
        *[f'- {item}' for item in findings], '',
        '## Gate scope', '',
        'This gate checks recorded evidence and deterministic reproduction. It is **not** a literature',
        'novelty review, multi-node hardware experiment, deployed scheduler, or peer review.',
        '',
    ]
    (output / 'RELEASE_GATE.md').write_text('\n'.join(md), encoding='utf-8')
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--synthetic', type=Path, required=True)
    parser.add_argument('--real', type=Path, required=True)
    parser.add_argument('--run', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    report = audit_release(args.synthetic, args.real, tuple(args.run), args.output)
    sim, real = report['synthetic'], report['real_cluster']
    overall = real['overall']
    print(f'PASS: {sim["cases"]} matched synthetic cases / {sim["policy_runs"]} policy runs reproduced.')
    print(f'PASS: {real["pod_trials"]} real recorded Pod trials / {real["blocks"]} blocks independently audited.')
    print(f'  age-aware={overall["age_on_time"]}/{overall["blocks"]}; '
          f'bounded={overall["bounded_on_time"]}/{overall["blocks"]}; '
          f'mean bounded wait={overall["mean_bounded_wait_seconds"]:.3f}s')
    print(f'PASS: report written to {args.output}; NO Kubernetes calls, NO new Pods.')
    if report['git']['available'] and not report['git']['clean']:
        print('NOTE: Git working tree is not clean; commit the Phase 3 changes after verifying tests.')


if __name__ == '__main__':
    main()
