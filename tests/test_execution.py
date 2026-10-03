import pandas as pd
import pytest
from research.data import DataStore,register_dataset
from research.schemas import TaskConfig
from research.evaluation import Evaluator,run_cached
from research import db


def test_signal_executes_next_day_and_halted_orders_are_blocked(tiny_data):
    f,m=tiny_data
    m.loc[(m.symbol=='A')&(m.date=='2020-02-03'),'can_buy']=False
    register_dataset('halt',f,m,'test',True,True)
    e=Evaluator(DataStore('halt'),TaskConfig(name='test',dataset_id='halt',min_turnover_cny=0,min_listing_days=0))
    signals={pd.Timestamp('2020-01-31'):pd.Series({'A':2.,'B':1.})}
    blocked=e.portfolio(signals,'2020-01-31','2020-02-28')
    assert blocked['blocked_orders']==1
    assert blocked['cost_cny']==0
    assert blocked['total_return']==0


def test_delisting_settlement_loss_is_retained(tiny_data):
    f,m=tiny_data
    m['delist_cash']=float('nan')
    m.loc[m.symbol=='A','delist_date']=pd.Timestamp('2020-02-10')
    m.loc[(m.symbol=='A')&(m.date=='2020-02-10'),'delist_cash']=0.
    m=m[~((m.symbol=='A')&(m.date>'2020-02-10'))]
    register_dataset('delist',f,m,'test',True,True)
    e=Evaluator(DataStore('delist'),TaskConfig(name='test',dataset_id='delist',min_turnover_cny=0))
    result=e.portfolio({pd.Timestamp('2020-01-31'):pd.Series({'A':2.,'B':1.})},'2020-01-31','2020-02-28')
    assert result['total_return']<-.98


def test_future_labels_never_cross_split_boundary(tiny_data):
    e=Evaluator(DataStore('tiny'),TaskConfig(name='test',dataset_id='tiny'))
    assert e.forward_return(pd.Timestamp('2020-12-01'),60,'2020-12-31') is None


def test_cached_experiment_not_reexecuted(tiny_data,monkeypatch):
    from research.api import create_task
    cfg=TaskConfig(name='test',dataset_id='tiny')
    tid=create_task(cfg)['id']
    e=Evaluator(DataStore('tiny'),cfg)
    count=[]
    monkeypatch.setattr(e,'run',lambda *a:(count.append(1) or {'metric':.5}))
    first=run_cached(tid,'h',{'formula':'revenue'},'development',e,'2014-01-01','2019-12-31')
    second=run_cached(tid,'h',{'formula':'revenue'},'development',e,'2014-01-01','2019-12-31')
    assert len(count)==1 and first['id']==second['id']
