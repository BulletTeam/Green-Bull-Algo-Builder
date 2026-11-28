# =====================================================================
#  সার্ভার
# =====================================================================

RUN_LOCK = threading.Lock()
JOBS = {}
JOB_N = [0]
DATA = {"D": None, "src": None}


def clean(o):
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def run_multi(payload, prog):
    """একই স্ট্র্যাটেজি একাধিক ইনডেক্সে পালা করে চালায় (প্রতিটির ডেটা লোড করে, চালিয়ে, ছেড়ে দেয়)"""
    global BASE_TF
    src = DATA.get("src")
    if not src or src.get("mode") != "db":
        raise ValueError("মাল্টি-ইনডেক্সের জন্য --db (SQLite) মোডে চালাতে হবে")
    insts = [str(x).upper() for x in payload.get("insts", [])]
    if not insts:
        raise ValueError("অন্তত একটা ইনডেক্স বাছুন")
    keep_base = BASE_TF
    rows = []
    try:
        for k, inst in enumerate(insts):
            row = {"inst": inst, "name": INSTR_NAMES.get(inst, inst)}
            try:
                D = load_db(src["db_dir"], inst, payload.get("from") or None, payload.get("to") or None, log=lambda *a: None)
                if not D.expiries:
                    row["note"] = "অপশন ডেটা নেই"
                else:
                    res = backtest(D, copy.deepcopy(payload["settings"]))
                    a = res["stats"]["all"]
                    row.update(n=a["n"], net=a["net"], pf=a["pf"], win=a["win"], dd=a["dd"], dd_pct=a["dd_pct"],
                               rec=a["rec"], exp=a["exp"], pm=a["pm"],
                               span="%s → %s" % (day_str(D.du[0]), day_str(D.du[-1])))
                del D
            except Exception as e:
                traceback.print_exc()
                row["note"] = "%s: %s" % (type(e).__name__, str(e)[:100])
            rows.append(row)
            prog((k + 1) / len(insts))
    finally:
        BASE_TF = keep_base
    return {"rows": rows}


