import json
from importlib.util import find_spec
import numpy as np
import pandas as pd
import pytest
from research import db
from research.schemas import TaskConfig
from research.research_types import SimilarityPolicy
from conftest import store_research_result


def similarity_module():
    assert find_spec('research.similarity'), 'research reference and similarity module is missing'
    from research import similarity
    return similarity


def publish_fixture(tid,hid):
    mod=similarity_module()
    for split in ['development','validation']:store_research_result(tid,hid,split)
    mod.freeze_reference_snapshot(tid)
    db.artifact(tid,'approved_batch','system','approved',{'policy_hash':db.digest(db.task(tid)['config']['research_policy']),'candidate_ids':[hid]})
    return mod.publish_research_references(tid,[hid])


def test_reference_snapshot_is_prefinal_and_immutable(research_task):
    mod=similarity_module()
    from research.api import create_task
    tid,hid=research_task
    refs=publish_fixture(tid,hid)
    new=create_task(TaskConfig(name='next',dataset_id='tiny'))['id']
    snapshot_id=mod.freeze_reference_snapshot(new)
    snapshot=db.one('SELECT payload FROM artifacts WHERE id=?',(snapshot_id,))['payload']
    assert len(json.loads(snapshot)['members'])==1
    with db.connect() as c:
        c.execute("INSERT INTO factors VALUES(?,?,?,?,?,?)",(db.uid(),tid,hid,'rejected','final performance bad',db.now()))
        c.execute("UPDATE tasks SET final_status='tested' WHERE id=?",(tid,))
    assert mod.freeze_reference_snapshot(new)==snapshot_id
    assert db.one('SELECT payload FROM artifacts WHERE id=?',(snapshot_id,))['payload']==snapshot
    with pytest.raises(ValueError,match='最终'):
        mod.publish_research_references(tid,[hid])


def test_general_memory_cannot_bypass_nearest_reference_prompt_limit(research_task):
    from research.api import create_task
    from research.literature import research_memory
    tid,hid=research_task
    refs=publish_fixture(tid,hid)
    new=create_task(TaskConfig(name='bounded prompt memory',dataset_id='tiny'))['id']
    similarity_module().freeze_reference_snapshot(new)
    memory=research_memory(new,'2022-12-31')
    assert memory['references']==[]
    assert memory['reference_count']==1
    assert refs[0] not in db.dump(memory)


def test_final_based_membership_cannot_enter_reference_pool(research_task,monkeypatch):
    mod=similarity_module()
    tid,hid=research_task
    with db.connect() as c:
        c.execute('INSERT INTO factors VALUES(?,?,?,?,?,?)',(db.uid(),tid,hid,'retained','最终收益 99%',db.now()))
    real=db.connect
    def guarded(final=False):
        assert not final,'research opened final database'
        return real()
    monkeypatch.setattr(db,'connect',guarded)
    sid=mod.freeze_reference_snapshot(tid)
    snap=json.loads(db.one('SELECT payload FROM artifacts WHERE id=?',(sid,))['payload'])
    assert snap['members']==[]
    assert '99%' not in db.dump(snap)


def test_reference_compatibility_and_control_groups(research_task):
    mod=similarity_module()
    from research.api import create_task,comparison
    tid,hid=research_task
    publish_fixture(tid,hid)
    other=create_task(TaskConfig(name='different',dataset_id='tiny',commission_bps=20))['id']
    sid=mod.freeze_reference_snapshot(other)
    snap=json.loads(db.one('SELECT payload FROM artifacts WHERE id=?',(sid,))['payload'])
    assert snap['members']==[] and snap['excluded']
    comparison(tid)
    rows=db.get_artifacts_by_kind('comparison')
    cutoffs={r['payload'].get('reference_cutoff') for r in rows}
    assert len(cutoffs)==1 and None not in cutoffs


def panels(months=12,symbols=20):
    return pd.DataFrame([{'signal_date':d,'symbol':str(i),'value':float(i),'eligible':True}
                         for d in pd.date_range('2019-01-31',periods=months,freq='ME') for i in range(symbols)])


def test_pair_similarity_matches_symbols_not_row_order():
    mod=similarity_module()
    left=panels()
    right=left.sample(frac=1,random_state=3)
    result=mod.compare_signal_pair(left,right,SimilarityPolicy(),20)
    assert result['status']=='complete' and result['mean_abs_correlation']==1
    right=right.assign(value=-right.value)
    result=mod.compare_signal_pair(left,right,SimilarityPolicy(),20)
    assert result['mean_correlation']==-1 and result['mean_abs_correlation']==1


@pytest.mark.parametrize('right',[panels(11),panels(symbols=19),panels().assign(value=1.),panels().assign(symbol=lambda x:'X'+x.symbol)])
def test_insufficient_overlap_is_not_zero_correlation(right):
    mod=similarity_module()
    result=mod.compare_signal_pair(panels(),right,SimilarityPolicy(),20)
    assert result['status']=='insufficient' and result['mean_abs_correlation'] is None


def test_warning_threshold_and_top_k_are_deterministic():
    mod=similarity_module()
    assert mod.is_high_similarity({'status':'complete','mean_abs_correlation':.75},SimilarityPolicy())
    assert not mod.is_high_similarity({'status':'complete','mean_abs_correlation':.749},SimilarityPolicy())
    assert not mod.is_high_similarity({'status':'insufficient','mean_abs_correlation':None},SimilarityPolicy())


def test_signal_cache_reuses_and_rejects_contamination(research_task,monkeypatch):
    mod=similarity_module()
    from research.evaluation import Evaluator
    tid,hid=research_task
    # The fixture period is intentionally shorter than a production interval.
    h={'formula':'market_cap','direction':1}
    count=[]
    original=Evaluator.signal_panel
    def track(*args,**kw):count.append(1);return original(*args,**kw)
    monkeypatch.setattr(Evaluator,'signal_panel',track)
    a=mod.load_research_signals(tid,h,'development')
    b=mod.load_research_signals(tid,h,'development')
    pd.testing.assert_frame_equal(a,b)
    assert len(count)==1
    from research.config import home
    path=next((home()/'research_signals').glob('*.parquet'))
    path.write_bytes(b'incomplete or corrupted cache')
    recovered=mod.load_research_signals(tid,h,'development')
    pd.testing.assert_frame_equal(a,recovered)
    assert len(count)==2
    mod.load_research_signals(tid,{**h,'direction':-1},'development')
    assert len(count)==3
    monkeypatch.setattr(mod,'engine_signature',lambda:'changed-evaluation-code')
    mod.load_research_signals(tid,h,'development')
    assert len(count)==4
    with pytest.raises(ValueError,match='开发|验证'):
        mod.load_research_signals(tid,h,'final')
