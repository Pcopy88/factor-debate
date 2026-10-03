from importlib.util import find_spec
import json
from types import SimpleNamespace
import pytest
from research import db
from research.schemas import TaskConfig,AgentOutput,Hypothesis,Critique,EmpiricalRevision
from research.research_types import SimilarityReport,MetricReference
from conftest import store_research_result


def loop_module():
    assert find_spec('research.research_loop'), 'empirical loop is missing'
    from research import research_loop
    return research_loop


@pytest.fixture
def loop_setup(research_task,monkeypatch):
    mod=loop_module()
    tid,hid=research_task
    def evaluate(tid,hid,h,split,ev,start,end,final=False):
        binding=db.one('SELECT experiment_id FROM evaluation_bindings WHERE task_id=? AND hypothesis_id=? AND split=?',(tid,hid,split))
        eid=binding['experiment_id'] if binding else store_research_result(tid,hid,split,formula=h['formula'])
        row=db.one('SELECT * FROM experiments WHERE id=?',(eid,))
        row['payload']=json.loads(row['payload'])
        return row
    monkeypatch.setattr(mod,'run_cached',evaluate)
    monkeypatch.setattr(mod,'assess_similarity',lambda *a:SimilarityReport(snapshot_hash='fixture'))
    calls=[]
    def agent(task_id,role,stage,action,context):
        calls.append((role,action,context))
        if action=='实现检查':return AgentOutput(hypotheses=[Hypothesis.model_validate(context['hypotheses'][0])])
        targets=context['targets']
        if action=='实证批判':
            return AgentOutput(critiques=[Critique(target_id=t['id'],issue='检验现金收益与静态估值的区别',
                proposed_test='通过标准化检查异常值和行业影响',metric_refs=[MetricReference(experiment_id=t['feedback']['validation']['experiment_id'],
                 split='validation',metric_path='ic.60.mean')]) for t in targets])
        return AgentOutput(revisions=[EmpiricalRevision(parent_id=t['id'],action='revise',
            hypothesis=Hypothesis.model_validate({**{k:v for k,v in t.items() if k in Hypothesis.model_fields},'formula':f"rank({t['formula']})"}),
            addressed_critique_ids=[c['id'] for c in context['critiques'] if c['target_id']==t['id']],
            rationale='用截面排序检验重尾值是否影响结果',expected_effects=['检查排序后的稳定性']) for t in reversed(targets)])
    monkeypatch.setattr(mod,'run_agent',agent)
    return mod,tid,hid,calls


def test_roles_alternate_on_the_same_lineage(loop_setup):
    mod,tid,hid,calls=loop_setup
    ids=mod.run_empirical_loop(tid,[hid])
    versions=[mod.hypothesis(tid,i) for i in ids]
    assert [v['role'] for v in versions]==['value','growth','value']
    assert versions[1]['parent_id']==hid and versions[2]['parent_id']==versions[1]['id']
    assert len(db.get_artifacts(tid,'research_feedback'))==3
    state=mod.lineage_states(tid)[hid]
    assert not state.active and '轮次' in state.stop_reason


def test_round_uses_fixed_evidence_before_any_revision(loop_setup):
    mod,tid,hid,calls=loop_setup
    mod.run_empirical_loop(tid,[hid])
    for _,action,ctx in calls:
        if action in ('实证批判','实证改写'):
            assert ctx['targets'][0]['feedback']['validation']['experiment_id']
            assert ctx['targets'][0]['source_code']
    assert [action for _,action,_ in calls]==['实现检查','实证批判','实证改写','实现检查','实证批判','实证改写','实现检查']


def test_reordered_model_output_keeps_correct_parents(loop_setup):
    mod,tid,hid,calls=loop_setup
    original=mod.hypothesis(tid,hid)['payload']
    other=db.artifact(tid,'hypothesis','value','proposal:1',{**original,'formula':'revenue / total_assets'})
    ids=mod.run_empirical_loop(tid,[hid,other])
    for i in ids[2:]:
        child=mod.hypothesis(tid,i)
        parent=mod.hypothesis(tid,child['parent_id'])
        assert child['payload']['formula']==f"rank({parent['payload']['formula']})"


def test_critique_and_revision_cite_real_evidence(loop_setup,monkeypatch):
    mod,tid,hid,calls=loop_setup
    original=mod.run_agent
    def forged(*args):
        output=original(*args)
        if output.critiques:output.critiques[0].metric_refs[0].experiment_id='a-final-or-other-experiment'
        return output
    monkeypatch.setattr(mod,'run_agent',forged)
    with pytest.raises(ValueError,match='引用|实验'):mod.run_empirical_loop(tid,[hid])
    assert len(db.get_artifacts(tid,'hypothesis'))==1


@pytest.mark.parametrize('action',['keep','drop','unchanged'])
def test_keep_drop_and_unchanged_formula_stop_the_lineage(loop_setup,monkeypatch,action):
    mod,tid,hid,calls=loop_setup
    original=mod.run_agent
    def decide(*args):
        output=original(*args)
        for revision in output.revisions:
            if action=='unchanged':revision.hypothesis.formula=mod.hypothesis(tid,hid)['payload']['formula']
            else:revision.action=action;revision.hypothesis=None
        return output
    monkeypatch.setattr(mod,'run_agent',decide)
    mod.run_empirical_loop(tid,[hid])
    assert len(db.rows("SELECT * FROM research_steps WHERE task_id=? AND step_key LIKE 'v2:definition:%'",(tid,)))==1
    assert len([x for x in calls if x[1]=='实证改写'])==1


@pytest.mark.parametrize('architecture',['independent','single'])
def test_independent_mode_does_not_receive_peer_lineages(loop_setup,architecture):
    mod,tid,hid,calls=loop_setup
    cfg=db.task(tid)['config'];cfg['architecture']=architecture
    with db.connect() as c:c.execute('UPDATE tasks SET config=? WHERE id=?',(db.dump(cfg),tid))
    mod.run_empirical_loop(tid,[hid])
    assert all(role=='value' for role,action,_ in calls if action!='实现检查')


def test_resume_after_feedback_and_after_model_response(loop_setup,monkeypatch):
    mod,tid,hid,calls=loop_setup
    original=db.artifact
    fired=[]
    def crash(task_id,kind,*a,**kw):
        result=original(task_id,kind,*a,**kw)
        if kind=='empirical_critique' and not fired:
            fired.append(True);raise RuntimeError('crash after persisted critique')
        return result
    monkeypatch.setattr(db,'artifact',crash)
    with pytest.raises(RuntimeError):mod.run_empirical_loop(tid,[hid])
    monkeypatch.setattr(db,'artifact',original)
    ids=mod.run_empirical_loop(tid,[hid])
    count=len(calls)
    assert mod.run_empirical_loop(tid,[hid])==ids and len(calls)==count
    assert len([x for x in calls if x[1]=='实证批判'])==2


def test_twenty_definition_limit_survives_restart(loop_setup):
    mod,tid,hid,calls=loop_setup
    cfg=db.task(tid)['config'];cfg['max_experiments']=1
    with db.connect() as c:c.execute('UPDATE tasks SET config=? WHERE id=?',(db.dump(cfg),tid))
    mod.run_empirical_loop(tid,[hid]);mod.run_empirical_loop(tid,[hid])
    assert len(db.rows("SELECT * FROM research_steps WHERE task_id=? AND step_key LIKE 'v2:definition:%'",(tid,)))==1
    assert len([x for x in calls if x[1]=='实现检查'])==1
    state=mod.lineage_states(tid)[hid]
    assert not state.active and '额度' in state.stop_reason


def test_same_step_different_input_is_rejected(research_task):
    mod=loop_module();tid,_=research_task
    assert mod.saved_step(tid,'key',{'a':1},lambda:{'ok':1})=={'ok':1}
    with pytest.raises(ValueError,match='输入'):mod.saved_step(tid,'key',{'a':2},lambda:{'ok':2})


def test_twenty_first_definition_rejected_by_persisted_registry(research_task):
    mod=loop_module();tid,hid=research_task
    cfg=TaskConfig.model_validate(db.task(tid)['config'])
    h=mod.hypothesis(tid,hid)['payload']
    ids=[db.artifact(tid,'hypothesis','value',f'quota:{i}',{**h,'formula':f'market_cap + {i}'}) for i in range(21)]
    for i in ids[:20]:assert mod.register_definition(tid,i,cfg)==i
    cfg=TaskConfig.model_validate(db.task(tid)['config'])
    assert mod.register_definition(tid,ids[20],cfg) is None
    assert mod.register_definition(tid,ids[0],cfg)==ids[0]
    assert db.one("SELECT COUNT(*) n FROM research_steps WHERE task_id=? AND step_key LIKE 'v2:definition:%'",(tid,))['n']==20


def test_resume_keeps_feedback_for_already_stopped_round_member(loop_setup,monkeypatch):
    mod,tid,hid,calls=loop_setup
    alias=db.artifact(tid,'hypothesis','value','duplicate:0',mod.hypothesis(tid,hid)['payload'])
    original=mod.save_lineage
    fired=[]
    def crash(tid,state,stage):
        result=original(tid,state,stage)
        if stage.startswith('reused:') and not fired:
            fired.append(True);raise RuntimeError('crash after stopped lineage receipt')
        return result
    monkeypatch.setattr(mod,'save_lineage',crash)
    with pytest.raises(RuntimeError):mod.run_empirical_loop(tid,[hid,alias])
    monkeypatch.setattr(mod,'save_lineage',original)
    mod.run_empirical_loop(tid,[hid,alias])
    manifest=db.get_artifacts(tid,'round_manifest')[0]['payload']
    assert set(manifest['hypothesis_ids'])=={hid,alias}
    assert any(a['payload']['hypothesis_id']==alias for a in db.get_artifacts(tid,'research_feedback'))


def test_budget_exhaustion_does_not_open_final_test(loop_setup,monkeypatch):
    mod,tid,hid,calls=loop_setup
    from research.models import BudgetExhausted
    def no_budget(*a):raise BudgetExhausted('budget')
    monkeypatch.setattr(mod,'run_agent',no_budget)
    with pytest.raises(BudgetExhausted):mod.run_empirical_loop(tid,[hid])
    assert db.task(tid)['final_status']=='unopened'
    assert not db.rows('SELECT * FROM windows',final=True)


def test_new_research_memory_does_not_read_unscoped_experiments(research_task):
    from research.literature import research_memory
    tid,hid=research_task
    store_research_result(tid,hid,formula='PEER_SECRET')
    text=db.dump(research_memory(tid,'2022-12-31'))
    assert 'PEER_SECRET' not in text


def test_demo_agent_returns_empirical_revisions_with_warning_responses(research_task):
    from research.agents import demo_output
    tid,hid=research_task
    h=json.loads(db.one('SELECT payload FROM artifacts WHERE id=?',(hid,))['payload'])
    out=demo_output('growth','实证改写',{'round':1,'targets':[{'id':hid,**h,'feedback':{'similarity':{'warning_ids':['warning']}}}],
                                         'critiques':[{'id':'critique','target_id':hid}]},db.task(tid)['config'])
    assert out.revisions and out.revisions[0].addressed_warning_ids==['warning']
