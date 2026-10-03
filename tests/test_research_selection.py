from importlib.util import find_spec
import json
import pandas as pd
import pytest
from research import db
from research.research_types import SimilarityReport,PairSimilarity
from conftest import store_research_result


def selection_module():
    assert find_spec('research.selection'),'fixed selection module is missing'
    from research import selection
    return selection


def test_completed_calculation_is_not_automatically_selected(research_task):
    mod=selection_module();tid,hid=research_task
    for split in ['development','validation']:
        store_research_result(tid,hid,split,annual_excess=-.01)
    result=mod.select_candidates(tid,[hid])
    assert result.selected_ids==[]
    assert result.decisions[0].execution_status=='complete'
    assert result.decisions[0].selection_status=='quality_rejected'


def test_selection_is_deterministic_and_records_covering_factor(research_task,monkeypatch):
    mod=selection_module();tid,hid=research_task
    base=json.loads(db.one('SELECT payload FROM artifacts WHERE id=?',(hid,))['payload'])
    other=db.artifact(tid,'hypothesis','growth','proposal:1',{**base,'formula':'rank(ttm(cfo) / total_assets)'})
    for h in [hid,other]:
        for split in ['development','validation']:store_research_result(tid,h,split)
    db.artifact(tid,'approved_batch','system','approved',{'policy_hash':db.digest(db.task(tid)['config']['research_policy'])})
    def similarity(tid,hid,*a):
        return SimilarityReport(pairs=[PairSimilarity(target_id=hid,reference_id='baseline:test',relationship='baseline',split=s,
             status='complete',mean_abs_correlation=.2,mean_correlation=.2,valid_months=24,planned_months=24) for s in ['development','validation']])
    monkeypatch.setattr(mod,'assess_similarity',similarity)
    panel=pd.DataFrame([{'signal_date':d,'symbol':str(i),'value':float(i),'eligible':True}
          for d in pd.date_range('2018-01-31',periods=24,freq='ME') for i in range(20)])
    monkeypatch.setattr(mod,'load_research_signals',lambda *a:panel)
    a=mod.select_candidates(tid,[hid,other]);b=mod.select_candidates(tid,[other,hid])
    assert len(a.selected_ids)==1 and a.selected_ids==b.selected_ids
    rejected=next(x for x in a.decisions if x.selection_status=='similarity_rejected')
    assert rejected.covered_by==a.selected_ids[0]
    assert len(db.get_artifacts(tid,'hypothesis'))==2


def test_all_versions_remain_visible_after_selection(research_task):
    mod=selection_module();tid,hid=research_task
    base=json.loads(db.one('SELECT payload FROM artifacts WHERE id=?',(hid,))['payload'])
    untested=db.artifact(tid,'hypothesis','growth','empirical:1',{**base,'formula':'revenue / total_assets'},hid)
    for split in ['development','validation']:store_research_result(tid,hid,split,annual_excess=-.01)
    result=mod.select_candidates(tid,[hid,untested])
    assert set(h for d in result.decisions for h in d.hypothesis_ids)=={hid,untested}
    assert any(d.execution_status=='not_evaluated' for d in result.decisions)


def test_no_qualified_candidate_finishes_without_opening_holdout(research_task,monkeypatch):
    from research import workflow as w
    from research import research_loop
    tid,hid=research_task
    for split in ['development','validation']:store_research_result(tid,hid,split,annual_excess=-.01)
    monkeypatch.setattr(research_loop,'run_empirical_loop',lambda *a:[hid])
    monkeypatch.setattr(w,'run_cached',lambda *a,**k:{'status':'complete'})
    state=w.experiments({'task_id':tid,'candidates':[hid]})
    assert state['candidates']==[]
    w.final_test({'task_id':tid,**state})
    assert db.task(tid)['final_status']=='no_candidates'
    assert not db.rows('SELECT * FROM windows',final=True)


def test_qualified_v2_graph_reaches_gate_ten_and_preserves_independent_memory(research_task,monkeypatch):
    # Explicit numerical stubs exercise the positive admission branch; these
    # fabricated values are not backtest evidence or changes to default gates.
    from research import workflow as w,research_loop,selection
    from research.api import create_task
    from research.schemas import TaskConfig
    from research.evaluation import Evaluator,engine_signature
    from research.data import DataStore
    from research.literature import research_memory
    source_tid,source_hid=research_task
    eid=store_research_result(source_tid,source_hid)
    template=json.loads(db.one('SELECT payload FROM experiments WHERE id=?',(eid,))['payload'])
    def evaluate(self,h,start,end,minimum_signals=12):
        return {**template,'formula':h['formula'],'period':[str(start),str(end)],'engine_signature':engine_signature()}
    monkeypatch.setattr(Evaluator,'run',evaluate)
    monkeypatch.setattr(DataStore,'audit',lambda *a:{'passed':True,'errors':[]})
    def similarity(tid,hid,*a):
        return SimilarityReport(pairs=[PairSimilarity(target_id=hid,reference_id='baseline:fixture',relationship='baseline',split=s,
            status='complete',mean_abs_correlation=.2,valid_months=24,planned_months=24) for s in ['development','validation']])
    monkeypatch.setattr(research_loop,'assess_similarity',similarity)
    monkeypatch.setattr(selection,'assess_similarity',similarity)
    panel=pd.DataFrame([{'signal_date':d,'symbol':str(i),'value':float(i),'eligible':True}
         for d in pd.date_range('2018-01-31',periods=24,freq='ME') for i in range(20)])
    monkeypatch.setattr(selection,'load_research_signals',lambda *a:panel)
    tid=create_task(TaskConfig(name='controlled admission fixture',dataset_id='tiny',proposals_per_agent=1,debate_rounds=0,revision_rounds=0))['id']
    w.run_workflow(tid,{'gate':1,'action':'approve','request_id':'fixture-intake'})
    assert db.task(tid)['gate']==5
    w.run_workflow(tid,{'gate':5,'action':'approve','request_id':'fixture-batch'})
    assert db.task(tid)['gate']==10
    selected=db.get_artifacts(tid,'candidate_selection')[0]['payload']['selected_ids']
    assert len(selected)==1
    before=research_memory(tid,'2022-12-31')
    w.run_workflow(tid,{'gate':10,'action':'observe','request_id':'fixture-admission','reason':'FINAL_DECISION_SENTINEL'})
    assert db.task(tid)['status']=='completed'
    assert len(db.rows('SELECT * FROM factors WHERE task_id=?',(tid,)))==1
    assert research_memory(tid,'2022-12-31')==before
    assert 'FINAL_DECISION_SENTINEL' not in db.dump(before)
