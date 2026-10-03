import json
import pytest
from fastapi.testclient import TestClient
from research import db
from research.api import app,create_task,decide
from research.schemas import TaskConfig,Decision,DocumentInput
from research.literature import add_document,research_memory,research_documents


def doc(version='2020-01-01',verified=True):
    return DocumentInput(title='现金流研究',url='https://example.org/paper-'+version,publisher='期刊',published_date='2019-01-01',version_date=version,
                         text='原文提到现金流与应计利润的不同，检验时需要考虑行业可比性。'*10,
                         verified=verified,evidence_note='核对了原文首页日期及作者发布页，该文件为所记录的版本。')


def test_literature_cutoff_and_unverified_are_enforced_in_query():
    old=add_document(doc())
    add_document(doc('2024-01-01'))
    add_document(doc('2021-01-01',False))
    assert [d['id'] for d in research_documents('2022-12-31')]==[old]


def test_final_results_never_enter_research_memory(tiny_data):
    task=create_task(TaskConfig(name='test',dataset_id='tiny',workflow_version=1))
    with db.connect(final=True) as c:
        c.execute('INSERT INTO results VALUES(?,?,?,?,?,?,?,?)',('result',task['id'],'hyp','fp','final',db.dump({'secret_final_metric':'DO_NOT_LEAK'}),'complete',db.now()))
    with db.connect() as c:
        c.execute('INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?)',('dev',task['id'],'hyp','devfp','development',db.dump({'formula':'revenue','ic':{'60':{'mean':.1}}}),'complete',db.now()))
    text=db.dump(research_memory(task['id'],'2022-12-31'))
    assert 'DO_NOT_LEAK' not in text
    assert 'revenue' in text


def test_repeated_approval_is_idempotent(tiny_data):
    t=create_task(TaskConfig(name='test',dataset_id='tiny'))
    d=Decision(gate=1,action='approve',expected_version=t['version'],request_id='same-request-123')
    assert decide(t['id'],d)['status']=='queued'
    assert decide(t['id'],d)['status']=='already_applied'
    assert len(db.rows('SELECT * FROM jobs'))==1
    assert len(db.rows('SELECT * FROM decisions'))==1


def test_stale_approval_rejected(tiny_data):
    t=create_task(TaskConfig(name='test',dataset_id='tiny'))
    c=TestClient(app)
    response=c.post(f"/api/tasks/{t['id']}/decisions",json=dict(gate=1,action='approve',expected_version=999,request_id='stale-request'))
    assert response.status_code==409
    assert not db.rows('SELECT * FROM jobs')


def test_cross_site_mutation_rejected(tiny_data):
    c=TestClient(app)
    response=c.post('/api/tasks',json={'name':'external','dataset_id':'tiny'},headers={'Origin':'https://malicious.example'})
    assert response.status_code==403


def test_export_keeps_failed_experiments(tiny_data):
    t=create_task(TaskConfig(name='test',dataset_id='tiny'))
    with db.connect() as c:
        c.execute('INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?)',('bad',t['id'],'hyp','fp','development',db.dump({'error':'missing data','formula':'revenue'}),'failed',db.now()))
    c=TestClient(app)
    report=c.get(f"/api/tasks/{t['id']}/export").json()
    assert report['experiments'][0]['status']=='failed'
    assert 'missing data' in c.get(f"/api/tasks/{t['id']}/export?format=markdown").text


def test_artifact_versions_are_append_only(tiny_data):
    t=create_task(TaskConfig(name='test',dataset_id='tiny'))
    a=db.artifact(t['id'],'hypothesis','value','proposal',{'formula':'revenue'})
    b=db.artifact(t['id'],'hypothesis','value','proposal',{'formula':'cfo'})
    assert a==b
    assert db.get_artifacts(t['id'])[0]['payload']['formula']=='revenue'
