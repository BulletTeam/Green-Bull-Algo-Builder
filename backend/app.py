"""Green Bull API (Starlette — FastAPI-র ভিত্তি; Termux-এ Rust ছাড়াই ইনস্টল হয়)।

চালানো (Termux-এ, backend ফোল্ডার থেকে):
  uvicorn app:app --host 127.0.0.1 --port 8000
"""
import datetime as dt
import os
import threading

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

import downloader
import store
from config import INSTR, OPT_DB_FILE

_state = {"running": False, "last": None}


def _opt_summary():
    """অপশন ডেটার সারাংশ। শুধু ছোট টেবিল পড়ে, তাই দ্রুত।"""
    if not os.path.exists(OPT_DB_FILE):
        return {}
    c = store.opt_conn()
    try:
        out = {}
        for code in INSTR:
            n, done, bars = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(s.done),0), COALESCE(SUM(s.rows),0) "
                "FROM contracts k LEFT JOIN ostate s ON s.cid=k.cid WHERE k.instr=?", (code,)).fetchone()
            out[code] = {"contracts": n, "done": int(done), "bars": int(bars)}
        return out
    finally:
        c.close()


async def health(request):
    return JSONResponse({"ok": True})


async def status(request):
    c = store.conn()
    try:
        spot = {code: store.summary(c, code) for code in INSTR}
    finally:
        c.close()
    return JSONResponse({"spot": spot, "options": _opt_summary(), "update": _state})


def _run_update():
    try:
        results = {}
        for code in INSTR:
            try:
                results[code] = downloader.update_spot(code)
            except Exception as e:
                results[code] = "ত্রুটি: %s" % e
        _state["last"] = {"at": dt.datetime.now().isoformat(timespec="seconds"), "results": results}
    finally:
        _state["running"] = False


async def update(request):
    if _state["running"]:
        return JSONResponse({"started": False, "message": "আপডেট আগে থেকেই চলছে"})
    _state["running"] = True
    threading.Thread(target=_run_update, daemon=True).start()
    return JSONResponse({"started": True, "message": "আপডেট শুরু হলো"})


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/status", status),
        Route("/update", update, methods=["POST"]),
    ],
    middleware=[Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])],
)
