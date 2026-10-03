import numpy as np
import pandas as pd
import pytest
from research.data import DataStore,register_dataset
from research.factors import evaluate_formula,FormulaError,canonical


def test_restatement_never_backfills_past(tiny_data):
    store=DataStore('tiny')
    before=store.snapshot('2020-02-03',0,0)
    after=store.snapshot('2020-08-03',0,0)
    assert before.panels['revenue'].loc[pd.Period('2019Q4'),'A']==100
    assert after.panels['revenue'].loc[pd.Period('2019Q4'),'A']==500


def test_date_only_announcement_available_next_session(tiny_data):
    store=DataStore('tiny')
    assert store.snapshot('2020-01-15',0,0).latest_period['A']==pd.Period('2019Q3')
    assert store.snapshot('2020-01-16',0,0).latest_period['A']==pd.Period('2019Q4')


def test_cumulative_to_quarter_and_ttm(tiny_data):
    snap=DataStore('tiny').snapshot('2020-02-03',0,0)
    assert evaluate_formula('revenue',snap)['A']==100
    assert evaluate_formula('ttm(revenue)',snap)['A']==400
    assert evaluate_formula('total_assets',snap)['A']==1000
    assert evaluate_formula('ttm(cfo) / total_assets',snap)['A']==.08


def test_missing_quarter_is_not_replaced_by_older_quarter(tiny_data):
    f,m=tiny_data
    f=f[~((f.symbol=='A') & (f.period==pd.Timestamp('2019-06-30')))]
    register_dataset('gap',f,m,'fixture',True,True)
    snap=DataStore('gap').snapshot('2020-02-03',0,0)
    assert np.isnan(evaluate_formula('ttm(revenue)',snap)['A'])


def test_zero_denominator_is_missing_not_infinite(tiny_data):
    snap=DataStore('tiny').snapshot('2020-02-03',0,0)
    assert evaluate_formula('revenue / (total_assets - total_assets)',snap).isna().all()


@pytest.mark.parametrize('formula',["__import__('os').system('echo x')","revenue.__class__","open('file')","lag(revenue, -1)","lag(revenue, 0)","lag(revenue, 13)","revenue ** 100","ttm(rank(revenue))","ttm(market_cap)","revenue[0]"])
def test_formula_rejects_unsafe_or_ambiguous_operations(formula):
    with pytest.raises(FormulaError):canonical(formula)


def test_cross_section_neutralization_uses_current_snapshot(tiny_data):
    snap=DataStore('tiny').snapshot('2020-02-03',0,0)
    values=evaluate_formula('industry_neutralize(revenue)',snap)
    assert values['A']==-50 and values['B']==50


def test_development_store_excludes_future_data(tiny_data):
    store=DataStore('tiny',allowed_end='2019-12-31')
    assert store.financials.version_date.max()<=pd.Timestamp('2019-12-31')
    assert store.market.date.max()<=pd.Timestamp('2019-12-31')
    with pytest.raises(PermissionError):store.snapshot('2020-02-03')


def test_historical_st_and_delisting_filter(tiny_data):
    f,m=tiny_data
    m.loc[(m.symbol=='A')&(m.date>='2020-01-01'),'is_st']=True
    m.loc[m.symbol=='B','delist_date']=pd.Timestamp('2020-06-01')
    register_dataset('states',f,m,'fixture',True,True)
    store=DataStore('states')
    assert 'A' in store.snapshot('2019-12-31',0,0).market.index
    assert 'A' not in store.snapshot('2020-02-03',0,0).market.index
    assert 'B' not in store.snapshot('2020-06-02',0,0).market.index


def test_import_rejects_duplicate_market_rows(tiny_data):
    f,m=tiny_data
    with pytest.raises(ValueError,match='重复行情'):register_dataset('bad',f,pd.concat([m,m.head(1)]),'fixture',demo=True)


def test_dataset_version_is_immutable(tiny_data):
    f,m=tiny_data
    f.loc[0,'revenue']=900
    with pytest.raises(ValueError,match='新版本'):register_dataset('tiny',f,m,'fixture',demo=True)
