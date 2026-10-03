"""Predeclared quality checks and deterministic greedy diversification."""
from . import db
from .schemas import TaskConfig
from .feedback import hypothesis, definition_hash, build_feedback, assess_quality
from .research_types import SimilarityReport, SelectionResult
from .similarity import (freeze_reference_snapshot, read_snapshot, assess_similarity, publish_research_references,
                         load_research_signals, compare_signal_pair, is_high_similarity)


def select_candidates(task_id:str,hypothesis_ids:list[str])->SelectionResult:
    cfg=TaskConfig.model_validate(db.task(task_id)['config'])
    sid=freeze_reference_snapshot(task_id)
    snapshot=read_snapshot(sid)
    definitions={}
    for hid in hypothesis_ids:
        h=hypothesis(task_id,hid)
        key=definition_hash(h['payload'])
        definitions.setdefault(key,[]).append(hid)
    decisions=[]
    qualified=[]
    feedbacks={}
    for key,ids in sorted(definitions.items()):
        hid=ids[0]
        f=build_feedback(task_id,hid,0,SimilarityReport())
        decision=assess_quality(f,cfg.research_policy)
        decision.hypothesis_ids=ids
        decisions.append(decision)
        feedbacks[hid]=f
        if decision.quality_status=='passed':qualified.append(hid)
    if qualified:publish_research_references(task_id,qualified)
    eligible=[]
    decision_by_id={d.hypothesis_ids[0]:d for d in decisions}
    for hid in qualified:
        d=decision_by_id[hid]
        # -1 excludes same-batch peers here; greedy selection handles them below.
        similarity=assess_similarity(task_id,hid,-1,sid)
        db.artifact(task_id,'similarity_report','system','selection:'+hid,similarity.model_dump(),hid)
        pairs=[p for p in similarity.pairs if p.relationship in ('historical','baseline')]
        high=[p for p in pairs if is_high_similarity(p.model_dump(),cfg.research_policy.similarity)]
        if high:
            high.sort(key=lambda p:(-p.mean_abs_correlation,p.reference_id,p.split))
            d.selection_status='similarity_rejected';d.covered_by=high[0].reference_id
            d.reasons.append({'rule':'reference_similarity','passed':False,'reference_id':d.covered_by,'actual':high[0].mean_abs_correlation,
                              'threshold':cfg.research_policy.similarity.similarity_threshold,'split':high[0].split})
        elif not pairs or any(p.status!='complete' for p in pairs):
            d.selection_status='insufficient'
            d.reasons.append({'rule':'reference_similarity','passed':False,'reason':'与必要研究参考的比较样本不足'})
        else:eligible.append(hid)
    eligible.sort(key=lambda hid:(-feedbacks[hid].validation.metrics['ic']['60']['mean'],
                                  -feedbacks[hid].development.metrics['ic']['60']['mean'],decision_by_id[hid].definition_id))
    selected=[]
    for hid in eligible:
        d=decision_by_id[hid]
        for chosen in selected:
            for split in ('development','validation'):
                pair=compare_signal_pair(load_research_signals(task_id,hypothesis(task_id,hid)['payload'],split),
                    load_research_signals(task_id,hypothesis(task_id,chosen)['payload'],split),cfg.research_policy.similarity,cfg.min_cross_section)
                d.reasons.append({'rule':'batch_similarity','reference_id':chosen,'split':split,**pair,
                                  'threshold':cfg.research_policy.similarity.similarity_threshold})
                if pair['status']!='complete':d.selection_status='insufficient'
                elif is_high_similarity(pair,cfg.research_policy.similarity):
                    d.selection_status='similarity_rejected';d.covered_by=chosen
                if d.selection_status!='pending':break
            if d.selection_status!='pending':break
        if d.selection_status=='pending':
            d.selection_status='selected';selected.append(hid)
    result=SelectionResult(decisions=decisions,selected_ids=selected,
        policy_hash=db.digest(cfg.research_policy.model_dump()),snapshot_hash=snapshot['snapshot_hash'],
        stop_reason='' if selected else '没有候选同时通过预设研究门槛和相似性检查；全部版本与失败记录已保留')
    db.artifact(task_id,'candidate_selection','system','selection',result.model_dump())
    return result
