import json
from fastapi.testclient import TestClient
from research import db
from research.api import app
from conftest import store_research_result


def test_research_summary_preserves_metrics_and_reasons(research_task):
    tid,hid=research_task
    db.artifact(tid,'candidate_selection','system','selection',{'decisions':[{'hypothesis_ids':[hid],
               'selection_status':'quality_rejected','reasons':[{'reason':'扣费超额收益为负'}]}],'selected_ids':[],'stop_reason':'没有合格候选'})
    with db.connect(final=True) as c:
        c.execute('INSERT INTO results VALUES(?,?,?,?,?,?,?,?)',('final',tid,hid,'f','final',db.dump({'secret':'FINAL_SECRET'}),'complete',db.now()))
    response=TestClient(app).get(f'/api/tasks/{tid}/research-summary')
    assert response.status_code==200
    assert '扣费超额收益为负' in response.text and 'FINAL_SECRET' not in response.text
    assert response.json()['workflow_version']==2


def test_exports_include_failed_and_reused_versions(research_task):
    tid,hid=research_task
    store_research_result(tid,hid)
    db.bind_evaluation(tid,'alias','development',db.one('SELECT id FROM experiments')['id'])
    db.artifact(tid,'empirical_critique','growth','r1',{'target_id':hid,'issue':'成本后是否仍然有效','metric_refs':[]},hid)
    db.artifact(tid,'empirical_revision','growth','r1',{'parent_id':hid,'rationale':'现金流约束','expected_effects':['降低重复程度']},hid)
    db.artifact(tid,'candidate_selection','system','selection',{'decisions':[{'hypothesis_ids':[hid],
               'selection_status':'insufficient','reasons':[{'reason':'缺少验证区实验'}]}],'selected_ids':[]})
    client=TestClient(app)
    text=client.get(f'/api/tasks/{tid}/export?format=markdown').text
    assert '成本后是否仍然有效' in text and '现金流约束' in text and '缺少验证区实验' in text
    assert 'alias' in text
    data=client.get(f'/api/tasks/{tid}/export').json()
    assert len(data['evaluation_bindings'])==2


def test_legacy_research_summary_is_readable(research_task):
    tid,_=research_task
    cfg=db.task(tid)['config'];cfg.pop('workflow_version')
    with db.connect() as c:c.execute('UPDATE tasks SET config=? WHERE id=?',(db.dump(cfg),tid))
    response=TestClient(app).get(f'/api/tasks/{tid}/research-summary')
    assert response.status_code==200 and response.json()['legacy'] is True
