import contextlib
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from .config import path_for


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


@contextlib.contextmanager
def connect(final=False):
    con = sqlite3.connect(path_for("final.sqlite" if final else "research.sqlite"), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def initialize():
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT OR IGNORE INTO meta VALUES('schema_version','1');
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY,name TEXT,config TEXT,status TEXT,gate INTEGER,
          version INTEGER DEFAULT 1,created_at TEXT,updated_at TEXT,error TEXT DEFAULT '',
          paused INTEGER DEFAULT 0,spent REAL DEFAULT 0,frozen_hash TEXT,
          final_status TEXT DEFAULT 'unopened');
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,kind TEXT,message TEXT,
          payload TEXT,created_at TEXT,UNIQUE(task_id,kind,message,payload));
        CREATE TABLE IF NOT EXISTS artifacts(
          id TEXT PRIMARY KEY,task_id TEXT,kind TEXT,role TEXT,stage TEXT,
          parent_id TEXT,payload TEXT,created_at TEXT,
          UNIQUE(task_id,kind,role,stage));
        CREATE TABLE IF NOT EXISTS decisions(
          request_id TEXT PRIMARY KEY,task_id TEXT,gate INTEGER,action TEXT,
          version INTEGER,reason TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY,task_id TEXT UNIQUE,kind TEXT DEFAULT 'research',
          status TEXT,payload TEXT,owner TEXT,heartbeat TEXT,created_at TEXT,error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS experiments(
          id TEXT PRIMARY KEY,task_id TEXT,hypothesis_id TEXT,fingerprint TEXT,
          split TEXT,payload TEXT,status TEXT,created_at TEXT,
          UNIQUE(task_id,fingerprint,split));
        CREATE TABLE IF NOT EXISTS documents(
          id TEXT PRIMARY KEY,title TEXT,url TEXT,publisher TEXT,published_date TEXT,
          version_date TEXT,text TEXT,verified INTEGER,evidence_note TEXT,
          content_hash TEXT,created_at TEXT,UNIQUE(url,content_hash));
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(id UNINDEXED,title,text);
        CREATE TABLE IF NOT EXISTS document_reviews(id TEXT PRIMARY KEY,document_id TEXT,payload TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY,payload TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS calls(
          id TEXT PRIMARY KEY,task_id TEXT,profile_id TEXT,stage TEXT,status TEXT,
          request_hash TEXT,prompt TEXT,response TEXT,usage TEXT,cost REAL,
          reserved REAL,error TEXT,created_at TEXT,UNIQUE(task_id,stage));
        CREATE TABLE IF NOT EXISTS factors(
          id TEXT PRIMARY KEY,task_id TEXT,hypothesis_id TEXT,status TEXT,reason TEXT,
          created_at TEXT,UNIQUE(task_id,hypothesis_id));
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY,manifest TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS benchmarks(id TEXT PRIMARY KEY,payload TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS evaluation_bindings(
          task_id TEXT,hypothesis_id TEXT,split TEXT,experiment_id TEXT NOT NULL,
          PRIMARY KEY(task_id,hypothesis_id,split));
        CREATE TABLE IF NOT EXISTS research_steps(
          task_id TEXT,step_key TEXT,input_hash TEXT,status TEXT,payload TEXT,
          error TEXT DEFAULT '',created_at TEXT,updated_at TEXT,
          PRIMARY KEY(task_id,step_key));
        CREATE TABLE IF NOT EXISTS research_reference_factors(
          reference_id TEXT PRIMARY KEY,task_id TEXT,definition_hash TEXT,payload TEXT,
          content_hash TEXT,published_at TEXT,UNIQUE(task_id,definition_hash));
        UPDATE meta SET value='2' WHERE key='schema_version';
        ''')
    with connect(final=True) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS windows(
          window_key TEXT PRIMARY KEY,task_id TEXT,frozen_hash TEXT,opened_at TEXT);
        CREATE TABLE IF NOT EXISTS results(
          id TEXT PRIMARY KEY,task_id TEXT,hypothesis_id TEXT,fingerprint TEXT,
          split TEXT,payload TEXT,status TEXT,created_at TEXT,
          UNIQUE(task_id,fingerprint,split));
        ''')


def rows(sql, params=(), final=False):
    with connect(final) as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def one(sql, params=(), final=False):
    data = rows(sql, params, final)
    return data[0] if data else None


def task(task_id):
    result = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not result:
        raise KeyError("研究任务不存在")
    result["config"] = json.loads(result["config"])
    return result


def bind_evaluation(task_id, hypothesis_id, split, experiment_id):
    if split not in ('development','validation'):
        raise ValueError('研究绑定仅允许开发与验证区')
    experiment = one('SELECT task_id,split FROM experiments WHERE id=?',(experiment_id,))
    if not experiment or experiment['task_id'] != task_id or experiment['split'] != split:
        raise ValueError('实验不属于当前任务或分区')
    with connect() as c:
        old = c.execute('SELECT experiment_id FROM evaluation_bindings WHERE task_id=? AND hypothesis_id=? AND split=?',
                        (task_id,hypothesis_id,split)).fetchone()
        if old and old[0] != experiment_id:
            raise ValueError('不能覆盖已绑定的实验')
        c.execute('INSERT OR IGNORE INTO evaluation_bindings VALUES(?,?,?,?)',
                  (task_id,hypothesis_id,split,experiment_id))


def get_artifacts_by_kind(kind):
    items=rows('SELECT * FROM artifacts WHERE kind=? ORDER BY created_at',(kind,))
    for item in items:item['payload']=json.loads(item['payload'])
    return items


def event(task_id, kind, message, payload=None):
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO events(task_id,kind,message,payload,created_at) VALUES(?,?,?,?,?)",
                  (task_id, kind, message, dump(payload or {}), now()))


def artifact(task_id, kind, role, stage, payload, parent_id=None):
    with connect() as c:
        existing = c.execute("SELECT id FROM artifacts WHERE task_id=? AND kind=? AND role=? AND stage=?",
                             (task_id, kind, role, stage)).fetchone()
        if existing:
            return existing[0]
        artifact_id = uid()
        c.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
                  (artifact_id,task_id,kind,role,stage,parent_id,dump(payload),now()))
        return artifact_id


def get_artifacts(task_id, kind=None):
    sql = "SELECT * FROM artifacts WHERE task_id=?"
    params = [task_id]
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    result = rows(sql + " ORDER BY created_at,id", params)
    for r in result:
        r["payload"] = json.loads(r["payload"])
    return result
