"""Small live samples only. Raw vendor data must pass PIT/market-state acceptance before admission."""
import importlib.util
import json
import httpx
import pandas as pd
from . import db
from .config import path_for
from .security import get_secret

PROVIDERS = [
    {"id":"ricequant","name":"米筐 RQData","description":"优先验收历史时点财务数据与原始版本", "secret_name":"ricequant",
     "docs":"https://www.ricequant.com/doc/rqdata/python/stock-mod", "pricing":"向供应商询价；尚未确认套餐、覆盖与费用"},
    {"id":"tushare","name":"Tushare Pro","description":"第二候选，需核对公告日与调整前后报表", "secret_name":"tushare",
     "docs":"https://tushare.pro/document/2?doc_id=33", "pricing":"普通财务接口 2000 积分起，部分批量接口 5000 积分；积分非人民币"},
]


def sample(provider, symbols, start="2020-01-01", end="2022-12-31"):
    if len(symbols) > 3 or not symbols: raise ValueError("每次试样限定 1—3 只股票")
    secret = get_secret(provider)
    if not secret: raise ValueError("请先通过本机凭据工具配置所选数据来源")
    outputs = {}
    if provider == "tushare":
        with httpx.Client(timeout=40, trust_env=False) as client:
            for symbol in symbols:
                for api in ["income","balancesheet","cashflow"]:
                    r = client.post("https://api.tushare.pro", json={"api_name":api,"token":secret,
                         "params":{"ts_code":symbol,"start_date":start.replace("-",""),"end_date":end.replace("-","")},"fields":""})
                    r.raise_for_status()
                    body = r.json()
                    if body.get("code") != 0: raise ValueError("Tushare 拒绝本次请求，请核对积分、接口权限及日期范围")
                    data = body.get("data") or {}
                    outputs[f"{symbol}-{api}"] = pd.DataFrame(data.get("items",[]),columns=data.get("fields",[]))
    elif provider == "ricequant":
        if not importlib.util.find_spec("rqdatac"):
            raise ValueError("尚未安装米筐 SDK；按使用说明安装 rqdatac 后试样")
        import rqdatac
        rqdatac.init("license",secret)
        for symbol in symbols:
            frame = rqdatac.get_pit_financials_ex(symbol,
                    ["operating_revenue","net_profit_parent_company","total_assets"],
                    str(pd.Period(start,freq="Q")).lower(),str(pd.Period(end,freq="Q")).lower(),date=end,statements="all")
            outputs[symbol+"-pit"] = frame.reset_index()
    else: raise ValueError("未知数据来源")
    sample_id = db.uid()
    result = {"id":sample_id,"provider":provider,"symbols":symbols,"period":[start,end],"status":"sampled",
              "pit_verified":False,"created_at":db.now(),"tables":[],
              "acceptance_required":["与原公告抽查字段、单位和实际披露日期","确认更正前后版本完整","核实单季度或年内累计口径",
                                     "补齐历史行业、ST、退市与成交限制后才能登记正式数据集"]}
    for name, frame in outputs.items():
        if frame.empty: continue
        safe_name = name.replace("/","_").replace("\\","_")
        file = path_for("samples",sample_id,safe_name+".parquet")
        frame.to_parquet(file,index=False)
        result["tables"].append({"name":name,"rows":len(frame),"columns":list(frame.columns),
                                  "preview":json.loads(frame.head(5).to_json(orient="records",date_format="iso"))})
    if not result["tables"]: raise ValueError("供应商未返回试样数据，不能认定连接或覆盖合格")
    with db.connect() as c: c.execute("INSERT INTO benchmarks VALUES(?,?,?)",(sample_id,db.dump(result),db.now()))
    return result

