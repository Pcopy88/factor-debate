"""Human-only recent and forward observations; no research memory imports this module."""
import pandas as pd
from . import db
from .schemas import TaskConfig
from .data import DataStore
from .evaluation import Evaluator, run_cached, engine_signature
from .factors import BASELINES


def frozen_batch(task_id):
    artifacts=db.get_artifacts(task_id,'frozen_batch')
    if not artifacts: raise ValueError('候选尚未冻结')
    frozen=artifacts[0]
    if db.digest(frozen['payload']) != db.task(task_id)['frozen_hash']: raise ValueError('冻结批次校验失败')
    if frozen['payload']['engine_signature'] != engine_signature(): raise ValueError('评估代码已变化，需使用冻结档案中的原引擎复现，不能混用前瞻结果')
    return frozen


def evaluate_observation(task_id,store,start,end,split):
    frozen=frozen_batch(task_id)['payload']
    cfg=TaskConfig.model_validate(frozen['config'])
    evaluator=Evaluator(store,cfg)
    if pd.Timestamp(start)>pd.Timestamp(end):return {'status':'waiting','reason':'尚无冻结之后的数据'}
    items=list(zip(frozen['candidate_ids'],frozen['formulas']))+[
        ('baseline:'+name,{'formula':formula,'direction':1}) for name,formula in BASELINES.items()]
    for hid,h in items:
        run_cached(task_id,hid,h,split,evaluator,start,end,final=True)
    return {'status':'complete','split':split,'start':str(start),'end':str(end),'dataset_version':store.manifest['version']}


def run_recent(task_id):
    frozen=frozen_batch(task_id)
    cfg=TaskConfig.model_validate(frozen['payload']['config'])
    start=pd.Timestamp(cfg.final_end)+pd.Timedelta(days=1)
    # Existing observations through freeze day remain retrospective supplements.
    end=min(pd.Timestamp(frozen['created_at']).tz_convert(None).normalize(),pd.Timestamp(db.task(task_id)['updated_at']).tz_convert(None).normalize())
    store=DataStore(cfg.dataset_id,allowed_end=end.date())
    end=min(end,store.dates.max())
    return evaluate_observation(task_id,store,start.date(),end.date(),'recent')


def run_forward(task_id,dataset_id):
    frozen=frozen_batch(task_id)
    cfg=TaskConfig.model_validate(frozen['payload']['config'])
    original=DataStore(cfg.dataset_id)
    store=DataStore(dataset_id)
    if store.manifest['demo'] != original.manifest['demo'] or (not store.manifest['demo'] and not store.manifest['pit_verified']):
        raise ValueError('新数据必须保持真实／演示类型，并通过历史时点验收')
    for kind,keys in [('market',['symbol','date']),('financials',['symbol','period','version_date'])]:
        old=getattr(original,kind).drop(columns=['liquidity_60'],errors='ignore').set_index(keys).sort_index()
        new=getattr(store,kind).drop(columns=['liquidity_60'],errors='ignore').set_index(keys).sort_index()
        common=new.reindex(index=old.index,columns=old.columns)
        if not old.equals(common): raise ValueError('新版本覆盖或删除了原有数据；前瞻观察只接受保留原始记录的追加版本')
    start=pd.Timestamp(frozen['created_at']).tz_convert(None).normalize()+pd.Timedelta(days=1)
    end=min(store.dates.max(),pd.Timestamp(db.now()).tz_convert(None).normalize())
    result=evaluate_observation(task_id,store,start.date(),end.date(),'forward')
    db.event(task_id,'observation','前瞻观察已登记，结果只供人工查看',{'status':result['status'],'dataset_id':dataset_id})
    return result
