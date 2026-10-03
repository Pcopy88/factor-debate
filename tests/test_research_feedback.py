from types import SimpleNamespace
import numpy as np
import pandas as pd
import pytest
from research import db
from research.research_types import SimilarityReport, ResearchPolicy
from conftest import store_research_result


def feedback_module():
    import research
    from importlib.util import find_spec
    assert find_spec('research.feedback'), 'complete feedback module is missing'
    from research import feedback
    return feedback


def test_feedback_contains_both_splits_and_all_required_metrics(research_task):
    mod=feedback_module()
    tid,hid=research_task
    for split in ['development','validation']:store_research_result(tid,hid,split)
    result=mod.build_feedback(tid,hid,0,SimilarityReport())
    assert result.development.experiment_id != result.validation.experiment_id
    metrics=result.validation.metrics
    assert {'ic','portfolio','annual_excess','coverage','double_cost_annual_excess',
            'yearly_excess','industries','baseline_comparison','controlled_ic','quarterly'} <= set(metrics)
    assert metrics['yearly_excess']['2020']==pytest.approx(.04)
    assert metrics['portfolio']['yearly']['2020']==pytest.approx(.12)
    assert metrics['benchmark']['yearly']['2020']==pytest.approx(.08)


def test_feedback_explains_nested_missing_metrics(research_task):
    mod=feedback_module();tid,hid=research_task
    store_research_result(tid,hid,'validation',ic={'60':{'n':1,'mean':.02,'icir':None}})
    part=mod.build_feedback(tid,hid,0,SimilarityReport()).validation
    assert part.metrics['ic']['60']['icir'] is None
    assert any('ic.60.icir' in r and '标准差' in r for r in part.missing_reasons)


def test_parent_changes_keep_improvements_and_deterioration(research_task):
    mod=feedback_module();tid,hid=research_task
    for split in ['development','validation']:store_research_result(tid,hid,split)
    parent=mod.build_feedback(tid,hid,0,SimilarityReport())
    child=parent.model_copy(deep=True)
    child.validation.metrics['ic']['60']['mean']=.01
    child.validation.metrics['portfolio']['max_drawdown']=-.1
    change=mod.compare_feedback(parent,child)['validation']['metrics']
    assert change['ic']['60']['mean']=={'before':.03,'after':.01,'delta':-.02}
    assert change['portfolio']['max_drawdown']=={'before':-.2,'after':-.1,'delta':.1}


def test_factor_coverage_includes_empty_months(tiny_data,monkeypatch):
    from research.evaluation import Evaluator
    from research.data import DataStore
    from research.schemas import TaskConfig
    from research.factors import evaluate_formula
    evaluator=Evaluator(DataStore('tiny'),TaskConfig(name='coverage',dataset_id='tiny',min_cross_section=10))
    assert hasattr(evaluator,'signal_panel'), 'full planned-month panel is missing'
    from research.factors import Snapshot
    # Use real formula evaluation; only supply explicit date-specific market universes.
    def snapshot(day):
        return Snapshot({},pd.Series(dtype=object),pd.DataFrame({'market_cap':[1.] * 20 if day.month==1 else [np.nan]*20},
                              index=[str(i) for i in range(20)]))
    monkeypatch.setattr(evaluator,'snapshot',snapshot)
    panel=evaluator.signal_panel('market_cap','2019-01-01','2019-02-28')
    assert panel.eligible.sum()==40
    assert panel.value.notna().sum()/panel.eligible.sum()==.5


def test_icir_and_excess_have_fixed_units():
    from research.evaluation import hac_test
    stats=hac_test([.01,.02,.03])
    assert stats.get('icir')==pytest.approx(2)
    assert hac_test([.02,.02])['icir'] is None


def test_full_evaluator_keeps_coverage_panel_after_controlled_ic():
    from research.data import generate_demo,DataStore
    from research.evaluation import Evaluator
    from research.schemas import TaskConfig
    generate_demo()
    ev=Evaluator(DataStore('demo-v1'),TaskConfig(name='完整数值评估'))
    from research.factors import BASELINES
    result=ev.run({'formula':next(iter(BASELINES.values())),'direction':1},'2018-01-01','2019-12-31')
    assert result['planned_signals']==24 and result['signals']==24
    assert 0 < result['coverage'] <= 1
    assert result['ic']['60']['n'] > 0
    assert result['double_cost_annual_excess']==pytest.approx(
        result['double_cost']['annual_return']-result['double_cost_benchmark']['annual_return'])


def test_cached_result_is_bound_to_every_version(research_task):
    from research.evaluation import run_cached
    from research.schemas import TaskConfig
    from research.data import DataStore
    tid,hid=research_task
    calls=[]
    ev=SimpleNamespace(store=DataStore('tiny'),config=TaskConfig(name='aliases',dataset_id='tiny'))
    def run(*a):calls.append(1);return {'coverage':1}
    ev.run=run
    h={'formula':'market_cap','direction':1}
    a=run_cached(tid,hid,h,'development',ev,'2019-01-01','2019-12-31')
    b=run_cached(tid,'alias',h,'development',ev,'2019-01-01','2019-12-31')
    assert a['id']==b['id'] and len(calls)==1
    binds=db.rows('SELECT * FROM evaluation_bindings WHERE task_id=?',(tid,))
    assert {x['hypothesis_id'] for x in binds}=={hid,'alias'}


def test_quality_rules_distinguish_failed_and_insufficient(research_task):
    mod=feedback_module()
    tid,hid=research_task
    for split in ['development','validation']:store_research_result(tid,hid,split)
    f=mod.build_feedback(tid,hid,0,SimilarityReport())
    assert mod.assess_quality(f,ResearchPolicy()).quality_status=='passed'
    f.validation.metrics['ic']['60']['mean']=-.01
    assert mod.assess_quality(f,ResearchPolicy()).quality_status=='failed'
    f.validation.metrics['ic']['60']['mean']=.03
    f.validation.metrics['ic']['60']['n']=5
    assert mod.assess_quality(f,ResearchPolicy()).quality_status=='insufficient'
