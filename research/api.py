import io
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.cors import CORSMiddleware
from . import db
from .config import ROOT
from .schemas import TaskConfig, Decision, ModelProfile, DocumentInput, SearchInput, StrictModel
from .security import get_secret, redact
from .data import generate_demo, register_dataset, manifest
from .literature import add_document, fetch_source, research_documents, research_memory, search_web
from .providers import PROVIDERS
from pydantic import Field


@asynccontextmanager
async def lifespan(app):
    db.initialize()
    generate_demo()
    yield


app = FastAPI(title="因子研究室", version="0.1.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware,allowed_hosts=["127.0.0.1","localhost","testserver"])
app.add_middleware(CORSMiddleware,allow_origins=["http://127.0.0.1:5173","http://localhost:5173"],allow_methods=["*"],allow_headers=["Content-Type"])


@app.middleware("http")
async def local_mutations(request: Request,call_next):
    if request.method not in ("GET","HEAD","OPTIONS"):
        origin = request.headers.get("origin")
        if origin and not re.fullmatch(r"http://(?:127\.0\.0\.1|localhost)(?::(?:8765|5173))?",origin):
            return Response("不允许跨站修改本机研究数据",status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(ValueError)
async def invalid(request,exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail":redact(str(exc))},status_code=422)


@app.exception_handler(KeyError)
async def missing(request,exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail":str(exc)},status_code=404)


def decoded(items):
    for r in items:
        for key in ["payload","config","usage"]:
            if key in r and isinstance(r[key],str): r[key] = json.loads(r[key])
    return items


def queue(task_id,kind="research",payload=None):
    with db.connect() as c:
        old = c.execute("SELECT * FROM jobs WHERE task_id=?",(task_id,)).fetchone()
        if old:
            if old["status"] in ("queued","running"): return old["id"]
            saved_payload = old["payload"] if payload is None else db.dump(payload)
            c.execute("UPDATE jobs SET status='queued',payload=?,error='' WHERE id=?",(saved_payload,old["id"]))
            return old["id"]
        job_id = db.uid()
        c.execute("INSERT INTO jobs(id,task_id,kind,status,payload,created_at) VALUES(?,?,?,?,?,?)",(job_id,task_id,kind,"queued",db.dump(payload or {}),db.now()))
        return job_id


@app.get("/api/health")
def health():
    hb = db.one("SELECT value FROM settings WHERE key='worker_heartbeat'")
    running = bool(hb and (datetime.now(timezone.utc)-datetime.fromisoformat(hb["value"])).total_seconds()<30)
    return {"status":"ok","worker_running":running,"version":"0.1.0"}


@app.get("/api/overview")
def overview():
    tasks = list_tasks()
    return {"tasks":len(tasks),"awaiting":sum(t["gate"] is not None for t in tasks),
            "experiments":db.one("SELECT COUNT(*) n FROM experiments")["n"],
            "factors":db.one("SELECT COUNT(*) n FROM factors WHERE status IN ('observing','retained')")["n"],
            "spent":round(sum(t["spent"] for t in tasks),4),"health":health(),
            "recent_events":decoded(db.rows("SELECT e.* FROM events e JOIN tasks t ON e.task_id=t.id ORDER BY e.id DESC LIMIT 12"))}


@app.get("/api/tasks")
def list_tasks():
    return decoded(db.rows("SELECT * FROM tasks WHERE id NOT LIKE 'benchmark-%' ORDER BY created_at DESC"))


@app.post("/api/tasks")
def create_task(cfg: TaskConfig):
    manifest(cfg.dataset_id)
    task_id = db.uid()
    with db.connect() as c:
        c.execute("INSERT INTO tasks(id,name,config,status,gate,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                  (task_id,cfg.name,db.dump(cfg.model_dump(mode="json")),"awaiting_approval",1,db.now(),db.now()))
    db.event(task_id,"created","研究任务已创建，等待确认目标与预算")
    return db.task(task_id)


@app.put("/api/tasks/{task_id}")
def edit_task(task_id: str,cfg: TaskConfig):
    t = db.task(task_id)
    if t["gate"] != 1 or db.one("SELECT id FROM artifacts WHERE task_id=? AND kind NOT IN ('data_audit','comparison') LIMIT 1",(task_id,)):
        raise HTTPException(409,"进入研究后不覆盖原任务定义；请创建新批次保留可复查历史")
    manifest(cfg.dataset_id)
    with db.connect() as c: c.execute("UPDATE tasks SET name=?,config=?,version=version+1,updated_at=? WHERE id=?",(cfg.name,db.dump(cfg.model_dump(mode="json")),db.now(),task_id))
    return db.task(task_id)


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str):
    t = db.task(task_id)
    t["artifacts"] = db.get_artifacts(task_id)
    t["events"] = decoded(db.rows("SELECT * FROM events WHERE task_id=? ORDER BY id",(task_id,)))
    t["experiments"] = decoded(db.rows("SELECT * FROM experiments WHERE task_id=? ORDER BY created_at",(task_id,)))
    t["final_results"] = decoded(db.rows("SELECT * FROM results WHERE task_id=? ORDER BY created_at",(task_id,),final=True))
    t["decisions"] = db.rows("SELECT * FROM decisions WHERE task_id=? ORDER BY created_at",(task_id,))
    t["calls"] = decoded(db.rows("SELECT id,profile_id,stage,status,usage,cost,reserved,error,created_at FROM calls WHERE task_id=? ORDER BY created_at",(task_id,)))
    t['evaluation_bindings']=db.rows('SELECT * FROM evaluation_bindings WHERE task_id=?',(task_id,))
    return t


@app.get('/api/tasks/{task_id}/research-summary')
def research_summary_api(task_id:str):
    from .research_loop import research_summary
    return research_summary(task_id)


@app.post("/api/tasks/{task_id}/decisions")
def decide(task_id: str,decision: Decision):
    allowed = {1:{"approve","return","reject"},5:{"approve","return","reject"},10:{"retain","observe","reject"}}
    if decision.action not in allowed[decision.gate]: raise ValueError("该环节不支持所选决定")
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        old = c.execute("SELECT * FROM decisions WHERE request_id=?",(decision.request_id,)).fetchone()
        if old:
            if old["task_id"] != task_id or old["gate"] != decision.gate or old["action"] != decision.action:
                raise HTTPException(409,"请求编号已用于另一项决定")
            return {"status":"already_applied"}
        t = c.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
        if not t: raise HTTPException(404,"任务不存在")
        if t["gate"] != decision.gate or t["version"] != decision.expected_version or t["paused"]:
            raise HTTPException(409,"任务版本或审核环节已变化，请刷新后再决定")
        c.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",(decision.request_id,task_id,decision.gate,decision.action,decision.expected_version,decision.reason,db.now()))
        c.execute("UPDATE tasks SET status='queued',gate=NULL,version=version+1,error='',updated_at=? WHERE id=?",(db.now(),task_id))
        old_job = c.execute("SELECT * FROM jobs WHERE task_id=?",(task_id,)).fetchone()
        payload = db.dump({"resume":decision.model_dump()})
        if old_job and old_job["status"] == "running": raise HTTPException(409,"上一步仍在收尾，请稍后提交")
        if old_job:
            c.execute("UPDATE jobs SET status='queued',payload=?,error='' WHERE id=?",(payload,old_job["id"]))
        else:
            c.execute("INSERT INTO jobs(id,task_id,kind,status,payload,created_at) VALUES(?,?,?,?,?,?)",(db.uid(),task_id,"research","queued",payload,db.now()))
    return {"status":"queued"}


@app.post("/api/tasks/{task_id}/pause")
def pause(task_id: str):
    t = db.task(task_id)
    if t["status"] in ("completed","rejected","budget_exhausted"): raise HTTPException(409,"该任务已结束")
    with db.connect() as c:
        c.execute("UPDATE tasks SET paused=1,updated_at=? WHERE id=?",(db.now(),task_id))
        c.execute("UPDATE jobs SET status='paused' WHERE task_id=? AND status='queued'",(task_id,))
    return {"status":"pause_requested","message":"在当前计算或调用完成后暂停"}


@app.post("/api/tasks/{task_id}/resume")
def resume(task_id: str):
    t = db.task(task_id)
    if t["status"] in ("completed","rejected","budget_exhausted"): raise HTTPException(409,"该批次已结束，请创建新的研究批次")
    with db.connect() as c: c.execute("UPDATE tasks SET paused=0,error='' WHERE id=?",(task_id,))
    if t["gate"] is not None: return {"status":"awaiting_approval"}
    queue(task_id)
    return {"status":"queued"}


@app.get("/api/tasks/{task_id}/research-memory")
def memory(task_id: str,q: str=""):
    return research_memory(task_id,db.task(task_id)["config"]["literature_cutoff"],q)


@app.get("/api/tasks/{task_id}/export")
def export(task_id: str,format: str="json"):
    t = task_detail(task_id)
    t['model_archive']=decoded(db.rows('SELECT * FROM calls WHERE task_id=? ORDER BY created_at',(task_id,)))
    t['data_manifest']=manifest(t['config']['dataset_id'])
    ids={t['config'].get('model_profile_id'),t['config'].get('engineer_profile_id')}-{None}
    t['model_profiles']=[json.loads(db.one('SELECT payload FROM profiles WHERE id=?',(pid,))['payload']) for pid in ids]
    if format == "markdown":
        text = f"# {t['name']}\n\n模式：{'合成数据与脚本演示' if t['config']['mode']=='demo' else '真实研究'}\n\n状态：{t['status']}\n\n"
        text += "## 任务与数据口径\n\n```json\n"+db.dump(t["config"])+"\n```\n\n"
        for item in t["artifacts"]:
            if item["kind"] == "hypothesis":
                h = item["payload"]
                text += f"## {h['title']}\n\n{h['mechanism']}\n\n公式：`{h['formula']}`\n\n失效条件：{h['failure_condition']}\n\n修改：{h['change_reason']}\n\n"
        text += "## 全部实验（包括失败记录）\n\n"
        for e in t["experiments"] + t["final_results"]:
            p = e["payload"]
            text += f"- {e['hypothesis_id']} · {e['split']} · {e['status']}："
            text += (p.get("error") or f"60 日 Rank IC={p.get('ic',{}).get('60',{}).get('mean')}；年化收益={p.get('portfolio',{}).get('annual_return')}；最大回撤={p.get('portfolio',{}).get('max_drawdown')}")+"\n"
        text += "\n## 实证批判与改写\n\n"
        for a in t['artifacts']:
            p=a['payload']
            if a['kind']=='empirical_critique':
                text+=f"- {a['role']} 批判 {p.get('target_id')}：{p.get('issue')}；准备检验：{p.get('proposed_test','')}\n"
            elif a['kind']=='empirical_revision':
                text+=f"- 父版本 {p.get('parent_id')}：{p.get('rationale')}；预期：{'；'.join(p.get('expected_effects',[]))}\n"
            elif a['kind']=='research_feedback':
                text+=f"\n### {p['hypothesis_id']} 第 {p['round_no']} 轮反馈\n\n"
                for split in ('development','validation'):
                    s=p[split];m=s['metrics']
                    text+=f"- {split}：覆盖率={m.get('coverage')}；扣费年化超额={m.get('annual_excess')}；双倍成本超额={m.get('double_cost_annual_excess')}；缺失说明={s['missing_reasons']}\n"
                text+='\n父子变化：\n```json\n'+db.dump(p.get('changes',{}))+'\n```\n'
                for warning in p.get('similarity',{}).get('warnings',[]):
                    text+=f"- 相似性提醒：{warning['reference_id']}；相关程度={warning['max_similarity']}；参考公式 `{warning['hypothesis']['formula']}`\n"
            elif a['kind']=='candidate_selection':
                text+='\n## 全部候选筛选依据\n\n'
                for d in p['decisions']:
                    text+=f"- {', '.join(d['hypothesis_ids'])}：{d['selection_status']}；"+db.dump(d['reasons'])+'\n'
        text+='\n## 实验绑定与复用\n\n'
        for b in t['evaluation_bindings']:
            text+=f"- {b['hypothesis_id']} · {b['split']} → {b['experiment_id']}\n"
        text += "\n完整曲线、逐月数据、全部反馈和实验版本请导出 JSON 档案。计算完成不等于达到研究门槛，合成数据不构成真实投资证据。\n"
        return Response(text,media_type="text/markdown; charset=utf-8",headers={"Content-Disposition":f'attachment; filename="research-{task_id[:8]}.md"'})
    return Response(db.dump(t),media_type="application/json",headers={"Content-Disposition":f'attachment; filename="research-{task_id[:8]}.json"'})


@app.get("/api/documents")
def documents():
    return db.rows("SELECT id,title,url,publisher,published_date,version_date,verified,evidence_note,content_hash,created_at,length(text) text_length FROM documents ORDER BY created_at DESC")


@app.post("/api/documents")
def document_create(data: DocumentInput): return {"id":add_document(data)}


@app.post("/api/documents/search")
def search(data: SearchInput): return search_web(data.query,data.cutoff)


@app.get('/api/documents/{document_id}')
def read_document(document_id:str):
    row=db.one('SELECT * FROM documents WHERE id=?',(document_id,))
    if not row:raise HTTPException(404,'资料不存在')
    return {k:row[k] for k in DocumentInput.model_fields}


@app.get("/api/profiles")
def profiles():
    result = decoded(db.rows("SELECT * FROM profiles ORDER BY created_at"))
    for p in result: p["configured"] = bool(get_secret(p["payload"]["secret_name"]))
    return result


@app.post("/api/profiles")
def profile_create(p: ModelProfile):
    from urllib.parse import urlsplit
    u = urlsplit(p.base_url)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("模型接口必须为不含凭据或查询参数的 HTTPS 地址")
    profile_id = db.uid()
    with db.connect() as c: c.execute("INSERT INTO profiles VALUES(?,?,?)",(profile_id,db.dump(p.model_dump()),db.now()))
    return {"id":profile_id}


@app.get("/api/datasets")
def datasets():
    return [json.loads(x["manifest"]) for x in db.rows("SELECT manifest FROM datasets ORDER BY created_at DESC")]


@app.post("/api/datasets/import")
async def import_dataset(dataset_id: str=Form(...),source: str=Form(...),pit_verified: bool=Form(False),
                         financials: UploadFile=File(...),market: UploadFile=File(...)):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}",dataset_id): raise ValueError("数据集编号只能包含字母、数字、点、下划线和短横线")
    frames = []
    for file in [financials,market]:
        raw = await file.read(150_000_001)
        if len(raw)>150_000_000: raise ValueError("单文件超过 150 MB，请通过本机导入命令登记数据")
        frames.append(pd.read_parquet(io.BytesIO(raw)) if (file.filename or "").endswith(".parquet") else pd.read_csv(io.BytesIO(raw)))
    return register_dataset(dataset_id,*frames,source,pit_verified=pit_verified)


@app.get("/api/providers")
def providers():
    return {"providers":[{**x,"configured":bool(get_secret(x["secret_name"]))} for x in PROVIDERS],"tavily_configured":bool(get_secret("tavily"))}


class SampleInput(StrictModel):
    symbols: list[str] = Field(min_length=1,max_length=3)
    start: str="2020-01-01"
    end: str="2022-12-31"


@app.post("/api/providers/{provider}/sample")
def data_sample(provider: str,data: SampleInput):
    if provider not in {x["id"] for x in PROVIDERS}: raise ValueError("未知数据来源")
    if not get_secret(provider): raise ValueError("请先配置数据来源凭据")
    sample_id = db.uid()
    return {"job_id":queue(sample_id,"data_sample",{"provider":provider,**data.model_dump()})}


class BenchmarkInput(StrictModel):
    profile_ids: list[str]=Field(min_length=1,max_length=4)
    budget_cny: float=Field(gt=0,le=1000)


@app.post("/api/benchmarks/models")
def model_benchmark(data: BenchmarkInput):
    if len(set(data.profile_ids))!=len(data.profile_ids): raise ValueError("模型列表不能重复")
    from .models import profile
    for profile_id in data.profile_ids:
        p = profile(profile_id)
        if not get_secret(p.secret_name): raise ValueError("候选模型尚未配置系统凭据")
    benchmark_id = db.uid()
    return {"id":benchmark_id,"job_id":queue(benchmark_id,"model_benchmark",data.model_dump())}


@app.get("/api/benchmarks")
def benchmarks(): return decoded(db.rows("SELECT * FROM benchmarks ORDER BY created_at DESC"))


@app.get("/api/jobs")
def jobs(): return decoded(db.rows("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 30"))


@app.post("/api/tasks/{task_id}/comparison")
def comparison(task_id: str):
    original = db.task(task_id)
    task_ids=[]
    group=db.uid()
    reference_cutoff=db.now()
    for architecture,label in [("single","单 Agent"),("independent","双 Agent 独立"),("debate","双 Agent 辩论")]:
        config={**original["config"],"workflow_version":2,"architecture":architecture,"name":original["name"]+" · "+label}
        task=create_task(TaskConfig.model_validate(config))
        db.artifact(task["id"],"comparison","system","group",{"group_id":group,"source_task":task_id,"reference_cutoff":reference_cutoff,"budget_policy":"每组相同费用和实验上限；同时报告实际使用量"})
        task_ids.append(task["id"])
    return {"group_id":group,"task_ids":task_ids,"note":"每个对照任务等待第 1 环节确认，不会自动触发付费调用"}


@app.get('/api/comparisons')
def comparison_reports():
    groups={}
    for artifact in db.get_artifacts_by_kind('comparison'):
        group=artifact['payload']['group_id']
        t=db.task(artifact['task_id'])
        exps=decoded(db.rows("SELECT * FROM experiments WHERE task_id=? AND hypothesis_id NOT LIKE 'baseline:%'",(t['id'],)))
        valid=[e for e in exps if e['split']=='validation' and e['status']=='complete']
        means=[e['payload'].get('ic',{}).get('60',{}).get('mean') for e in valid]
        means=[x for x in means if x is not None]
        calls=db.rows('SELECT status,cost FROM calls WHERE task_id=?',(t['id'],))
        groups.setdefault(group,{'id':group,'groups':[],
            'note':'比较同预算上限下开发与验证阶段的全部实验。验证区参与过选择；历史最终测试不能在三组间顺次重复开封。本报告不声称已经证明辩论带来增益。'})['groups'].append({
                'task_id':t['id'],'name':t['name'],'architecture':t['config']['architecture'],'status':t['status'],
                'mode':t['config']['mode'],'budget_cny':t['config']['budget_cny'],'spent_cny':t['spent'],
                'versions':sum(e['split']=='validation' for e in exps),'failed_experiments':sum(e['status']=='failed' for e in exps),
                'mean_validation_ic':sum(means)/len(means) if means else None,
                'positive_validation_ratio':sum(x>0 for x in means)/len(means) if means else None,
                'model_calls':len(calls),'model_errors':sum(c['status']!='complete' for c in calls)})
    return list(groups.values())


class ObservationInput(StrictModel):
    dataset_id: str


@app.post('/api/tasks/{task_id}/observe')
def observe(task_id:str,data:ObservationInput):
    from .observation import frozen_batch
    frozen_batch(task_id)
    manifest(data.dataset_id)
    if db.task(task_id)['status']!='completed':raise HTTPException(409,'请先完成环节 10 的人工判断')
    job_key='observation-'+db.digest({'task':task_id,'dataset':data.dataset_id})
    return {'job_id':queue(job_key,'observation',{'task_id':task_id,'dataset_id':data.dataset_id})}


@app.get("/api/factors")
def factors():
    results=db.rows("SELECT f.*,t.name,t.config,a.payload FROM factors f JOIN tasks t ON t.id=f.task_id JOIN artifacts a ON a.id=f.hypothesis_id ORDER BY f.created_at DESC")
    return decoded(results)


static=ROOT / "frontend" / "dist"
if static.exists():
    app.mount("/assets",StaticFiles(directory=static / "assets"),name="assets")


@app.get("/{path:path}")
def frontend(path: str):
    if path.startswith("api/"): raise HTTPException(404,"接口不存在")
    if not (static/"index.html").exists():
        return Response("网页尚未构建，请执行安装与构建脚本。",status_code=503)
    return FileResponse(static/"index.html")
