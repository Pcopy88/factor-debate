"""Deterministic, allowlisted evidence for researchers; never opens final.sqlite."""
import json
import math
from . import db
from .data import manifest
from .factors import canonical
from .evaluation import finite
from .schemas import TaskConfig
from .research_types import ResearchFeedback, SplitFeedback, SimilarityReport, CandidateDecision, ResearchPolicy

SPLITS=('development','validation')
IC_FIELDS=('mean','n','icir','positive_ratio','t','p_adjusted','label_coverage','missing_labels')
PORTFOLIO_FIELDS=('annual_return','total_return','max_drawdown','annual_turnover','cost_cny','blocked_orders','sharpe')


def definition_hash(h):
    return db.digest({'formula':canonical(h['formula']),'direction':h.get('direction',1)})


def hypothesis(task_id,hid):
    row=db.one("SELECT * FROM artifacts WHERE task_id=? AND id=? AND kind='hypothesis'",(task_id,hid))
    if not row: raise ValueError('假设版本不属于当前任务')
    row['payload']=json.loads(row['payload'])
    return row


def period_for(cfg,split):
    if split not in SPLITS: raise ValueError('研究只允许开发与验证分区')
    prefix='develop' if split=='development' else 'validation'
    return [str(getattr(cfg,prefix+'_start')),str(getattr(cfg,prefix+'_end'))]


def numeric(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def pick(source,fields):
    return {k:source.get(k) if numeric(source.get(k)) else None for k in fields}


def missing_metrics(value,path=''):
    if isinstance(value,dict):
        return [reason for key,item in value.items() for reason in missing_metrics(item,f'{path}.{key}' if path else key)]
    if isinstance(value,list):
        return [reason for index,item in enumerate(value) for reason in missing_metrics(item,f'{path}.{index}')]
    if value is not None:return []
    reason=('有效月份少于 2、标准差为零或原实验未提供该统计量' if path.endswith('.icir') else
            '样本不足、统计量无法计算或原实验未提供' if path.endswith(('.t','.p_adjusted')) else
            '原实验缺失或数值不是有限值')
    return [f'{path} 缺失：{reason}']


def split_feedback(task_id,hid,split,cfg):
    row=db.one('SELECT e.* FROM evaluation_bindings b JOIN experiments e ON e.id=b.experiment_id '
               'WHERE b.task_id=? AND b.hypothesis_id=? AND b.split=? AND e.task_id=? AND e.split=?',
               (task_id,hid,split,task_id,split))
    period=period_for(cfg,split)
    if not row: return SplitFeedback(execution_status='not_evaluated',period=period,missing_reasons=['尚无绑定实验'])
    p=json.loads(row['payload'])
    if row['status']!='complete':
        return SplitFeedback(experiment_id=row['id'],execution_status='failed',period=period,missing_reasons=[p.get('error','计算失败')])
    if p.get('period')!=period or p.get('dataset_version')!=manifest(cfg.dataset_id)['version']:
        raise ValueError('实验区间或数据版本与任务不一致')
    metrics={k:pick(p.get(k,{}),PORTFOLIO_FIELDS) for k in ('portfolio','benchmark','double_cost','double_cost_benchmark','quarterly')}
    metrics.update(pick(p,('coverage','signals','planned_signals','signal_month_ratio','annual_excess','double_cost_annual_excess')))
    metrics['ic']={str(h):pick(p.get('ic',{}).get(str(h),{}),IC_FIELDS) for h in cfg.horizons}
    metrics['controlled_ic']=pick(p.get('controlled_ic',{}),('mean','n','t','p_adjusted','icir'))
    metrics['baseline_comparison']=[{'name':x.get('name'),**pick(x,('rank_correlation','months'))} for x in p.get('baseline_comparison',[])]
    metrics['industries']=[{'name':x.get('name'),**pick(x,('rank_ic','months'))} for x in p.get('industries',[])]
    metrics['groups']=[pick(x,('group','mean_forward_return')) for x in p.get('groups',[])]
    yearly=p.get('portfolio',{}).get('yearly',{})
    benchmark=p.get('benchmark',{}).get('yearly',{})
    years=[str(y) for y in range(int(period[0][:4]),int(period[1][:4])+1)]
    for key in ('portfolio','benchmark','double_cost','double_cost_benchmark','quarterly'):
        annual=p.get(key,{}).get('yearly',{})
        metrics[key]['yearly']={y:annual[y] if numeric(annual.get(y)) else None for y in years}
    metrics['yearly_excess']={y:finite(yearly[y]-benchmark[y]) if numeric(yearly.get(y)) and numeric(benchmark.get(y)) else None for y in years}
    metrics['positive_excess_years']=sum(v>0 for v in metrics['yearly_excess'].values() if v is not None)
    metrics['observed_years']=sum(v is not None for v in metrics['yearly_excess'].values())
    missing=missing_metrics(metrics)
    return SplitFeedback(experiment_id=row['id'],execution_status='complete',period=period,metrics=metrics,
                         missing_reasons=missing,dataset_version=p.get('dataset_version'),engine_signature=p.get('engine_signature'))


def build_feedback(task_id: str,hypothesis_id: str,round_no: int,similarity: SimilarityReport)->ResearchFeedback:
    cfg=TaskConfig.model_validate(db.task(task_id)['config'])
    h=hypothesis(task_id,hypothesis_id)
    lineages=db.get_artifacts(task_id,'lineage')
    lineage=next((x['payload']['lineage_id'] for x in reversed(lineages) if x['payload']['head_id']==hypothesis_id),'')
    result=ResearchFeedback(task_id=task_id,hypothesis_id=hypothesis_id,lineage_id=lineage,round_no=round_no,
        parent_id=h['parent_id'],formula=h['payload']['formula'],direction=h['payload'].get('direction',1),
        policy_hash=db.digest(cfg.research_policy.model_dump()),
        development=split_feedback(task_id,hypothesis_id,'development',cfg),
        validation=split_feedback(task_id,hypothesis_id,'validation',cfg),similarity=similarity)
    parent=next((x['payload'] for x in reversed(db.get_artifacts(task_id,'research_feedback')) if x['payload']['hypothesis_id']==h['parent_id']),None)
    result.changes=compare_feedback(ResearchFeedback.model_validate(parent),result) if parent else {'status':'initial_version'}
    result.quality=assess_quality(result,cfg.research_policy).model_dump()
    return result


def compare_feedback(parent:ResearchFeedback,child:ResearchFeedback)->dict:
    result={}
    def diff(a,b):
        if isinstance(a,dict) and isinstance(b,dict):return {k:diff(a.get(k),v) for k,v in b.items()}
        if numeric(a) and numeric(b):return {'before':a,'after':b,'delta':finite(b-a)}
        return {'before':a,'after':b,'delta':None}
    for split in SPLITS:
        a,b=getattr(parent,split),getattr(child,split)
        if a.execution_status!='complete' or b.execution_status!='complete' or not a.engine_signature or (a.dataset_version,a.engine_signature,a.period)!=(b.dataset_version,b.engine_signature,b.period):
            result[split]={'status':'incomparable','reason':'分区、数据、引擎不同或实验未完成'}
        else:result[split]={'status':'comparable','metrics':diff(a.metrics,b.metrics)}
    return result


def assess_quality(feedback:ResearchFeedback,policy:ResearchPolicy)->CandidateDecision:
    reasons=[]
    failed=False
    insufficient=False
    execution='complete'
    for split in SPLITS:
        s=getattr(feedback,split)
        if s.execution_status!='complete':
            execution=s.execution_status
            insufficient=True
            reasons.append({'split':split,'rule':'execution','passed':False,'reason':'; '.join(s.missing_reasons)})
            continue
        ic=s.metrics.get('ic',{}).get('60',{})
        tests=[('ic.60.n',ic.get('n'),policy.min_ic_months,False,True),
               ('coverage',s.metrics.get('coverage'),policy.min_factor_coverage,False,False),
               ('ic.60.label_coverage',ic.get('label_coverage'),policy.min_label_coverage,False,False),
               ('ic.60.mean',ic.get('mean'),policy.min_rank_ic,True,False),
               ('annual_excess',s.metrics.get('annual_excess'),policy.min_annual_excess,True,False)]
        for path,value,threshold,strict,sample in tests:
            ok=numeric(value) and (value>threshold if strict else value>=threshold)
            reasons.append({'split':split,'rule':path,'actual':value,'threshold':threshold,'comparison':'>' if strict else '>=','passed':ok})
            if not ok:
                if not numeric(value) or sample:insufficient=True
                else:failed=True
    quality='insufficient' if insufficient else 'failed' if failed else 'passed'
    return CandidateDecision(definition_id=definition_hash({'formula':feedback.formula,'direction':feedback.direction}),
        hypothesis_ids=[feedback.hypothesis_id],execution_status=execution,quality_status=quality,
        selection_status='pending' if quality=='passed' else 'quality_rejected' if quality=='failed' else 'insufficient',reasons=reasons)
