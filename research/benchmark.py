import time
from . import db
from .schemas import TaskConfig
from .models import call_model, profile
from .agents import system_prompt
from .factors import canonical


def run_benchmark(benchmark_id,payload):
    results = []
    for profile_id in payload["profile_ids"]:
        p = profile(profile_id)
        task_id = f"benchmark-{benchmark_id}-{profile_id}"
        cfg = TaskConfig(name="模型试评",mode="live",model_profile_id=profile_id,budget_cny=payload["budget_cny"] / len(payload["profile_ids"]))
        with db.connect() as c:
            c.execute("INSERT OR IGNORE INTO tasks(id,name,config,status,gate,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                      (task_id,"模型试评 · "+p.name,db.dump(cfg.model_dump(mode="json")),"benchmarking",None,db.now(),db.now()))
        cases = [
            ("value","提出一个用经营现金流和总资产衡量现金创造质量的假设。引用只能使用 evidence-demo。该证据的内容：现金流和利润不同，应检查应计项目。"),
            ("growth","批判收入增速加速度因子。target_id=factor-demo。指出低基数问题并给出对照检验，不要假装已经回测。"),
            ("engineer","输出一个假设，公式必须准确等于 ttm(cfo) / total_assets；保留定义，不优化公式。"),
        ]
        row = {"profile_id":profile_id,"name":p.name,"model":p.model,"cases":[],"auto_checks_passed":0}
        started = time.monotonic()
        for i,(role,prompt) in enumerate(cases):
            try:
                answer = call_model(task_id,profile_id,f"benchmark:{i}",system_prompt(role),prompt)
                valid = all(set(h.evidence_ids) <= {"evidence-demo"} for h in answer.hypotheses)
                if i == 0: valid = valid and bool(answer.hypotheses) and all("cfo" in canonical(h.formula) for h in answer.hypotheses)
                elif i == 1: valid = valid and bool(answer.critiques) and all(x.target_id == "factor-demo" for x in answer.critiques)
                else: valid = valid and bool(answer.hypotheses) and canonical(answer.hypotheses[0].formula) == "ttm(cfo) / total_assets"
                row["cases"].append({"case":i+1,"passed":valid,"output":answer.model_dump()})
                row["auto_checks_passed"] += int(valid)
            except Exception as e:
                row["cases"].append({"case":i+1,"passed":False,"error":str(e)[:500]})
                break
        row["elapsed_seconds"] = round(time.monotonic()-started,2)
        row["cost_cny"] = db.task(task_id)["spent"]
        row["note"] = "自动检查只验证结构、引用 ID 和指定任务遵循；经济逻辑质量需查看原文人工比较。"
        results.append(row)
        with db.connect() as c: c.execute("UPDATE tasks SET status='benchmark_complete' WHERE id=?",(task_id,))
    result = {"id":benchmark_id,"type":"model_benchmark","results":results,"created_at":db.now()}
    with db.connect() as c: c.execute("INSERT OR REPLACE INTO benchmarks VALUES(?,?,?)",(benchmark_id,db.dump(result),db.now()))
    return result
