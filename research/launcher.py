import os
import subprocess
import sys
import uvicorn
from .config import ROOT,path_for
from . import db


def main():
    db.initialize()
    log=open(path_for("worker.log"),"ab")
    subprocess.Popen([sys.executable,"-m","research.worker"],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                     creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
    print("因子研究室：http://127.0.0.1:8765")
    uvicorn.run("research.api:app",host="127.0.0.1",port=8765,access_log=False)


if __name__=="__main__":main()
