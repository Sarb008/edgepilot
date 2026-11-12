"""Rebuild EdgePilot v2 release figures from audited Phase 2 CSV evidence.

Strictly offline. Does not modify original evidence, invoke Kubernetes, or infer
causal/general effects from sequential single-host kind experiments.

Usage:
 python -m research.final_figures --synthetic research_outputs/phase2_sensitivity_01 \
   --real research_outputs/kubernetes/phase2_real_report_01 \
   --output research_outputs/final_figures_01
"""
from __future__ import annotations
import argparse
import csv
from hashlib import sha256
import json
from pathlib import Path


def _read(path: Path):
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _integer(row: dict[str, str], key: str) -> int:
    val = row.get(key, '')
    try:
        number = int(val)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'invalid {key}: {val!r}') from exc
    if number < 0:
        raise ValueError(f'negative {key}')
    return number


def verify(synthetic: Path, real: Path) -> dict:
    synth_rows = _read(synthetic / 'strata.csv')
    real_rows = _read(real / 'worker_strata.csv')
    relevant = [r for r in synth_rows if r.get('factor') == 'change_type']
    sim = {r['level']: r for r in relevant}
    if len(relevant) != 3 or set(sim) != {'unchanged','feasible_change','infeasible_change'}:
        raise ValueError('synthetic change_type strata are missing/duplicated')
    for k, r in sim.items():
        n = _integer(r, 'paired_cases')
        a = _integer(r, 'age_on_time'); b = _integer(r, 'bounded_on_time')
        if n != 108 or a > n or b > n:
            raise ValueError(f'unexpected/inconsistent frozen synthetic stratum {k}')
    if [(sim[k]['age_on_time'],sim[k]['bounded_on_time']) for k in ('unchanged','feasible_change','infeasible_change')] != [('108','108'),('108','108'),('54','84')]:
        raise ValueError('frozen synthetic counts do not match research claims')
    overall = [r for r in real_rows if r.get('perturbed_worker') == 'ALL']
    workers = [r for r in real_rows if r.get('perturbed_worker') != 'ALL']
    if len(overall)!=1 or len(workers)!=2 or len({r['perturbed_worker'] for r in workers})!=2:
        raise ValueError('expected exactly two worker rows and one ALL row')
    all_row = overall[0]
    for key, expected in [('blocks',12),('age_on_time',6),('bounded_on_time',12),('bounded_better',6),('age_better',0),('ties',6)]:
        if _integer(all_row,key)!=expected:
            raise ValueError(f'frozen all-worker {key} mismatch')
    if sum(_integer(r,'blocks') for r in workers)!=12:
        raise ValueError('worker block counts do not match overall')
    if sorted((_integer(r,'blocks'),_integer(r,'age_on_time'),_integer(r,'bounded_on_time')) for r in workers) != [(6,0,6),(6,6,6)]:
        raise ValueError('unexpected worker-level results')
    try:
        wait=float(all_row['mean_bounded_wait_seconds'])
    except (TypeError, ValueError) as exc:
        raise ValueError('missing/invalid wait time') from exc
    if not 1.7 <= wait <= 2.0:
        raise ValueError('unexpected wait value')
    return {'synthetic':{k:{'cases':108,'age_on_time':int(sim[k]['age_on_time']), 'bounded_on_time':int(sim[k]['bounded_on_time'])} for k in ('unchanged','feasible_change','infeasible_change')},
            'real':{'blocks':12,'age_on_time':6,'bounded_on_time':12,'mean_bounded_wait_seconds':wait,'workers':workers},
            'inputs_sha256':{str(p):sha256(p.read_bytes()).hexdigest() for p in [synthetic/'strata.csv',real/'worker_strata.csv']},
            'interpretation':'Descriptive sequential single-host kind; no general causal claim.'}


def render(summary: dict, output: Path) -> None:
    if output.exists():
        raise FileExistsError(f'refusing to overwrite output: {output}')
    # Delay matplotlib import until source evidence is validated.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    output.mkdir(parents=True, exist_ok=False)
    s=summary['synthetic']
    labels=['Unchanged','Feasible change','Infeasible change']
    keys=['unchanged','feasible_change','infeasible_change']
    age=[s[k]['age_on_time']/108*100 for k in keys]
    bounded=[s[k]['bounded_on_time']/108*100 for k in keys]
    fig,ax=plt.subplots(figsize=(9.3,4.8))
    pos=list(range(3)); w=.35
    ax.bar([x-w/2 for x in pos],age,w,label='Immediate age-aware')
    ax.bar([x+w/2 for x in pos],bounded,w,label='Bounded deferral')
    ax.set_xticks(pos,labels)
    ax.set_ylim(0,115); ax.set_ylabel('On-time outcomes in fixed design (%)')
    ax.set_title('Deterministic simulator: 108 configured cases per condition')
    ax.legend(frameon=False,loc='upper right')
    for collection in ax.containers:
        ax.bar_label(collection,fmt='%.1f%%',padding=3,fontsize=9)
    fig.text(.08,.01,'Engineered cases; not an empirical population estimate.',fontsize=9)
    fig.tight_layout(rect=[0,.035,1,1]); fig.savefig(output/'synthetic_conditions.png',dpi=180);plt.close(fig)
    fig,ax=plt.subplots(figsize=(9.3,4.8))
    labels=['First worker perturbed','Second worker perturbed','Overall']
    age=[0,100,50]; bounded=[100,100,100]
    pos=list(range(3));w=.35
    ax.bar([x-w/2 for x in pos],age,w,label='Immediate age-aware')
    ax.bar([x+w/2 for x in pos],bounded,w,label='Bounded deferral')
    ax.set_xticks(pos,labels);ax.set_ylim(0,115)
    ax.set_ylabel('Observed on-time completions (%)')
    ax.set_title('Local kind: 24 sequential Pod trials across 12 blocks')
    ax.legend(frameon=False,loc='upper right')
    for collection in ax.containers:
        ax.bar_label(collection,fmt='%.0f%%',padding=3,fontsize=9)
    fig.text(.08,.01,'Shared physical host; sequential arms; not a causal effect estimate.',fontsize=9)
    fig.tight_layout(rect=[0,.035,1,1]);fig.savefig(output/'kubernetes_worker_strata.png',dpi=180);plt.close(fig)
    with (output/'evidence_summary.json').open('w',encoding='utf-8') as f:
        json.dump(summary,f,indent=2); f.write('\n')
    (output/'README.txt').write_text('Figures regenerated from audited evidence. Do not generalize causal superiority from these plots.\n',encoding='utf-8')


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--synthetic',type=Path,required=True)
    ap.add_argument('--real',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args(argv)
    summary=verify(args.synthetic,args.real)
    render(summary,args.output)
    print(f"PASS: frozen 324 synthetic cases and 12 kind blocks verified. Figures: {args.output}")
    print('Evidence is descriptive; no causal or cross-cluster generalization.')

if __name__ == '__main__':
    main()
