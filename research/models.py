import json
import time
import httpx
from . import db
from .schemas import AgentOutput, ModelProfile
from .security import get_secret, public_url, redact

PROMPT_VERSION = "fundamental-research-2.0"


class BudgetExhausted(RuntimeError): pass
class UncertainCall(RuntimeError): pass


def profile(profile_id):
    record = db.one("SELECT payload FROM profiles WHERE id=?", (profile_id,))
    if not record: raise ValueError("尚未配置所选模型")
    return ModelProfile.model_validate_json(record["payload"])


def strict_schema(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            schema["additionalProperties"] = False
            schema["required"] = list(schema.get("properties", {}).keys())
        for v in schema.values(): strict_schema(v)
    elif isinstance(schema, list):
        for v in schema: strict_schema(v)
    return schema


def call_model(task_id, profile_id, stage, system, user):
    current = db.one("SELECT * FROM calls WHERE task_id=? AND stage=?", (task_id, stage))
    if current:
        if current["status"] == "complete": return AgentOutput.model_validate_json(current["response"])
        raise UncertainCall("该模型调用已登记但未完成。为避免重复扣费，自动流程已停止；请核对供应商账单并重新创建研究批次。")
    p = profile(profile_id)
    public_url(p.base_url)
    secret = get_secret(p.secret_name)
    if not secret: raise ValueError(f"模型 {p.name} 尚未配置系统凭据")
    prompt = db.dump({"system": system, "user": user})
    # UTF-8 bytes + envelope allowance is a conservative input-token reservation.
    reserve = ((len(prompt.encode("utf-8")) + 4096) * p.input_cny_per_million
               + p.max_output_tokens * p.output_cny_per_million) / 1e6
    call_id = db.uid()
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        t = c.execute("SELECT config,spent FROM tasks WHERE id=?", (task_id,)).fetchone()
        cfg = json.loads(t[0])
        reserved = c.execute("SELECT COALESCE(SUM(reserved),0) FROM calls WHERE task_id=? AND status IN ('requested','uncertain')", (task_id,)).fetchone()[0]
        if t[1] + reserved + reserve > cfg["budget_cny"]:
            raise BudgetExhausted("本批剩余额度不足以覆盖下一次调用，已保存研究记录并结束")
        c.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (call_id, task_id, profile_id, stage, "requested", db.digest(prompt), prompt, "", "{}", 0., reserve, "", db.now()))
    schema = strict_schema(AgentOutput.model_json_schema())
    if p.protocol == "responses":
        endpoint = p.base_url.rstrip("/") + "/responses"
        body = {"model": p.model, "store": False, "instructions": system,
                "input": [{"role": "user", "content": user}], "max_output_tokens": p.max_output_tokens,
                "text": {"format": {"type": "json_schema", "name": "research_output", "schema": schema, "strict": True}}}
    else:
        endpoint = p.base_url.rstrip("/") + "/chat/completions"
        body = {"model": p.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": p.max_output_tokens,
                "response_format": {"type": "json_schema", "json_schema": {"name": "research_output", "schema": schema, "strict": True}}}
    try:
        with httpx.Client(timeout=180, trust_env=False) as client:
            response = client.post(endpoint, headers={"Authorization": f"Bearer {secret}", "Idempotency-Key": call_id}, json=body)
        if response.status_code != 200:
            raise RuntimeError(f"模型请求返回 HTTP {response.status_code}；请检查所选模型是否支持结构化输出及额度")
        data = response.json()
        if p.protocol == "responses":
            text = "".join(c.get("text", "") for item in data.get("output", []) for c in item.get("content", []) if c.get("type") == "output_text")
            usage = data.get("usage", {})
            tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
        else:
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            tokens_in, tokens_out = usage.get("prompt_tokens"), usage.get("completion_tokens")
        clean_text = redact(text, [secret])
        actual = (tokens_in * p.input_cny_per_million + tokens_out * p.output_cny_per_million)/1e6 if tokens_in is not None and tokens_out is not None else reserve
        # Persist raw model text and cost even when schema validation subsequently fails.
        try:
            output = AgentOutput.model_validate_json(clean_text)
            status, error = "complete", ""
        except ValueError:
            output, status, error = None, "invalid", "模型输出不符合因子研究结构，费用已记录"
        with db.connect() as c:
            c.execute("UPDATE calls SET status=?,response=?,usage=?,cost=?,reserved=0,error=? WHERE id=?",
                      (status, clean_text, db.dump(usage), actual, error, call_id))
            c.execute("UPDATE tasks SET spent=spent+? WHERE id=?", (actual, task_id))
        if output is None: raise ValueError(error)
        return output
    except ValueError:
        raise
    except Exception as e:
        message = redact(str(e), [secret])[:800]
        with db.connect() as c:
            c.execute("UPDATE calls SET status='uncertain',error=? WHERE id=? AND status='requested'", (message, call_id))
        raise UncertainCall("连接或调用结果不确定；已保留费用预留并停止自动重试，避免重复扣费") from e
