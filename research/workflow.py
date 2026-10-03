import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.sqlite import SqliteSaver
from . import db
from .config import path_for
from .schemas import TaskConfig, Hypothesis
from .agents import run_agent
from .models import BudgetExhausted
from .data import DataStore, manifest
from .factors import canonical, BASELINES
from .evaluation import Evaluator, run_cached, ENGINE_VERSION, engine_signature, engine_sources

os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
os.environ["LANGSMITH_TRACING"] = "false"


class Paused(RuntimeError): pass


class State(TypedDict, total=False):
    task_id: str
    candidates: list[str]
    rejected: bool
    return_to_intake: bool
    return_to_batch: bool
    feedback: str
    return_id: str
    last_decision_id: str
    data_rejected: bool


def check_running(task_id):
    t = db.task(task_id)
    if t["paused"]: raise Paused("任务已暂停，将从保存的步骤继续")


def status(task_id, text):
    with db.connect() as c:
        c.execute("UPDATE tasks SET status=?,gate=NULL,updated_at=? WHERE id=?", (text,db.now(),task_id))


def candidate(task_id, artifact_id):
    item = db.one("SELECT * FROM artifacts WHERE id=? AND task_id=? AND kind='hypothesis'", (artifact_id,task_id))
    if not item: raise ValueError("候选版本不存在")
    item["payload"] = json.loads(item["payload"])
    return item


def save_hypotheses(task_id, role, stage, hypotheses, parents=None):
    result = []
    for i, h in enumerate(hypotheses):
        parent = parents[i] if parents and i < len(parents) else None
        result.append(db.artifact(task_id, "hypothesis", role, f"{stage}:{i}", h.model_dump(), parent))
    return result


def intake(state):
    decision = interrupt({"gate": 1, "title": "确定研究目标、数据和本批预算"})
    return {"rejected": decision['action']=='reject',"return_to_intake":decision['action']=='return',
            "last_decision_id":decision.get('request_id','')}


def preflight(state):
    task_id = state["task_id"]
    check_running(task_id)
    status(task_id, "preflight")
    cfg = TaskConfig.model_validate(db.task(task_id)["config"])
    store = DataStore(cfg.dataset_id)
    audit = store.audit(cfg)
    if cfg.mode == "live" and store.manifest["demo"]:
        audit['passed']=False
        audit['errors'].append("真实研究不能使用演示数据集")
    db.artifact(task_id,"data_audit","system","preflight:"+db.digest(cfg.model_dump(mode='json')),audit)
    if not audit["passed"]:
        db.event(task_id,"data_rejected","数据验收未通过，返回环节 1；不会自动缩短历史区间",audit)
        return {"data_rejected":True}
    db.event(task_id,"data","数据覆盖与版本检查完成",{"demo":store.manifest["demo"]})
    from .similarity import freeze_reference_snapshot
    freeze_reference_snapshot(task_id)
    return {"data_rejected":False}


def propose(state):
    task_id = state["task_id"]
    check_running(task_id)
    status(task_id, "proposing")
    cfg = db.task(task_id)["config"]
    roles = ["value"] if cfg["architecture"] == "single" else ["value", "growth"]
    with ThreadPoolExecutor(max_workers=len(roles)) as pool:
        count = cfg['proposals_per_agent'] * (2 if cfg['architecture']=='single' else 1)
        outputs = list(pool.map(lambda role: run_agent(task_id, role, "proposal", "独立提案", {"count": count}), roles))
    candidates = []
    for role, output in zip(roles, outputs):
        candidates.extend(save_hypotheses(task_id, role, "proposal", output.hypotheses[:count]))
    if not candidates:
        with db.connect() as c:c.execute("UPDATE tasks SET final_status='no_candidates' WHERE id=?",(task_id,))
        db.event(task_id,'no_candidates','本批未形成可计算的候选，保留提案记录并正常结束')
    return {"candidates": candidates}


def debate(state):
    task_id, ids = state["task_id"], state["candidates"]
    cfg = db.task(task_id)["config"]
    if cfg["architecture"] != "debate": return {}
    status(task_id, "debating")
    for round_no in range(1, cfg["debate_rounds"] + 1):
        check_running(task_id)
        grouped = {role: [candidate(task_id, h) for h in ids if candidate(task_id, h)["role"] == role] for role in ["value","growth"]}
        reviews = {}
        for role, other in [("value","growth"),("growth","value")]:
            targets = [{"id": h["id"], **h["payload"]} for h in grouped[other]]
            reviews[role] = run_agent(task_id, role, f"critique:{round_no}", "交叉批判", {"targets":targets,"round":round_no})
            if any(c.target_id not in {h["id"] for h in grouped[other]} for c in reviews[role].critiques):
                raise ValueError("批判目标必须属于对方当前提案")
        revised_ids = []
        for role, other in [("value","growth"),("growth","value")]:
            originals = grouped[role]
            output = run_agent(task_id, role, f"rewrite:{round_no}", "回应与改写",
                               {"hypotheses":[h["payload"] for h in originals],"critiques":[c.model_dump() for c in reviews[other].critiques],"round":round_no})
            revised_ids.extend(save_hypotheses(task_id,role,f"rewrite:{round_no}",output.hypotheses[:len(originals)],[h["id"] for h in originals]))
        ids = revised_ids or ids
    return {"candidates": ids}


def approve_batch(state):
    decision = interrupt({"gate":5,"title":"批准本批候选与限次修改范围","candidate_ids":state["candidates"]})
    return {"rejected": decision['action']=='reject',"return_to_batch":decision['action']=='return',
            "feedback":decision.get('reason',''),"return_id":decision.get('request_id',''),
            "last_decision_id":decision.get('request_id','')}


def return_revision(state):
    task_id=state['task_id']
    check_running(task_id)
    status(task_id,'debating')
    ids=[]
    for role in ['value','growth']:
        originals=[candidate(task_id,h) for h in state['candidates'] if candidate(task_id,h)['role']==role]
        if not originals:continue
        stage='human-return:'+state['return_id']
        output=run_agent(task_id,role,stage,'回应与改写',{'hypotheses':[h['payload'] for h in originals],
                         'human_feedback':state['feedback'],'round':0})
        ids.extend(save_hypotheses(task_id,role,stage,output.hypotheses[:len(originals)],[h['id'] for h in originals]))
    return {'candidates':ids or state['candidates'],'return_to_batch':False}


def experiments(state):
    task_id = state["task_id"]
    check_running(task_id)
    status(task_id, "evaluating")
    cfg = TaskConfig.model_validate(db.task(task_id)["config"])
    # Physical query cutoff prevents development evaluators from loading future data.
    evaluator = Evaluator(DataStore(cfg.dataset_id, allowed_end=cfg.validation_end), cfg)
    for title, formula in BASELINES.items():
        h = {"formula":formula,"direction":1}
        for split, start, end in [("development",cfg.develop_start,cfg.develop_end),("validation",cfg.validation_start,cfg.validation_end)]:
            check_running(task_id)
            run_cached(task_id,"baseline:"+title,h,split,evaluator,start,end)
    from .research_loop import run_empirical_loop, research_sources
    from .selection import select_candidates
    all_ids=run_empirical_loop(task_id,list(state['candidates']))
    selection=select_candidates(task_id,all_ids)
    ids=selection.selected_ids
    frozen = {"candidate_ids":ids,"formulas":[candidate(task_id,h)["payload"] for h in ids],
              "config":cfg.model_dump(mode="json"),"dataset_version":evaluator.store.manifest["version"],
              "engine":ENGINE_VERSION,"engine_signature":engine_signature(),
              "all_candidate_ids":all_ids,"selection":selection.model_dump(),
              "research_signature":db.digest(research_sources())}
    db.artifact(task_id,'engine_source','engineer','frozen',engine_sources())
    db.artifact(task_id,'research_source','system','frozen',research_sources())
    frozen_hash = db.digest(frozen)
    db.artifact(task_id,"frozen_batch","system","frozen",frozen)
    with db.connect() as c: c.execute("UPDATE tasks SET frozen_hash=? WHERE id=?",(frozen_hash,task_id))
    return {"candidates":ids}


def final_test(state):
    task_id = state["task_id"]
    check_running(task_id)
    cfg = TaskConfig.model_validate(db.task(task_id)["config"])
    if not state["candidates"]:
        with db.connect() as c: c.execute("UPDATE tasks SET final_status='no_candidates' WHERE id=?",(task_id,))
        return {}
    frozen = [a for a in db.get_artifacts(task_id,"frozen_batch")][0]["payload"]
    if db.digest(frozen) != db.task(task_id)["frozen_hash"]: raise ValueError("冻结批次校验失败")
    key = f"{cfg.mode}|{cfg.final_start}|{cfg.final_end}"
    if cfg.mode == "live":
        with db.connect(final=True) as c:
            c.execute("BEGIN IMMEDIATE")
            for window in c.execute("SELECT * FROM windows").fetchall():
                mode, start, end = window["window_key"].split("|")
                if mode == "live" and window["task_id"] != task_id and start <= str(cfg.final_end) and end >= str(cfg.final_start):
                    with db.connect() as other: other.execute("UPDATE tasks SET final_status='previously_used' WHERE id=?",(task_id,))
                    db.event(task_id,"final","该历史测试区已使用，本批候选转入等待新数据观察")
                    return {}
            c.execute("INSERT OR IGNORE INTO windows VALUES(?,?,?,?)",(key,task_id,db.task(task_id)["frozen_hash"],db.now()))
    status(task_id,"final_testing")
    evaluator = Evaluator(DataStore(cfg.dataset_id, allowed_end=cfg.final_end),cfg)
    for hid in state["candidates"]:
        check_running(task_id)
        run_cached(task_id,hid,candidate(task_id,hid)["payload"],"final",evaluator,cfg.final_start,cfg.final_end,final=True)
    for title,formula in BASELINES.items():
        run_cached(task_id,"baseline:"+title,{"formula":formula,"direction":1},"final",evaluator,cfg.final_start,cfg.final_end,final=True)
    from .observation import run_recent
    run_recent(task_id)
    with db.connect() as c: c.execute("UPDATE tasks SET final_status=? WHERE id=?",("demo_only" if cfg.mode=="demo" else "tested",task_id))
    db.event(task_id,"final","最终评估已完成，结果仅提供给人工审核")
    return {}


def admission(state):
    decision = interrupt({"gate":10,"title":"查看最终报告，决定观察、入库或淘汰","candidate_ids":state.get("candidates",[])})
    outcome = {"retain":"retained","observe":"observing"}.get(decision["action"],"rejected")
    for hid in state.get("candidates",[]):
        with db.connect() as c:
            c.execute("INSERT OR IGNORE INTO factors VALUES(?,?,?,?,?,?)",(db.uid(),state["task_id"],hid,outcome,decision.get("reason",""),db.now()))
    return {"last_decision_id":decision.get('request_id','')}


def finish(state):
    status(state["task_id"],"rejected" if state.get("rejected") else "completed")
    db.event(state["task_id"],"complete","研究批次已结束，所有版本与失败记录均保留")
    return {}


def build_graph(checkpointer):
    graph = StateGraph(State)
    for name, fn in [("intake",intake),("preflight",preflight),("propose",propose),("debate",debate),
                     ("approve_batch",approve_batch),("return_revision",return_revision),("experiments",experiments),("final_test",final_test),("admission",admission),("finish",finish)]:
        graph.add_node(name,fn)
    graph.add_edge(START,"intake")
    graph.add_conditional_edges("intake",lambda s:"finish" if s.get("rejected") else 'intake' if s.get('return_to_intake') else "preflight")
    graph.add_conditional_edges("preflight",lambda s:'intake' if s.get('data_rejected') else 'propose')
    graph.add_conditional_edges("propose",lambda s:'debate' if s.get('candidates') else 'finish')
    graph.add_edge("debate","approve_batch")
    graph.add_conditional_edges("approve_batch",lambda s:"finish" if s.get("rejected") else 'return_revision' if s.get('return_to_batch') else "experiments")
    graph.add_edge('return_revision','approve_batch')
    graph.add_edge("experiments","final_test")
    graph.add_conditional_edges('final_test',lambda s:'admission' if s.get('candidates') else 'finish')
    graph.add_edge("admission","finish")
    graph.add_edge("finish",END)
    return graph.compile(checkpointer=checkpointer)


def run_workflow(task_id, resume=None):
    if db.task(task_id)['config'].get('workflow_version',1) != 2:
        with db.connect() as c:
            c.execute("UPDATE tasks SET status='needs_attention',error=? WHERE id=?",
                      ('旧流程检查点需使用对应旧版本继续，或从第 1 环节创建新版任务；原档案与检查点已保留',task_id))
        return {}
    with SqliteSaver.from_conn_string(str(path_for("checkpoints.sqlite"))) as saver:
        graph = build_graph(saver)
        config = {"configurable":{"thread_id":task_id},"recursion_limit":60}
        snapshot = graph.get_state(config)
        if not snapshot.values:
            graph.invoke({"task_id":task_id},config)
            snapshot = graph.get_state(config)
        pending = [i for t in snapshot.tasks for i in t.interrupts]
        # A queued approval can survive a process crash. Only feed it to its original
        # interrupt, never to the next gate reached by an already committed decision.
        already_applied = bool(resume and resume.get('request_id') and
                               snapshot.values.get('last_decision_id') == resume['request_id'])
        matching_gate = bool(pending and (not resume or not resume.get('gate') or
                                         pending[0].value['gate'] == resume['gate']))
        if resume and pending and matching_gate and not already_applied:
            result = graph.invoke(Command(resume=resume),config)
        else:
            result = graph.invoke(None,config)
        if result and result.get("__interrupt__"):
            gate = result["__interrupt__"][0].value["gate"]
            with db.connect() as c:
                c.execute("UPDATE tasks SET status='awaiting_approval',gate=?,version=version+1,updated_at=? WHERE id=?",(gate,db.now(),task_id))
            db.event(task_id,"approval",f"等待你的第 {gate} 环节决策")
        return result
