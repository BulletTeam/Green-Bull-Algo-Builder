"""Green Bull API (FastAPI)। Flutter অ্যাপ এখানে ডাকবে।

চালানো (Termux-এ, backend ফোল্ডার থেকে):
  uvicorn app:app --host 127.0.0.1 --port 8000
"""
import datetime as dt
import threading

from fastapi import BackgroundTasks, FastAPI

import downloader
import store
from config import INSTR

app = FastAPI(title="Green Bull API")
_lock = threading.Lock()
_state = {"running": False, "last": None}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/status")
def status():
    c = store.conn()
    try:
        instruments = {code: store.summary(c, code) for code in INSTR}
    finally:
        c.close()
    return {"instruments": instruments, "update": _state}


@app.post("/update")
def update(background: BackgroundTasks):
    if _state["running"]:
        return {"started": False, "message": "আপডেট আগে থেকেই চলছে"}
    _state["running"] = True
    background.add_task(_run_update)
    return {"started": True, "message": "আপডেট শুরু হলো"}


def _run_update():
    try:
        with _lock:
            results = {}
            for code in INSTR:
                try:
                    results[code] = downloader.update_spot(code)
                except Exception as e:
                    results[code] = "ত্রুটি: %s" % e
            _state["last"] = {
                "at": dt.datetime.now().isoformat(timespec="seconds"),
                "results": results,
            }
    finally:
        _state["running"] = False
