import numpy as np
import pandas as pd
import pytest
from research import db
from research.data import register_dataset
from research.factors import FINANCIAL_FIELDS


@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setenv('FACTOR_LAB_HOME',str(tmp_path/'lab'))
    db.initialize()
    return tmp_path


@pytest.fixture
def tiny_data():
    financial=[]
    for symbol,scale in [('A',1.),('B',2.)]:
        for period in pd.period_range('2018Q1','2020Q4',freq='Q'):
            day=period.end_time.normalize()
            row={k:100.*scale for k in FINANCIAL_FIELDS}
            row.update(symbol=symbol,period=day,announced_at=day+pd.Timedelta(days=15),
                       version_date=day+pd.Timedelta(days=15),report_kind='cumulative',
                       revenue=period.quarter*100.*scale,cfo=period.quarter*20.*scale,
                       net_profit=period.quarter*15.*scale,total_assets=1000.*scale)
            financial.append(row)
            if symbol=='A' and str(period)=='2019Q4':
                financial.append({**row,'version_date':pd.Timestamp('2020-08-01'),'revenue':800.})
    market=[]
    for day in pd.bdate_range('2018-01-01','2021-12-31'):
        for symbol in ['A','B']:
            market.append(dict(symbol=symbol,date=day,open=10.,close=10.,adj_factor=1.,amount=100000000.,
                               market_cap=100000000.,industry='制造',is_st=False,can_buy=True,can_sell=True,
                               list_date=pd.Timestamp('2010-01-01'),delist_date=pd.NaT))
    f,m=pd.DataFrame(financial),pd.DataFrame(market)
    register_dataset('tiny',f,m,'test fixtures',pit_verified=True,demo=True)
    return f,m


@pytest.fixture
def research_task(tiny_data):
    from research.api import create_task
    from research.schemas import TaskConfig, Hypothesis
    tid=create_task(TaskConfig(name='v2 fixture',dataset_id='tiny'))['id']
    h=Hypothesis(title='质量测试',formula='ttm(cfo) / total_assets',
                 mechanism='现金流支持的资产收益质量可以反映经营质量',failure_condition='一次性回款可能导致失效')
    hid=db.artifact(tid,'hypothesis','value','proposal:0',h.model_dump())
    return tid,hid


def store_research_result(tid,hid,split='development',**overrides):
    from research.evaluation import engine_signature
    from research.data import manifest
    cfg=db.task(tid)['config']
    prefix='develop' if split=='development' else 'validation'
    payload={'formula':'ttm(cfo) / total_assets','dataset_version':manifest('tiny')['version'],
             'engine_signature':engine_signature(),'period':[cfg[prefix+'_start'],cfg[prefix+'_end']],
             'ic':{str(h):{'mean':.03,'n':30,'icir':.8,'positive_ratio':.7,'label_coverage':.98,
                           'missing_labels':2,'t':2.,'p_adjusted':.2,'points':[]} for h in [20,60,120]},
             'coverage':.9,'signals':36,'planned_signals':36,'signal_month_ratio':1.,
             'portfolio':{'annual_return':.08,'max_drawdown':-.2,'annual_turnover':2.,'cost_cny':2000,
                          'blocked_orders':1,'yearly':{'2020':.12}},
             'benchmark':{'annual_return':.05,'yearly':{'2020':.08}},
             'annual_excess':.03,'double_cost':{'annual_return':.06},
             'double_cost_benchmark':{'annual_return':.045},'double_cost_annual_excess':.015,
             'quarterly':{'annual_return':.07},'industries':[{'name':'制造','rank_ic':.02,'months':30}],
             'baseline_comparison':[],'controlled_ic':{'mean':.01,'n':30},'groups':[],'demo':True}
    payload.update(overrides)
    eid=db.uid()
    with db.connect() as c:
        c.execute('INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?)',
                  (eid,tid,hid,eid,split,db.dump(payload),'complete',db.now()))
    db.bind_evaluation(tid,hid,split,eid)
    return eid
