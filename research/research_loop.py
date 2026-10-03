"""Bounded empirical debate with immutable evidence and durable action receipts."""
import json
from pathlib import Path
from . import db
from .schemas import TaskConfig, AgentOutput, Hypothesis
from .research_types import LineageState, RoundManifest, ResearchFeedback
from .agents import run_agent
from .data import DataStore
from .evaluation import Evaluator, run_cached, engine_signature
from .factors import canonical, source_code
from .feedback import hypothesis, definition_hash, period_for, build_feedback
from .similarity import freeze_reference_snapshot, read_snapshot, assess_similarity, prompt_similarity


def research_sources():
    names=('research_types.py','schemas.py','feedback.py','similarity.py','research_loop.py','selection.py',
           'agents.py','models.py','literature.py','workflow.py','db.py')
    return {n:Path(__file__).with_name(n).read_text(encoding='utf-8') for n in names if Path(__file__).with_name(n).exists()}


def check_running(task_id):
    if db.task(task_id)['paused']:
        from .workflow import Paused
        raise Paused('任务已暂停，将从保存的实证动作继续')


def saved_step(task_id,key,inputs,work):
    check_running(task_id)
    digest=db.digest(inputs)
    with db.connect() as c:
        old=c.execute('SELECT * FROM research_steps WHERE task_id=? AND step_key=?',(task_id,key)).fetchone()
        if old:
            if old['input_hash']!=digest:raise ValueError('已保存步骤的输入发生变化，停止复用')
            if old['status']=='complete':return json.loads(old['payload'])
        else:
            c.execute('INSERT INTO research_steps VALUES(?,?,?,?,?,?,?,?)',(task_id,key,digest,'running','{}','',db.now(),db.now()))
    try:
        result=work()
        with db.connect() as c:
            c.execute("UPDATE research_steps SET status='complete',payload=?,error='',updated_at=? WHERE task_id=? AND step_key=?",
                      (db.dump(result),db.now(),task_id,key))
        return result
    except Exception as exc:
        with db.connect() as c:
            c.execute("UPDATE research_steps SET error=?,updated_at=? WHERE task_id=? AND step_key=?",(str(exc)[:1200],db.now(),task_id,key))
        raise


def lineage_states(task_id):
    states={}
    for row in db.get_artifacts(task_id,'lineage'):
        p=LineageState.model_validate(row['payload'])
        states[p.lineage_id]=p
    return states


def save_lineage(task_id,state,stage):
    return db.artifact(task_id,'lineage','system',stage,state.model_dump())


def register_definition(task_id,hid,cfg):
    h=hypothesis(task_id,hid)['payload']
    key='v2:definition:r0:system:'+definition_hash(h)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT payload FROM research_steps WHERE task_id=? AND step_key=?',(task_id,key)).fetchone()
        if old:return json.loads(old[0])['hypothesis_id']
        count=c.execute("SELECT COUNT(*) FROM research_steps WHERE task_id=? AND step_key LIKE 'v2:definition:%'",(task_id,)).fetchone()[0]
        if count>=cfg.max_experiments:return None
        c.execute('INSERT INTO research_steps VALUES(?,?,?,?,?,?,?,?)',
                  (task_id,key,definition_hash(h),'complete',db.dump({'hypothesis_id':hid}),'',db.now(),db.now()))
    return hid


def evaluate_round(task_id:str,round_no:int,lineage_ids:list[str])->str:
    cfg=TaskConfig.model_validate(db.task(task_id)['config'])
    sid=freeze_reference_snapshot(task_id)
    old=next((a for a in db.get_artifacts(task_id,'round_manifest') if a['payload']['round_no']==round_no),None)
    if old:return old['id']
    states=lineage_states(task_id)
    round_input=next((a['payload'] for a in db.get_artifacts(task_id,'round_input') if a['payload']['round_no']==round_no),None)
    if round_input is None:
        round_input={'round_no':round_no,'lineage_ids':lineage_ids,'heads':[states[i].head_id for i in lineage_ids]}
        db.artifact(task_id,'round_input','system',f'round:{round_no}',round_input)
    lineage_ids=round_input['lineage_ids']
    heads=round_input['heads']
    if heads!=[states[i].head_id for i in lineage_ids]:raise ValueError('已冻结轮次的父版本发生变化')
    ev=Evaluator(DataStore(cfg.dataset_id,allowed_end=cfg.validation_end),cfg)
    for lid in lineage_ids:
        check_running(task_id)
        state=states[lid]
        hid=state.head_id
        h=hypothesis(task_id,hid)['payload']
        source=register_definition(task_id,hid,cfg)
        if source is None:
            state.active=False;state.stop_reason='实验定义额度已用尽'
            save_lineage(task_id,state,f'limit:{round_no}:{lid}')
            db.event(task_id,'experiment_limit','新版本未执行：已达到实验定义上限',{'hypothesis_id':hid})
            continue
        if source!=hid:
            for row in db.rows('SELECT * FROM evaluation_bindings WHERE task_id=? AND hypothesis_id=?',(task_id,source)):
                db.bind_evaluation(task_id,hid,row['split'],row['experiment_id'])
            state.active=False;state.stop_reason='公式与已有定义相同，已复用实验'
            save_lineage(task_id,state,f'reused:{round_no}:{lid}')
            continue
        stage=f'v2:engineer:r{round_no}:{lid}:{definition_hash(h)}'
        output=AgentOutput.model_validate(saved_step(task_id,stage,{'hypothesis':h},
            lambda:run_agent(task_id,'engineer',stage,'实现检查',{'hypotheses':[h],'round':round_no}).model_dump()))
        if not output.hypotheses or definition_hash(output.hypotheses[0].model_dump())!=definition_hash(h):
            state.active=False;state.stop_reason='工程检查改变已批准的公式或方向'
            save_lineage(task_id,state,f'invalid:{round_no}:{lid}')
            db.event(task_id,'invalid',state.stop_reason,{'hypothesis_id':hid})
            continue
        for split in ('development','validation'):
            result=run_cached(task_id,hid,h,split,ev,*period_for(cfg,split))
            db.event(task_id,'experiment',f'实证第 {round_no} 轮 · {split} · {result["status"]}',{'hypothesis_id':hid,'experiment_id':result['id']})
    # All calculations precede any research model call in this round.
    feedback_ids=[]
    role_groups={}
    states=lineage_states(task_id)
    for lid in lineage_ids:
        check_running(task_id)
        state=states[lid]
        hid=state.head_id
        stage=f'r{round_no}:{hid}'
        existing=next((a for a in db.get_artifacts(task_id,'research_feedback') if a['stage']==stage),None)
        if existing:feedback_ids.append(existing['id'])
        else:
            similarity=assess_similarity(task_id,hid,round_no,sid)
            db.artifact(task_id,'similarity_report','system',stage,similarity.model_dump(),hid)
            feedback=build_feedback(task_id,hid,round_no,similarity)
            feedback_ids.append(db.artifact(task_id,'research_feedback','system',stage,feedback.model_dump(),hid))
        if state.active:
            role=({'value':'growth','growth':'value'}[state.current_author] if cfg.architecture=='debate' else state.current_author)
            role_groups.setdefault(role,[]).append(hid)
    payload=RoundManifest(task_id=task_id,round_no=round_no,hypothesis_ids=heads,snapshot_id=sid,
                          feedback_ids=feedback_ids,role_groups=role_groups,
                          input_hash=db.digest({'heads':heads,'policy':cfg.research_policy.model_dump(),'engine':engine_signature()}))
    return db.artifact(task_id,'round_manifest','system',f'round:{round_no}',payload.model_dump())


def _artifact_payload(identifier,kind):
    row=db.one('SELECT * FROM artifacts WHERE id=? AND kind=?',(identifier,kind))
    if not row:raise ValueError('研究步骤引用不存在')
    return json.loads(row['payload'])


def _target(task_id,hid,feedback,cfg):
    h=hypothesis(task_id,hid)
    ancestors=[]
    parent=h['parent_id']
    while parent:
        old=hypothesis(task_id,parent)
        ancestors.append({'id':parent,**old['payload']})
        parent=old['parent_id']
    # Current-round critique must not change the prompt when recovering this step.
    ids={x['id'] for x in ancestors}
    critiques=[{'id':a['id'],**a['payload']} for a in db.get_artifacts(task_id,'empirical_critique') if a['parent_id'] in ids]
    f=feedback.model_dump()
    f['similarity']=prompt_similarity(feedback.similarity,cfg.research_policy.similarity.top_k)
    revisions=[a['payload'] for a in db.get_artifacts(task_id,'empirical_revision') if a['parent_id'] in ids]
    return {'id':hid,**h['payload'],'source_code':source_code(h['payload']['formula']),
            'feedback':f,'history':{'ancestors':ancestors,'critiques':critiques,'revisions':revisions}}


def validate_critiques(output,targets):
    allowed={t['id']:t for t in targets}
    if set(c.target_id for c in output.critiques)!=set(allowed):raise ValueError('实证批判必须覆盖本次全部目标，不能引用其它版本')
    for c in output.critiques:
        target=allowed[c.target_id]
        f=target['feedback']
        if any(f[s]['execution_status']=='complete' for s in ('development','validation')) and not c.metric_refs:
            raise ValueError('实证批判必须引用已归档实验指标')
        for ref in c.metric_refs:
            part=f[ref.split]
            if ref.experiment_id!=part['experiment_id']:raise ValueError('批判引用了未经授权的实验')
            value=part['metrics']
            for component in ref.metric_path.split('.'):
                if not isinstance(value,dict) or component not in value:raise ValueError('批判引用的指标路径不存在')
                value=value[component]


def revise_round(task_id:str,manifest_id:str)->list[str]:
    cfg=TaskConfig.model_validate(db.task(task_id)['config'])
    m=RoundManifest.model_validate(_artifact_payload(manifest_id,'round_manifest'))
    if m.task_id!=task_id:raise ValueError('轮次不属于当前任务')
    feedbacks=[ResearchFeedback.model_validate(_artifact_payload(fid,'research_feedback')) for fid in m.feedback_ids]
    fmap={f.hypothesis_id:f for f in feedbacks}
    created=[]
    for role,hids in sorted(m.role_groups.items()):
        targets=[_target(task_id,hid,fmap[hid],cfg) for hid in hids]
        round_no=m.round_no+1
        scope=db.digest(sorted(hids))
        context={'targets':targets,'round':round_no,'manifest_id':manifest_id}
        key=f'v2:critique:r{round_no}:{role}:{scope}'
        critiques=AgentOutput.model_validate(saved_step(task_id,key,context,
            lambda:run_agent(task_id,role,key,'实证批判',context).model_dump()))
        validate_critiques(critiques,targets)
        stored=[]
        for index,c in enumerate(critiques.critiques):
            cid=db.artifact(task_id,'empirical_critique',role,f'{key}:{index}',c.model_dump(),c.target_id)
            stored.append({'id':cid,**c.model_dump()})
        context={**context,'critiques':stored}
        key=f'v2:rewrite:r{round_no}:{role}:{scope}'
        output=AgentOutput.model_validate(saved_step(task_id,key,context,
            lambda:run_agent(task_id,role,key,'实证改写',context).model_dump()))
        if len(output.revisions)!=len(hids) or {r.parent_id for r in output.revisions}!=set(hids):
            raise ValueError('改写必须逐项绑定允许的父版本，不得遗漏、重复或越权')
        by_parent={r.parent_id:r for r in output.revisions}
        states=lineage_states(task_id)
        for hid in hids:
            revision=by_parent[hid]
            expected={c['id'] for c in stored if c['target_id']==hid}
            warnings={w['warning_id'] for w in fmap[hid].similarity.warnings}
            if set(revision.addressed_critique_ids)!=expected or set(revision.addressed_warning_ids)!=warnings:
                raise ValueError('改写未准确回应对应批判和相似性警告')
            if revision.action=='revise' and not revision.expected_effects:raise ValueError('改写必须给出可检验的预期结果')
            state=next((s for s in states.values() if s.head_id==hid),None)
            # A crash may have already advanced this lineage; recover it by its prior records.
            if state is None:
                old=next(a['payload'] for a in db.get_artifacts(task_id,'lineage') if a['payload']['head_id']==hid)
                state=LineageState.model_validate(old)
            db.artifact(task_id,'empirical_revision',role,f'{key}:{hid}',revision.model_dump(),hid)
            if revision.action=='revise':
                new_id=db.artifact(task_id,'hypothesis',role,f'empirical:r{round_no}:{hid}',revision.hypothesis.model_dump(),hid)
                created.append(new_id)
                state.head_id=new_id;state.current_author=role;state.round_no=round_no
                state.active=True;state.stop_reason=''
            else:
                state.active=False;state.stop_reason='保留当前定义' if revision.action=='keep' else '研究 Agent 建议停止该谱系'
            save_lineage(task_id,state,f'revision:r{round_no}:{state.lineage_id}')
    return created


def run_empirical_loop(task_id:str,approved_ids:list[str])->list[str]:
    cfg=TaskConfig.model_validate(db.task(task_id)['config'])
    sid=freeze_reference_snapshot(task_id)
    scope={'candidate_ids':approved_ids,'config_hash':db.digest(cfg.model_dump(mode='json')),
           'policy_hash':db.digest(cfg.research_policy.model_dump()),'snapshot_hash':read_snapshot(sid)['snapshot_hash'],
           'research_signature':db.digest(research_sources())}
    saved_step(task_id,'v2:approved',scope,lambda:{'artifact_id':db.artifact(task_id,'approved_batch','system','approved',scope)})
    for hid in approved_ids:
        h=hypothesis(task_id,hid)
        state=LineageState(lineage_id=hid,origin_role=h['role'],current_author=h['role'],head_id=hid)
        save_lineage(task_id,state,f'initial:{hid}')
    stop_reason='实证轮次已完成'
    for round_no in range(cfg.revision_rounds+1):
        check_running(task_id)
        old=next((a for a in db.get_artifacts(task_id,'round_manifest') if a['payload']['round_no']==round_no),None)
        if old:mid=old['id']
        else:
            states=lineage_states(task_id)
            round_input=next((a['payload'] for a in db.get_artifacts(task_id,'round_input') if a['payload']['round_no']==round_no),None)
            active=round_input['lineage_ids'] if round_input else [lid for lid in approved_ids if states[lid].active and states[lid].round_no==round_no]
            if not active:break
            mid=evaluate_round(task_id,round_no,active)
        if round_no==cfg.revision_rounds:break
        count=db.one("SELECT COUNT(*) n FROM research_steps WHERE task_id=? AND step_key LIKE 'v2:definition:%'",(task_id,))['n']
        if count>=cfg.max_experiments:
            stop_reason='实验定义额度已用尽'
            db.event(task_id,'experiment_limit','实验定义额度已用尽，停止新的实证改写')
            break
        saved_step(task_id,f'v2:revision-round:{round_no}',{'manifest_id':mid},lambda:{'ids':revise_round(task_id,mid)})
    for state in lineage_states(task_id).values():
        if state.active:
            state.active=False;state.stop_reason=stop_reason
            save_lineage(task_id,state,f'finished:{state.lineage_id}')
    roots=set(approved_ids)
    result=list(approved_ids)
    for a in db.get_artifacts(task_id,'hypothesis'):
        if a['parent_id'] in roots:
            roots.add(a['id']);result.append(a['id'])
    return list(dict.fromkeys(result))


def research_summary(task_id:str)->dict:
    t=db.task(task_id)
    artifacts=db.get_artifacts(task_id)
    kinds=('lineage','round_manifest','research_feedback','similarity_report','candidate_selection','empirical_critique','empirical_revision','approved_batch')
    out={k:[{'id':a['id'],'role':a['role'],'parent_id':a['parent_id'],'payload':a['payload']} for a in artifacts if a['kind']==k] for k in kinds}
    snaps=[a for a in artifacts if a['kind']=='reference_snapshot']
    return {'workflow_version':t['config'].get('workflow_version',1),'legacy':t['config'].get('workflow_version',1)!=2,
            'status':t['status'],'error':t['error'],'definition_count':db.one("SELECT COUNT(*) n FROM research_steps WHERE task_id=? AND step_key LIKE 'v2:definition:%'",(task_id,))['n'],
            'reference_count':len(snaps[0]['payload']['members']) if snaps else 0,
            'reference_cutoff':snaps[0]['payload']['cutoff'] if snaps else None,
            'lineages':[s.model_dump() for s in lineage_states(task_id).values()],**out}
