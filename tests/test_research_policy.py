import json
import pytest
from pydantic import ValidationError
from research import db, schemas


def test_policy_defaults_are_explicit():
    cfg = schemas.TaskConfig(name='new')
    assert getattr(cfg, 'workflow_version', None) == 2
    p = cfg.research_policy
    assert (p.min_ic_months, p.min_factor_coverage, p.min_label_coverage) == (24, .7, .9)
    assert p.min_rank_ic == p.min_annual_excess == 0
    assert p.similarity.similarity_threshold == .75
    assert (p.similarity.min_pair_symbols, p.similarity.min_valid_months, p.similarity.top_k) == (20, 12, 3)
    for changes in [{'min_factor_coverage': 1.1}, {'min_ic_months': -1}]:
        with pytest.raises(ValidationError):
            schemas.TaskConfig(name='bad', research_policy=changes)


def test_schema_upgrade_preserves_old_archives():
    with db.connect() as c:
        c.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        c.execute("INSERT INTO tasks(id,name,config,status) VALUES('old','old','{}','completed')")
    before = db.task('old')
    db.initialize()
    db.initialize()
    assert db.task('old') == before
    assert db.one("SELECT value FROM meta WHERE key='schema_version'")['value'] == '2'
    names = {r['name'] for r in db.rows("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'evaluation_bindings','research_steps','research_reference_factors'} <= names


def test_legacy_config_is_not_silently_upgraded(tiny_data):
    from research.api import create_task
    from research.workflow import run_workflow
    tid = create_task(schemas.TaskConfig(name='old', dataset_id='tiny'))['id']
    cfg = db.task(tid)['config']
    cfg.pop('workflow_version', None)
    with db.connect() as c:
        c.execute('UPDATE tasks SET config=? WHERE id=?',(db.dump(cfg),tid))
    run_workflow(tid)
    assert db.task(tid)['status'] == 'needs_attention'
    assert not db.get_artifacts(tid)
