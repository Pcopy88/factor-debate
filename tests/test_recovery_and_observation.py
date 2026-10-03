import json
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from research import db
from research.api import app,create_task
from research.schemas import TaskConfig,ModelProfile
from research.data import DataStore,register_dataset
from research.workflow import run_workflow
from research.evaluation import engine_signature,hac_test


def test_crash_after_approval_does_not_apply_to_next_gate(tiny_data,monkeypatch):
    import research.workflow as w
    original=w.propose
    def crash(state):raise RuntimeError('simulated process interruption')
    monkeypatch.setattr(DataStore,'audit',lambda s,c:{'passed':True,'errors':[]})
    monkeypatch.setattr(w,'propose',crash)
    tid=create_task(TaskConfig(name='recovery',dataset_id='tiny'))['id']
    decision={'gate':1,'action':'approve','request_id':'first-approval'}
    with pytest.raises(RuntimeError):run_workflow(tid,decision)
    monkeypatch.setattr(w,'propose',original)
    run_workflow(tid,decision)
    assert db.task(tid)['gate']==5
    run_workflow(tid,decision)
    assert db.task(tid)['gate']==5
    assert not db.rows('SELECT * FROM experiments WHERE task_id=?',(tid,))


def test_pause_queued_job_preserves_decision(tiny_data):
    client=TestClient(app)
    t=create_task(TaskConfig(name='paused',dataset_id='tiny'))
    decision={'gate':1,'action':'approve','request_id':'keep-this-approval','expected_version':t['version']}
    assert client.post(f"/api/tasks/{t['id']}/decisions",json=decision).status_code==200
    client.post(f"/api/tasks/{t['id']}/pause")
    client.post(f"/api/tasks/{t['id']}/resume")
    job=db.one('SELECT * FROM jobs WHERE task_id=?',(t['id'],))
    assert job['status']=='queued' and json.loads(job['payload'])['resume']['request_id']==decision['request_id']


def test_failed_data_audit_returns_editable_intake(tiny_data):
    t=create_task(TaskConfig(name='audit',dataset_id='tiny'))
    run_workflow(t['id'],{'gate':1,'action':'approve','request_id':'audit-approval'})
    assert db.task(t['id'])['gate']==1
    cfg=db.task(t['id'])['config']
    cfg['name']='修改后的任务'
    assert TestClient(app).put(f"/api/tasks/{t['id']}",json=cfg).status_code==200
    assert any(a['payload']['passed'] is False for a in db.get_artifacts(t['id'],'data_audit'))


def test_unknown_model_price_is_rejected():
    with pytest.raises(ValueError,match='报价'):
        ModelProfile(name='unknown',model='test',secret_name='test',input_cny_per_million=0,output_cny_per_million=0)


def test_short_sample_has_no_significance_claim():
    stats=hac_test([.1,.2,.3],3)
    assert stats['t'] is None and stats['p_adjusted'] is None


def test_nan_prices_are_rejected(tiny_data):
    f,m=tiny_data
    m.loc[0,'close']=float('nan')
    with pytest.raises(ValueError,match='有限数值'):register_dataset('bad',f,m,'test',True,True)


def freeze_empty(tid):
    cfg=db.task(tid)['config']
    frozen={'candidate_ids':[],'formulas':[],'config':cfg,'engine_signature':engine_signature()}
    db.artifact(tid,'frozen_batch','system','frozen',frozen)
    with db.connect() as c:c.execute('UPDATE tasks SET frozen_hash=? WHERE id=?',(db.digest(frozen),tid))


def test_forward_waits_for_new_dates(tiny_data):
    from research.observation import run_forward
    tid=create_task(TaskConfig(name='forward',dataset_id='tiny'))['id']
    freeze_empty(tid)
    result=run_forward(tid,'tiny')
    assert result['status']=='waiting'
    assert not db.rows('SELECT * FROM results',final=True)


def test_forward_rejects_rewritten_history(tiny_data):
    from research.observation import run_forward
    f,m=tiny_data
    tid=create_task(TaskConfig(name='forward',dataset_id='tiny'))['id']
    freeze_empty(tid)
    m.loc[0,'close']=11.
    register_dataset('modified',f,m,'test',True,True)
    with pytest.raises(ValueError,match='覆盖或删除'):run_forward(tid,'modified')


def test_empty_proposal_ends_normally(tiny_data,monkeypatch):
    import research.workflow as w
    from research.schemas import AgentOutput
    monkeypatch.setattr(DataStore,'audit',lambda s,c:{'passed':True,'errors':[]})
    monkeypatch.setattr(w,'run_agent',lambda *args,**kwargs:AgentOutput(explanation='没有足够证据'))
    tid=create_task(TaskConfig(name='empty',dataset_id='tiny'))['id']
    run_workflow(tid,{'gate':1,'action':'approve'})
    assert db.task(tid)['status']=='completed'
    assert db.task(tid)['final_status']=='no_candidates'


def test_final_window_overlap_stops_before_evaluation(tiny_data,monkeypatch):
    import research.workflow as w
    tid=create_task(TaskConfig(name='window',dataset_id='tiny',mode='live',model_profile_id='unused',budget_cny=1))['id']
    freeze_empty(tid)
    with db.connect(final=True) as c:
        c.execute('INSERT INTO windows VALUES(?,?,?,?)',('live|2024-01-01|2025-12-31','older-task','frozen',db.now()))
    def forbidden(*a,**k):raise AssertionError('must not load held-out data')
    monkeypatch.setattr(w,'DataStore',forbidden)
    w.final_test({'task_id':tid,'candidates':['frozen-candidate']})
    assert db.task(tid)['final_status']=='previously_used'
    assert not db.rows('SELECT * FROM results',final=True)


def test_unverified_source_can_be_reviewed_without_rewriting_content():
    from research.literature import add_document,research_documents
    from research.schemas import DocumentInput
    source=DocumentInput(title='试验资料',url='https://example.org/source',publisher='作者',published_date='2020-01-01',
                         version_date='2020-01-01',text='用于核验研究工作流的原始文献段落。'*20)
    identifier=add_document(source)
    assert research_documents('2022-12-31')==[]
    source.verified=True
    source.evidence_note='已核对作者原文首页及明确版本日期，与当前文件一致。'
    assert add_document(source)==identifier
    assert research_documents('2022-12-31')[0]['id']==identifier
    assert len(db.rows('SELECT * FROM document_reviews'))==1
