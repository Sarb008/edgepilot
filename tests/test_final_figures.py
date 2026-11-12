"""Fail-closed release figure integrity tests. No cluster access."""
import csv
import json
import pytest
from research.final_figures import verify, render


def _write(path, headers, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=headers);w.writeheader();w.writerows(records)


def fixtures(tmp_path):
    sy=tmp_path/'sim'; re=tmp_path/'real'
    _write(sy/'strata.csv',['factor','level','paired_cases','age_on_time','bounded_on_time'],[
        {'factor':'change_type','level':'unchanged','paired_cases':108,'age_on_time':108,'bounded_on_time':108},
        {'factor':'change_type','level':'feasible_change','paired_cases':108,'age_on_time':108,'bounded_on_time':108},
        {'factor':'change_type','level':'infeasible_change','paired_cases':108,'age_on_time':54,'bounded_on_time':84}])
    _write(re/'worker_strata.csv',['perturbed_worker','blocks','age_on_time','bounded_on_time','bounded_better','age_better','ties','mean_bounded_wait_seconds'],[
        {'perturbed_worker':'edgepilot-v2-worker','blocks':6,'age_on_time':0,'bounded_on_time':6,'bounded_better':6,'age_better':0,'ties':0,'mean_bounded_wait_seconds':1.89},
        {'perturbed_worker':'edgepilot-v2-worker2','blocks':6,'age_on_time':6,'bounded_on_time':6,'bounded_better':0,'age_better':0,'ties':6,'mean_bounded_wait_seconds':1.83},
        {'perturbed_worker':'ALL','blocks':12,'age_on_time':6,'bounded_on_time':12,'bounded_better':6,'age_better':0,'ties':6,'mean_bounded_wait_seconds':1.863}])
    return sy,re


def test_valid_fixture(tmp_path):
    sy,re=fixtures(tmp_path)
    s=verify(sy,re)
    assert s['real']['blocks']==12 and s['synthetic']['infeasible_change']['bounded_on_time']==84


def test_missing_files(tmp_path):
    with pytest.raises(FileNotFoundError): verify(tmp_path/'missing',tmp_path/'bad')


def test_tampered_synthetic(tmp_path):
    sy,re=fixtures(tmp_path)
    p=sy/'strata.csv'
    p.write_text(p.read_text().replace('54,84','60,84'))
    with pytest.raises(ValueError): verify(sy,re)


def test_tampered_real(tmp_path):
    sy,re=fixtures(tmp_path)
    p=re/'worker_strata.csv'
    p.write_text(p.read_text().replace('12,6,12,6','12,8,12,6'))
    with pytest.raises(ValueError): verify(sy,re)


def test_plot_output_and_refuses_overwrite(tmp_path):
    sy,re=fixtures(tmp_path)
    out=tmp_path/'figs'
    render(verify(sy,re),out)
    assert (out/'synthetic_conditions.png').read_bytes()[:8]==b'\x89PNG\r\n\x1a\n'
    assert (out/'kubernetes_worker_strata.png').read_bytes()[:8]==b'\x89PNG\r\n\x1a\n'
    assert json.loads((out/'evidence_summary.json').read_text())['real']['blocks']==12
    with pytest.raises(FileExistsError): render(verify(sy,re),out)
