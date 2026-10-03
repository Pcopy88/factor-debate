import json
import re
from datetime import date
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup
from . import db
from .security import public_url, get_secret
from .schemas import DocumentInput


def fetch_source(url):
    for _ in range(4):
        public_url(url)
        with httpx.Client(timeout=25, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", url, headers={"User-Agent": "FundamentalLab/0.1 research-source-reader"}) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 12_000_000: raise ValueError("文档超过 12 MB，请提供较小的原文")
                mime = response.headers.get("content-type", "")
        if "pdf" in mime or bytes(raw).startswith(b"%PDF"):
            import pymupdf
            with pymupdf.open(stream=bytes(raw), filetype="pdf") as document:
                text = "\n".join(page.get_text() for page in document)
            if len(text.strip()) < 100:
                raise ValueError("这份 PDF 缺少可读文本；请提供可检索版本或核对后的文字")
            return {"url": url, "title": "", "text": text[:300000]}
        soup = BeautifulSoup(bytes(raw), "html.parser")
        for element in soup(["script", "style", "nav", "footer", "header", "form"]): element.decompose()
        return {"url": url, "title": soup.title.get_text(strip=True) if soup.title else "",
                "text": soup.get_text("\n", strip=True)[:300000]}
    raise ValueError("来源跳转过多")


def add_document(data: DocumentInput):
    if data.version_date < data.published_date:
        raise ValueError("版本日期不能早于发表日期")
    source = {"text": data.text, "url": data.url}
    if not data.text.strip(): source = fetch_source(data.url)
    elif not data.url.startswith("https://"):
        raise ValueError("资料必须提供可核查的 HTTPS 原文链接")
    if data.verified and (len(data.evidence_note.strip()) < 15 or len(source["text"].strip()) < 100):
        raise ValueError("标记核验完成时，需要记录原文版本日期证据（至少 15 字）和原文内容")
    record_id = db.uid()
    sha = db.digest(source["text"])
    with db.connect() as c:
        old = c.execute("SELECT * FROM documents WHERE url=? AND content_hash=?", (data.url, sha)).fetchone()
        if old:
            c.execute('INSERT INTO document_reviews VALUES(?,?,?,?)',(db.uid(),old['id'],db.dump(dict(old)),db.now()))
            c.execute('UPDATE documents SET title=?,publisher=?,published_date=?,version_date=?,verified=?,evidence_note=? WHERE id=?',
                      (data.title,data.publisher,str(data.published_date),str(data.version_date),int(data.verified),data.evidence_note,old['id']))
            return old['id']
        c.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (record_id, data.title, data.url, data.publisher, str(data.published_date), str(data.version_date),
                   source["text"], int(data.verified), data.evidence_note, sha, db.now()))
        c.execute("INSERT INTO documents_fts(id,title,text) VALUES(?,?,?)", (record_id,data.title,source["text"]))
    return record_id


def research_documents(cutoff, query="", limit=8):
    # Date conditions belong in the storage query, never in an LLM instruction only.
    clauses = "verified=1 AND published_date<=? AND version_date<=?"
    params = [str(cutoff), str(cutoff)]
    if query:
        clauses += " AND (title LIKE ? OR text LIKE ?)"
        params += [f"%{query}%", f"%{query}%"]
    result = db.rows(f"SELECT id,title,url,publisher,published_date,version_date,text,evidence_note FROM documents WHERE {clauses} ORDER BY version_date DESC LIMIT ?", (*params, limit))
    for item in result:
        item["excerpt"] = item.pop("text")[:4500]
    return result


def search_web(query, cutoff):
    secret = get_secret("tavily")
    if not secret: raise ValueError("尚未配置 Tavily 凭据；可先手动添加论文原文链接")
    with httpx.Client(timeout=30, trust_env=False) as client:
        response = client.post("https://api.tavily.com/search", json={
            "api_key": secret, "query": query + " 论文 或 证券研究报告 原文",
            "max_results": 6, "search_depth": "basic", "end_date": str(cutoff),
            "include_published_date": True, "filter_by_published_date": True,
            "include_answer": False, "include_raw_content": False,
        })
        if response.status_code != 200: raise ValueError(f"检索服务返回 HTTP {response.status_code}，请检查额度与连接")
        results = response.json().get("results", [])
    return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content", "")[:1800],
             "published_date": r.get("published_date"), "verified": False,
             "note": "检索结果只供发现来源；需核对原文、发布机构和版本后入库"} for r in results]


def research_memory(task_id, cutoff, query=""):
    if db.task(task_id)['config'].get('workflow_version',1)==2:
        snapshots=db.get_artifacts(task_id,'reference_snapshot')
        if not snapshots:
            return {'documents':research_documents(cutoff,query),'experiments':[],'references':[],'reference_count':0}
        from .similarity import read_snapshot
        snap=read_snapshot(snapshots[0]['id'])
        documents=[d for d in snap.get('documents',[]) if d['published_date']<=str(cutoff) and d['version_date']<=str(cutoff)
                   and (not query or query in d.get('text','') or query in d.get('title',''))]
        # Reference definitions enter model context only through the per-target
        # nearest-neighbor projection, never via this general memory channel.
        return {'documents':documents,'experiments':[],
                'references':[],'reference_count':len(snap['members']),
                'snapshot_hash':snap['snapshot_hash']}
    # Only development/validation experiments are in this database. Final DB is never opened here.
    history = []
    tasks = db.rows("SELECT id,config FROM tasks")
    mode = db.task(task_id)["config"]["mode"]
    eligible_tasks = {t["id"] for t in tasks if json.loads(t["config"])["validation_end"] <= str(cutoff)
                      and json.loads(t["config"])["mode"] == mode}
    for row in db.rows("SELECT task_id,hypothesis_id,payload,status,split,created_at FROM experiments WHERE split IN ('development','validation') ORDER BY created_at DESC LIMIT 100"):
        if row["task_id"] not in eligible_tasks: continue
        if row['task_id'] != task_id and row['created_at'] >= db.task(task_id)['created_at']: continue
        p = json.loads(row["payload"])
        if query and query not in p.get("formula", ""): continue
        history.append({"task_id": row["task_id"], "hypothesis_id": row["hypothesis_id"], "split": row["split"],
                        "status": row["status"], "formula": p.get("formula"), "mean_ic": p.get("ic", {}).get("60", {}).get("mean"),
                        "error": p.get("error"), "demo": p.get("demo"), "evidence_type": "开发区证据，非最终验证结论"})
    return {"documents": research_documents(cutoff, query), "experiments": history[:12]}
