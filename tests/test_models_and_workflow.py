import json
import httpx
import pytest
from research import db
from research.schemas import TaskConfig,ModelProfile,AgentOutput
from research.api import create_task,profile_create
from research.models import call_model,BudgetExhausted,UncertainCall
from research.workflow import run_workflow


def setup_model(monkeypatch,budget=5):
    from research import models
    monkeypatch.setattr(models,'public_url',lambda x:x)
    monkeypatch.setattr(models,'get_secret',lambda x:'sk-test-secret-never-log-123456')
    p=ModelProfile(name='mock',model='test',secret_name='test',input_cny_per_million=10,output_cny_per_million=20)
    pid=profile_create(p)['id']
    tid=create_task(TaskConfig(name='model',dataset_id='tiny',mode='live',model_profile_id=pid,budget_cny=budget))['id']
    return pid,tid


def test_budget_is_reserved_before_network(tiny_data,monkeypatch):
    pid,tid=setup_model(monkeypatch,.001)
    def forbidden(*a,**k):raise AssertionError('network must not be called')
    monkeypatch.setattr(httpx.Client,'post',forbidden)
    with pytest.raises(BudgetExhausted):call_model(tid,pid,'stage','system','user')
    assert not db.rows('SELECT * FROM calls')


def test_completed_model_call_reused_and_secret_not_logged(tiny_data,monkeypatch):
    pid,tid=setup_model(monkeypatch)
    count=[]
    def post(*a,**k):
        count.append(1)
        return httpx.Response(200,json={'output':[{'content':[{'type':'output_text','text':AgentOutput(explanation='ok').model_dump_json()}]}],
                                        'usage':{'input_tokens':100,'output_tokens':100}})
    monkeypatch.setattr(httpx.Client,'post',post)
    a=call_model(tid,pid,'stage','system','user')
    b=call_model(tid,pid,'stage','system','user')
    assert a==b and len(count)==1
    assert db.task(tid)['spent']==pytest.approx(.003)
    assert 'sk-test-secret' not in db.dump(db.rows('SELECT * FROM calls'))


def test_uncertain_call_is_not_automatically_retried(tiny_data,monkeypatch):
    pid,tid=setup_model(monkeypatch)
    count=[]
    def fail(*a,**k):count.append(1);raise httpx.ReadTimeout('timeout')
    monkeypatch.setattr(httpx.Client,'post',fail)
    with pytest.raises(UncertainCall):call_model(tid,pid,'stage','system','user')
    with pytest.raises(UncertainCall):call_model(tid,pid,'stage','system','user')
    assert len(count)==1
    row=db.one('SELECT * FROM calls')
    assert row['status']=='uncertain' and row['reserved']>0


def test_graph_has_persistent_human_gates(tiny_data,monkeypatch):
    from research.data import DataStore
    monkeypatch.setattr(DataStore,'audit',lambda s,c:{'passed':True,'errors':[],'manifest':s.manifest})
    tid=create_task(TaskConfig(name='graph',dataset_id='tiny'))['id']
    run_workflow(tid)
    assert db.task(tid)['gate']==1
    run_workflow(tid,{'action':'approve'})
    assert db.task(tid)['gate']==5
    first=len(db.get_artifacts(tid))
    run_workflow(tid)
    assert len(db.get_artifacts(tid))==first
    assert db.task(tid)['gate']==5


def test_return_to_batch_creates_new_versions(tiny_data,monkeypatch):
    from research.data import DataStore
    monkeypatch.setattr(DataStore,'audit',lambda s,c:{'passed':True,'errors':[],'manifest':s.manifest})
    tid=create_task(TaskConfig(name='graph',dataset_id='tiny'))['id']
    run_workflow(tid,{'action':'approve'})
    before=len(db.get_artifacts(tid,'hypothesis'))
    run_workflow(tid,{'action':'return','reason':'请解释低基数','request_id':'review-again'})
    assert db.task(tid)['gate']==5
    assert len(db.get_artifacts(tid,'hypothesis'))>before

