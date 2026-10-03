"""Pre-final reference snapshots and monthly cross-sectional rank comparisons."""
import json
from functools import lru_cache
import numpy as np
import pandas as pd
from . import db
from .config import path_for, home
from .schemas import TaskConfig
from .data import DataStore, manifest, file_hash
from .evaluation import Evaluator, engine_signature, finite
from .factors import BASELINES, source_code, canonical
from .feedback import hypothesis, definition_hash, period_for, build_feedback
from .research_types import SimilarityPolicy, SimilarityReport, PairSimilarity


def compatibility(cfg):
    fields=('mode','develop_start','develop_end','validation_start','validation_end','literature_cutoff',
            'min_listing_days','min_turnover_cny','min_cross_section','commission_bps','slippage_bps','horizons','research_policy')
    data=cfg.model_dump(mode='json')
    return {**{k:data[k] for k in fields},'dataset_version':manifest(cfg.dataset_id)['version'],'engine_signature':engine_signature()}


def read_snapshot(snapshot_id):
    row=db.one("SELECT * FROM artifacts WHERE id=? AND kind='reference_snapshot'",(snapshot_id,))
    if not row: raise ValueError('研究参考快照不存在')
    snap=json.loads(row['payload'])
    if db.digest({k:v for k,v in snap.items() if k!='snapshot_hash'})!=snap['snapshot_hash']:
        raise ValueError('研究参考快照校验失败')
    return snap


def freeze_reference_snapshot(task_id: str)->str:
    existing=db.get_artifacts(task_id,'reference_snapshot')
    if existing:
        read_snapshot(existing[0]['id'])
        return existing[0]['id']
    t=db.task(task_id)
    cfg=TaskConfig.model_validate(t['config'])
    group=db.get_artifacts(task_id,'comparison')
    cutoff=group[0]['payload'].get('reference_cutoff',t['created_at']) if group else t['created_at']
    contract=compatibility(cfg)
    members,excluded=[],[]
    for row in db.rows('SELECT * FROM research_reference_factors WHERE published_at<? ORDER BY reference_id',(cutoff,)):
        p=json.loads(row['payload'])
        if db.digest(p)!=row['content_hash']:raise ValueError('研究参考项内容校验失败')
        if p.get('compatibility')!=contract or p.get('provenance')!='pre-final-v2':
            excluded.append({'reference_id':row['reference_id'],'reason':'数据、股票池、分区、研究规则或来源不兼容'})
            continue
        members.append({'reference_id':row['reference_id'],**p,'content_hash':row['content_hash']})
    from .literature import research_documents
    payload={'cutoff':cutoff,'compatibility':contract,'members':members,'excluded':excluded,
             'documents':research_documents(cfg.literature_cutoff)}
    payload['snapshot_hash']=db.digest(payload)
    return db.artifact(task_id,'reference_snapshot','system','initial',payload)


def publish_research_references(task_id: str,qualified_ids:list[str])->list[str]:
    t=db.task(task_id)
    if t['config'].get('workflow_version',1)!=2 or t['final_status']!='unopened' or t['status'] in ('final_testing','completed'):
        raise ValueError('仅允许在最终测试之前发布新版研究参考')
    cfg=TaskConfig.model_validate(t['config'])
    approvals=db.get_artifacts(task_id,'approved_batch')
    if not approvals or approvals[-1]['payload'].get('policy_hash')!=db.digest(cfg.research_policy.model_dump()):
        raise ValueError('缺少与当前研究策略一致的批准记录')
    sid=freeze_reference_snapshot(task_id)
    out=[]
    for hid in qualified_ids:
        h=hypothesis(task_id,hid)
        f=build_feedback(task_id,hid,0,SimilarityReport())
        if f.quality['quality_status']!='passed':continue
        key=definition_hash(h['payload'])
        rid=db.digest({'task':task_id,'definition':key})
        p={'provenance':'pre-final-v2','hypothesis_id':hid,'source_task_id':task_id,
           'definition_hash':key,'hypothesis':h['payload'],'compatibility':compatibility(cfg),
           'reference_snapshot_hash':read_snapshot(sid)['snapshot_hash'],
           'development':f.development.model_dump(),'validation':f.validation.model_dump(),
           'policy_hash':f.policy_hash}
        with db.connect() as c:
            c.execute('INSERT OR IGNORE INTO research_reference_factors VALUES(?,?,?,?,?,?)',
                      (rid,task_id,key,db.dump(p),db.digest(p),db.now()))
        out.append(rid)
    return out


@lru_cache(maxsize=2)
def _signal_evaluator(home_key,config_json,split,signature):
    cfg=TaskConfig.model_validate_json(config_json)
    return Evaluator(DataStore(cfg.dataset_id,allowed_end=period_for(cfg,split)[1]),cfg)


def load_research_signals(task_id:str,hypothesis:dict,split:str)->pd.DataFrame:
    cfg=TaskConfig.model_validate(db.task(task_id)['config'])
    period=period_for(cfg,split)
    key=db.digest({'contract':compatibility(cfg),'period':period,'split':split,
                   'definition':definition_hash(hypothesis)})
    path=path_for('research_signals',key+'.parquet')
    meta=path.with_suffix('.json')
    if path.exists() and meta.exists():
        try:
            saved=json.loads(meta.read_text(encoding='utf-8'))
            if saved.get('hash')==file_hash(path):return pd.read_parquet(path)
        except (ValueError,OSError):pass
    ev=_signal_evaluator(str(home()),cfg.model_dump_json(),split,engine_signature())
    panel=ev.signal_panel(hypothesis['formula'],*period,hypothesis.get('direction',1))
    temp=path.with_suffix('.tmp')
    panel.to_parquet(temp,index=False)
    temp.replace(path)
    temp_meta=meta.with_suffix('.tmp.json')
    temp_meta.write_text(db.dump({'hash':file_hash(path),'split':split,'period':period}),encoding='utf-8')
    temp_meta.replace(meta)
    return panel


def compare_signal_pair(left:pd.DataFrame,right:pd.DataFrame,policy:SimilarityPolicy,min_cross_section:int)->dict:
    required={'signal_date','symbol','value','eligible'}
    if not required <= set(left) or not required <= set(right):raise ValueError('信号面板缺少必需字段')
    if left.duplicated(['signal_date','symbol']).any() or right.duplicated(['signal_date','symbol']).any():
        raise ValueError('同月同股票信号重复')
    days=sorted(set(left.signal_date)|set(right.signal_date))
    l={d:g[g.eligible].set_index('symbol').value.replace([np.inf,-np.inf],np.nan).dropna() for d,g in left.groupby('signal_date')}
    r={d:g[g.eligible].set_index('symbol').value.replace([np.inf,-np.inf],np.nan).dropna() for d,g in right.groupby('signal_date')}
    values,overlaps=[],[]
    for day in days:
        a,b=l.get(day,pd.Series(dtype=float)),r.get(day,pd.Series(dtype=float))
        shared=a.index.intersection(b.index)
        ratio=len(shared)/max(1,len(a.index.union(b.index)))
        if len(shared)<max(policy.min_pair_symbols,min_cross_section) or ratio<policy.min_overlap_ratio:continue
        a,b=a.loc[shared],b.loc[shared]
        if a.nunique()<2 or b.nunique()<2:continue
        rho=a.rank().corr(b.rank())
        if np.isfinite(rho):values.append(float(rho));overlaps.append(ratio)
    enough=len(values)>=policy.min_valid_months and len(values)/max(1,len(days))>=policy.min_month_ratio
    return {'status':'complete' if enough else 'insufficient',
            'mean_abs_correlation':finite(np.mean(np.abs(values))) if enough else None,
            'mean_correlation':finite(np.mean(values)) if enough else None,
            'valid_months':len(values),'planned_months':len(days),
            'mean_overlap':finite(np.mean(overlaps)) if overlaps else None,
            'reason':'' if enough else '共同股票、重叠比例、有效月份或方差不足'}


def is_high_similarity(pair,policy):
    return pair['status']=='complete' and pair['mean_abs_correlation'] is not None and pair['mean_abs_correlation']>=policy.similarity_threshold


def assess_similarity(task_id:str,hypothesis_id:str,round_no:int,snapshot_id:str)->SimilarityReport:
    cfg=TaskConfig.model_validate(db.task(task_id)['config'])
    h=hypothesis(task_id,hypothesis_id)
    snap=read_snapshot(snapshot_id)
    refs=[{'reference_id':'baseline:'+name,'relationship':'baseline','hypothesis':{'title':name,'formula':formula,'direction':1,
            'mechanism':'固定基础因子对照','failure_condition':'基础暴露不等于增量信息'}} for name,formula in BASELINES.items()]
    refs += [{'reference_id':m['reference_id'],'relationship':'historical','hypothesis':m['hypothesis'],
              'content_hash':m['content_hash'],'development':m['development'],'validation':m['validation']} for m in snap['members']]
    ancestor_ids=set()
    parent=h['parent_id']
    while parent:
        ancestor_ids.add(parent)
        parent=hypothesis(task_id,parent)['parent_id']
    prior_ids=set()
    for m in db.get_artifacts(task_id,'round_manifest'):
        if m['payload']['round_no']<round_no:prior_ids.update(m['payload']['hypothesis_ids'])
    for pid in sorted(prior_ids):
        old=hypothesis(task_id,pid)
        if cfg.architecture!='debate' and old['role']!=h['role']:continue
        refs.append({'reference_id':pid,'relationship':'ancestor' if pid in ancestor_ids else 'peer','hypothesis':old['payload']})
    report=SimilarityReport(snapshot_hash=snap['snapshot_hash'])
    panels={}
    for split in ('development','validation'):
        try:panels[split]=load_research_signals(task_id,h['payload'],split)
        except (ValueError,KeyError,ArithmeticError) as exc:panels[split]=str(exc)
    for ref in refs:
        ref_pairs=[]
        for split,panel in panels.items():
            try:
                if isinstance(panel,str):raise ValueError(panel)
                other=load_research_signals(task_id,ref['hypothesis'],split)
                stats=compare_signal_pair(panel,other,cfg.research_policy.similarity,cfg.min_cross_section)
            except (ValueError,KeyError,ArithmeticError) as exc:
                stats={'status':'insufficient','reason':str(exc)}
            pair=PairSimilarity(target_id=hypothesis_id,reference_id=ref['reference_id'],relationship=ref['relationship'],split=split,**stats)
            report.pairs.append(pair)
            ref_pairs.append(pair.model_dump())
            if pair.status!='complete' and ref['relationship']!='ancestor':
                report.insufficient_reasons.append(f"{ref['reference_id']} / {split}：{pair.reason}")
        summary={**ref,'source_code':source_code(ref['hypothesis']['formula']),'pairs':ref_pairs}
        summary['max_similarity']=max((p['mean_abs_correlation'] for p in ref_pairs if p['mean_abs_correlation'] is not None),default=None)
        report.references.append(summary)
        if ref['relationship']!='ancestor' and any(is_high_similarity(p,cfg.research_policy.similarity) for p in ref_pairs):
            report.warnings.append({'warning_id':db.digest({'target':hypothesis_id,'reference':ref['reference_id'],'snapshot':snap['snapshot_hash']}),
                                    'target_id':hypothesis_id,**summary})
    report.references.sort(key=lambda x:(-(x['max_similarity'] if x['max_similarity'] is not None else -1),x['reference_id']))
    report.warnings.sort(key=lambda x:(-x['max_similarity'],x['reference_id']))
    return report


def prompt_similarity(report,top_k):
    data=report.model_dump() if isinstance(report,SimilarityReport) else report
    return {'snapshot_hash':data['snapshot_hash'],'warning_count':len(data['warnings']),
            'warning_ids':[w['warning_id'] for w in data['warnings']],
            'warnings':data['warnings'][:top_k],
            'references':[r for r in data['references'] if r['relationship']!='ancestor'][:top_k],
            'ancestors':[r for r in data['references'] if r['relationship']=='ancestor'],
            'insufficient_reasons':data['insufficient_reasons']}
