# =====================================================================
#  ডেটা লোড
# =====================================================================


class Data:
    pass


def _to_min(series):
    d = pd.to_datetime(series)
    if getattr(d.dt, "tz", None) is not None:
        d = d.dt.tz_localize(None)
    return ((d - EPOCH) // pd.Timedelta(minutes=1)).to_numpy().astype("int64")


def _to_days(series):
    d = pd.to_datetime(series)
    if getattr(d.dt, "tz", None) is not None:
        d = d.dt.tz_localize(None)
    return ((d - EPOCH) // pd.Timedelta(days=1)).to_numpy().astype("int64")


def day_str(d):
    return dt.date.fromordinal(719163 + int(d)).isoformat()


def day_int(s):
    return dt.date.fromisoformat(s).toordinal() - 719163


def hm_str(m):
    m = int(m) % 1440
    return "%02d:%02d" % (m // 60, m % 60)


def hm(s):
    a, b = str(s).strip().split(":")
    return int(a) * 60 + int(b)


def _spot_finish(D, log, view=False):
    """D.t/o/h/l/c/v ভরা থাকলে বাকি স্পট-হিসাব (দিন, BASE_TF, দৈনিক OHLC) বানায়"""
    D.day = D.t // 1440
    D.mod = D.t % 1440
    global BASE_TF
    if not view:
        dd = np.diff(D.t)[np.diff(D.day) == 0]
        BASE_TF = int(np.median(dd)) if len(dd) else 5
    D.base = BASE_TF
    D.du = np.unique(D.day)
    st = np.flatnonzero(np.r_[True, D.day[1:] != D.day[:-1]])
    en = np.r_[st[1:], len(D.day)]
    D.dO = D.o[st]
    D.dH = np.maximum.reduceat(D.h, st)
    D.dL = np.minimum.reduceat(D.l, st)
    D.dC = D.c[en - 1]
    log("স্পট ডেটা: %d ক্যান্ডেল, %d দিন (%s → %s)" % (len(D.t), len(D.du), day_str(D.du[0]), day_str(D.du[-1])))


def _opt_finish(D, log):
    for (ex, typ, stk) in D.opt:
        D.strikes.setdefault((ex, typ), set()).add(stk)
    D.strikes = {k: np.array(sorted(v), dtype="float64") for k, v in D.strikes.items()}
    D.expiries = sorted(D.exp_range.keys())
    log("অপশন ডেটা: %d এক্সপায়ারি, %d কন্ট্র্যাক্ট" % (len(D.expiries), len(D.opt)))


def load_data(spot_csv, opt_dir, log=print):
    D = Data()
    D._bars = {}
    D._cache = {}
    sp = pd.read_csv(spot_csv)
    sp.columns = [c.strip().lower() for c in sp.columns]
    for c in ["datetime", "open", "high", "low", "close"]:
        if c not in sp.columns:
            raise ValueError("স্পট CSV-তে '%s' কলাম নেই" % c)
    if "volume" not in sp.columns:
        sp["volume"] = 0
    sp["t"] = _to_min(sp["datetime"])
    sp = sp.sort_values("t").drop_duplicates("t")
    mod = sp["t"] % 1440
    sp = sp[(mod >= OPEN_MIN) & (mod <= OPEN_MIN + 374)]
    D.t = sp["t"].to_numpy("int64")
    D.o = sp["open"].to_numpy("float64")
    D.h = sp["high"].to_numpy("float64")
    D.l = sp["low"].to_numpy("float64")
    D.c = sp["close"].to_numpy("float64")
    D.v = sp["volume"].fillna(0).to_numpy("float64")
    _spot_finish(D, log)

    D.opt = {}
    D.strikes = {}
    D.exp_lot = {}
    D.exp_range = {}
    D.expiries = []
    files = sorted(glob.glob(os.path.join(opt_dir, "*.csv.gz")) + glob.glob(os.path.join(opt_dir, "*.csv")))
    want = {"datetime", "expiry", "type", "strike", "lot", "open", "high", "low", "close"}
    for f in files:
        try:
            df = pd.read_csv(f, usecols=lambda c: c.strip().lower() in want)
        except Exception as e:
            log("  বাদ (পড়া যায়নি): %s %s" % (f, e))
            continue
        df.columns = [c.strip().lower() for c in df.columns]
        if df.empty:
            continue
        df["t"] = _to_min(df["datetime"])
        df["ex"] = _to_days(df["expiry"])
        df["strike"] = df["strike"].astype(float)
        for (ex, typ, stk), g in df.groupby(["ex", "type", "strike"], sort=False):
            g = g.sort_values("t").drop_duplicates("t")
            D.opt[(int(ex), str(typ), float(stk))] = (
                g["t"].to_numpy("int64"), g["open"].to_numpy("float64"),
                g["high"].to_numpy("float64"), g["low"].to_numpy("float64"),
                g["close"].to_numpy("float64"))
        for ex, g in df.groupby("ex"):
            ex = int(ex)
            D.exp_lot[ex] = int(g["lot"].mode().iloc[0])
            lo, hi = int(g["t"].min()), int(g["t"].max())
            if ex in D.exp_range:
                lo, hi = min(lo, D.exp_range[ex][0]), max(hi, D.exp_range[ex][1])
            D.exp_range[ex] = (lo, hi)
        log("  লোড: %s (%d সারি)" % (os.path.basename(f), len(df)))
    _opt_finish(D, log)
    return D


INSTR_NAMES = {"BN": "BankNifty", "N50": "Nifty 50", "FIN": "FinNifty", "SEN": "Sensex"}


def _load_db_raw(db_dir, inst, d0=None, d1=None, log=print):
    """নতুন SQLite ডেটা (gb.db স্পট + gb_opt.db অপশন) থেকে D বানায়। d0/d1 = 'YYYY-MM-DD' (ঐচ্ছিক)।
    ফাঁকা থাকলে সব ডেটা (সময় ও মেমোরি বেশি লাগে; দরকারে তারিখ সীমা দিন)।"""
    import sqlite3
    sp_path = os.path.join(db_dir, "gb.db")
    op_path = os.path.join(db_dir, "gb_opt.db")
    if not os.path.exists(sp_path):
        raise ValueError("gb.db পাওয়া যায়নি: %s" % sp_path)
    D = Data()
    D._bars = {}
    D._cache = {}
    con = sqlite3.connect(sp_path, timeout=30)
    try:
        first, last = con.execute("SELECT MIN(t), MAX(t) FROM bars WHERE instr=?", (inst,)).fetchone()
        if last is None:
            raise ValueError("%s-এর স্পট ডেটা gb.db-তে নেই" % inst)
        day1 = day_int(d1) if d1 else int(last // 1440)
        day0 = day_int(d0) if d0 else int(first // 1440)
        rows = con.execute("SELECT t,o,h,l,c,v FROM bars WHERE instr=? AND t>=? AND t<? ORDER BY t",
                           (inst, day0 * 1440, (day1 + 1) * 1440)).fetchall()
        try:
            fv = con.execute("SELECT t,v FROM fvol WHERE instr=? AND t>=? AND t<? ORDER BY t",
                             (inst, day0 * 1440, (day1 + 1) * 1440)).fetchall()
        except sqlite3.OperationalError:
            fv = []
    finally:
        con.close()
    if not rows:
        raise ValueError("%s: এই সময়ে স্পট ডেটা নেই (%s → %s)" % (inst, day_str(day0), day_str(day1)))
    a = np.array(rows, dtype="float64")
    t = a[:, 0].astype("int64")
    mod = t % 1440
    keep = (mod >= OPEN_MIN) & (mod <= OPEN_MIN + 374)
    D.t = t[keep]
    D.o, D.h, D.l, D.c, D.v = (a[keep, k].copy() for k in (1, 2, 3, 4, 5))
    if fv:      # স্পট ইনডেক্সে ভলিউম নেই, তাই নিকটতম মাসিক ফিউচারের ভলিউম বসাই
        fa = np.array(fv, dtype="float64")
        ft = fa[:, 0].astype("int64")
        idx = np.minimum(np.searchsorted(ft, D.t), len(ft) - 1)
        D.v = np.where(ft[idx] == D.t, fa[idx, 1], 0.0)
    _spot_finish(D, log)

    D.opt = {}
    D.strikes = {}
    D.exp_lot = {}
    D.exp_range = {}
    D.expiries = []
    if os.path.exists(op_path):
        oc = sqlite3.connect(op_path, timeout=30)
        try:
            ks = oc.execute("SELECT cid, expiry, otype, strike, lot FROM contracts WHERE instr=? AND expiry>=? AND expiry<=?",
                            (inst, day_str(day0), day_str(day1 + 45))).fetchall()
            lo_t, hi_t = day0 * 1440, (day1 + 1) * 1440
            for n, (cid, ex_s, typ, stk, lot) in enumerate(ks, 1):
                r = oc.execute("SELECT t,o,h,l,c FROM obars WHERE cid=? AND t>=? AND t<? ORDER BY t",
                               (cid, lo_t, hi_t)).fetchall()
                if not r:
                    continue
                b = np.array(r, dtype="int64")
                ex = day_int(ex_s)
                D.opt[(ex, str(typ), float(stk))] = (b[:, 0].copy(), b[:, 1] / 100.0, b[:, 2] / 100.0,
                                                    b[:, 3] / 100.0, b[:, 4] / 100.0)
                if lot:
                    D.exp_lot[ex] = int(lot)
                lo, hi = int(b[0, 0]), int(b[-1, 0])
                if ex in D.exp_range:
                    lo, hi = min(lo, D.exp_range[ex][0]), max(hi, D.exp_range[ex][1])
                D.exp_range[ex] = (lo, hi)
                if n % 300 == 0:
                    log("  অপশন লোড: %d/%d" % (n, len(ks)))
        finally:
            oc.close()
    _opt_finish(D, log)
    return D


def load_db(db_dir, inst, d0=None, d1=None, log=print):
    """SQLite থেকে লোড; প্রথমবারের পর ডিস্ক-ক্যাশ (pickle) থেকে কয়েক সেকেন্ডে। ডেটাবেস বদলালে (আপডেট) ক্যাশ নিজে বাতিল।"""
    import pickle, hashlib
    global BASE_TF
    try:
        sig = []
        for fn in ("gb.db", "gb_opt.db"):
            pth = os.path.join(db_dir, fn)
            stt = os.stat(pth) if os.path.exists(pth) else None
            sig.append("%s:%s:%s" % (fn, stt.st_size if stt else 0, int(stt.st_mtime) if stt else 0))
        key = hashlib.md5(("|".join(sig) + "|%s|%s|%s|v1" % (inst, d0, d1)).encode()).hexdigest()[:12]
        cdir = os.path.join(db_dir, "cache")
        cf = os.path.join(cdir, "D_%s_%s.pkl" % (inst, key))
    except Exception:
        return _load_db_raw(db_dir, inst, d0, d1, log)
    if os.path.exists(cf):
        try:
            t0 = time.time()
            with open(cf, "rb") as f:
                D = pickle.load(f)
            BASE_TF = int(D.base)
            D._bars, D._cache = {}, {}
            log("ক্যাশ থেকে লোড (%.1f সেকেন্ড): %s, %d দিন, %d কন্ট্র্যাক্ট" % (time.time() - t0, inst, len(D.du), len(D.opt)))
            return D
        except Exception as e:
            log("ক্যাশ পড়া গেল না (%s), নতুন করে লোড হচ্ছে" % e)
    D = _load_db_raw(db_dir, inst, d0, d1, log)
    try:
        os.makedirs(cdir, exist_ok=True)
        for old in glob.glob(os.path.join(cdir, "D_%s_*.pkl" % inst)):
            os.remove(old)
        D._bars, D._cache = {}, {}
        tmp = cf + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(D, f, protocol=5)
        os.replace(tmp, cf)
        log("ক্যাশ সেভ হয়েছে — পরেরবার দ্রুত খুলবে (%.0f MB)" % (os.path.getsize(cf) / 1e6))
    except Exception as e:
        log("ক্যাশ সেভ হয়নি: %s" % e)
    return D


def datasets_info(db_dir):
    """gb.db / gb_opt.db-তে কোন ইনডেক্সের কতটা ডেটা আছে (UI-র তালিকার জন্য)"""
    import sqlite3
    out = {}
    con = sqlite3.connect(os.path.join(db_dir, "gb.db"), timeout=30)
    try:
        for inst, t0, t1 in con.execute("SELECT instr, MIN(t), MAX(t) FROM bars GROUP BY instr"):
            out[inst] = {"inst": inst, "name": INSTR_NAMES.get(inst, inst), "spot_from": day_str(t0 // 1440),
                         "spot_to": day_str(t1 // 1440), "contracts": 0, "done": 0, "opt_from": "", "opt_to": ""}
    finally:
        con.close()
    op = os.path.join(db_dir, "gb_opt.db")
    if os.path.exists(op):
        oc = sqlite3.connect(op, timeout=30)
        try:
            q = ("SELECT k.instr, COUNT(*), COALESCE(SUM(s.done),0), MIN(k.expiry), MAX(k.expiry) "
                 "FROM contracts k LEFT JOIN ostate s ON s.cid=k.cid GROUP BY k.instr")
            for inst, n, done, e0, e1 in oc.execute(q):
                if inst in out:
                    out[inst].update(contracts=n, done=int(done), opt_from=e0, opt_to=e1)
        finally:
            oc.close()
    order = ["BN", "N50", "FIN", "SEN"]
    return sorted(out.values(), key=lambda d: order.index(d["inst"]) if d["inst"] in order else 9)


