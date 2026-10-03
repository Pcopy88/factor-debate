import json
import os
import threading
import time
from . import db
from .config import path_for
from .workflow import run_workflow, Paused
from .models import BudgetExhausted
from .security import redact


def claim():
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row: return None
        c.execute("UPDATE jobs SET status='running',owner=?,heartbeat=? WHERE id=?",(str(os.getpid()),db.now(),row["id"]))
        return dict(row)


def execute(job):
    done = threading.Event()
    def heartbeat():
        while not done.wait(10):
            with db.connect() as c:
                c.execute("UPDATE jobs SET heartbeat=? WHERE id=?",(db.now(),job["id"]))
                c.execute("INSERT OR REPLACE INTO settings VALUES('worker_heartbeat',?)",(db.now(),))
    thread = threading.Thread(target=heartbeat,daemon=True)
    thread.start()
    task_id = job["task_id"]
    try:
        payload = json.loads(job["payload"] or "{}")
        if job["kind"] == "research": run_workflow(task_id,payload.get("resume"))
        elif job["kind"] == "data_sample":
            from .providers import sample
            result = sample(**payload)
            with db.connect() as c: c.execute("UPDATE jobs SET payload=? WHERE id=?",(db.dump({"result_id":result["id"]}),job["id"]))
        elif job["kind"] == "model_benchmark":
            from .benchmark import run_benchmark
            run_benchmark(task_id,payload)
        elif job['kind'] == 'observation':
            from .observation import run_forward
            result=run_forward(**payload)
            with db.connect() as c: c.execute("UPDATE jobs SET payload=? WHERE id=?",(db.dump({**payload,'result':result}),job['id']))
        with db.connect() as c: c.execute("UPDATE jobs SET status='complete',heartbeat=? WHERE id=?",(db.now(),job["id"]))
    except Paused:
        with db.connect() as c:
            c.execute("UPDATE tasks SET status='paused',updated_at=? WHERE id=?",(db.now(),task_id))
            c.execute("UPDATE jobs SET status='paused' WHERE id=?",(job["id"],))
    except BudgetExhausted as e:
        with db.connect() as c:
            c.execute("UPDATE tasks SET status='budget_exhausted',error=?,updated_at=? WHERE id=?",(str(e),db.now(),task_id))
            c.execute("UPDATE jobs SET status='complete',error=? WHERE id=?",(str(e),job["id"]))
        db.event(task_id,"budget",str(e))
    except Exception as e:
        message = redact(f"{type(e).__name__}: {e}")[:1200]
        with db.connect() as c:
            c.execute("UPDATE tasks SET status='needs_attention',error=?,updated_at=? WHERE id=?",(message,db.now(),task_id))
            c.execute("UPDATE jobs SET status='failed',error=? WHERE id=?",(message,job["id"]))
        db.event(task_id,"error",message)
    finally:
        done.set()
        thread.join(timeout=1)


def run():
    db.initialize()
    # One OS-locked worker owns the queue. Releasing the lock proves a previous worker stopped.
    lock = open(path_for("worker.lock"),"a+b")
    lock.seek(0)
    if os.name == "nt":
        import msvcrt
        if lock.read(1) == b"": lock.write(b"0"); lock.flush()
        lock.seek(0)
        try: msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError: return
    else:
        import fcntl
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError: return
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='queued',owner=NULL WHERE status='running'")
    try:
        while True:
            with db.connect() as c:
                c.execute("INSERT OR REPLACE INTO settings VALUES('worker_heartbeat',?)",(db.now(),))
            job = claim()
            if job: execute(job)
            else: time.sleep(1)
    finally:
        lock.close()


if __name__ == "__main__": run()
