# =====================================================================
#  চার্ট বার ও ইন্ডিকেটর
# =====================================================================

class Bars:
    pass


def get_bars(D, tf):
    tf = int(tf)
    if tf in D._bars:
        return D._bars[tf]
    key = D.day * 1000 + (D.mod - OPEN_MIN) // tf
    ch = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    en = np.r_[ch[1:], len(key)]
    B = Bars()
    B.tf = tf
    B.o = D.o[ch]
    B.h = np.maximum.reduceat(D.h, ch)
    B.l = np.minimum.reduceat(D.l, ch)
    B.c = D.c[en - 1]
    B.v = np.add.reduceat(D.v, ch)
    B.t = D.t[ch]
    B.e = B.t + tf
    B.day = D.day[ch]
    B.mod = D.mod[ch]
    B.n = len(ch)
    D._bars[tf] = B
    return B


def shift_arr(a, k):
    k = int(k)
    if k <= 0:
        return a
    out = np.full(len(a), np.nan)
    if k < len(a):
        out[k:] = a[:-k]
    return out


def price_src(X, name):
    if name == "open":
        return X.o
    if name == "high":
        return X.h
    if name == "low":
        return X.l
    if name == "hl2":
        return (X.h + X.l) / 2
    if name == "hlc3":
        return (X.h + X.l + X.c) / 3
    if name == "ohlc4":
        return (X.o + X.h + X.l + X.c) / 4
    return X.c


def wma_calc(x, n):
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    n = int(n)
    if n < 1 or len(x) < n:
        return out
    w = np.arange(1, n + 1, dtype=float)
    win = np.lib.stride_tricks.sliding_window_view(x, n)
    out[n - 1:] = win @ w / w.sum()
    return out


def ma_calc(x, typ, n):
    n = max(int(n), 1)
    s = pd.Series(x)
    if typ == "ema":
        return s.ewm(span=n, adjust=False, min_periods=n).mean().to_numpy()
    if typ == "sma":
        return s.rolling(n).mean().to_numpy()
    if typ == "rma":
        return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()
    if typ == "wma":
        return wma_calc(x, n)
    if typ == "hma":
        raw = 2 * wma_calc(x, max(n // 2, 1)) - wma_calc(x, n)
        return wma_calc(raw, max(int(round(math.sqrt(n))), 1))
    raise ValueError("অজানা MA টাইপ: %s" % typ)


def rma_arr(x, n):
    return np.array(pd.Series(x).ewm(alpha=1.0 / max(int(n), 1), adjust=False, min_periods=int(n)).mean().to_numpy(), dtype=float)


def rsi_calc(x, n):
    s = pd.Series(x)
    d = s.diff()
    up = d.clip(lower=0)
    dn = (-d).clip(lower=0)
    ru = up.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()
    rd = dn.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        r = 100 - 100 / (1 + ru / rd)
    r = np.where((rd == 0) & (ru > 0), 100.0, r)
    r = np.where((rd == 0) & (ru == 0), 50.0, r)
    r[np.isnan(ru) | np.isnan(rd)] = np.nan
    return r


def tr_calc(h, l, c):
    pc = shift_arr(c, 1)
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    tr[0] = h[0] - l[0]
    return tr


def dmi_calc(h, l, c, n, sm):
    up = h - shift_arr(h, 1)
    dn = shift_arr(l, 1) - l
    up = np.nan_to_num(up)
    dn = np.nan_to_num(dn)
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = rma_arr(tr_calc(h, l, c), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        dip = 100 * rma_arr(pdm, n) / atr
        dim = 100 * rma_arr(mdm, n) / atr
        dx = 100 * np.abs(dip - dim) / (dip + dim)
    adx = rma_arr(np.nan_to_num(dx, nan=0.0), sm)
    adx[np.isnan(dip)] = np.nan
    return adx, dip, dim


def supertrend_calc(h, l, c, n, mult):
    atr = rma_arr(tr_calc(h, l, c), n)
    hl2 = (h + l) / 2
    ub = hl2 + mult * atr
    lb = hl2 - mult * atr
    N = len(c)
    st = np.full(N, np.nan)
    di = np.full(N, np.nan)
    fu = np.full(N, np.nan)
    fl = np.full(N, np.nan)
    d = 1
    started = False
    for i in range(N):
        if np.isnan(atr[i]):
            continue
        if not started:
            fu[i], fl[i] = ub[i], lb[i]
            d = 1
            started = True
        else:
            fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
            fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
            if d == 1 and c[i] < fl[i]:
                d = -1
            elif d == -1 and c[i] > fu[i]:
                d = 1
        st[i] = fl[i] if d == 1 else fu[i]
        di[i] = d
    return st, di


def tf_apply(D, B, tf_ind, fn):
    """ইন্ডিকেটর অন্য টাইমফ্রেমে গণনা করে চার্ট বারে বসায় (শুধু সম্পূর্ণ হওয়া HTF বার, রিপেইন্ট নেই)"""
    tf_ind = int(tf_ind or 0)
    if tf_ind <= B.tf:
        return fn(B)
    H = get_bars(D, tf_ind)
    res = fn(H)
    pos = np.searchsorted(H.e, B.e, side="right") - 1
    ok = pos >= 0
    out = {}
    for k, a in res.items():
        m = np.full(B.n, np.nan)
        m[ok] = a[pos[ok]]
        out[k] = m
    return out


SOURCES = [
    ("close", "Close", "দাম"), ("open", "Open", "দাম"), ("high", "High", "দাম"), ("low", "Low", "দাম"),
    ("body", "Candle Body (পয়েন্ট)", "ক্যান্ডেল"), ("bodyp", "Body % (রেঞ্জের)", "ক্যান্ডেল"),
    ("rng", "Candle Range (High-Low)", "ক্যান্ডেল"), ("uw", "Upper Wick (পয়েন্ট)", "ক্যান্ডেল"),
    ("lw", "Lower Wick (পয়েন্ট)", "ক্যান্ডেল"), ("uwp", "Upper Wick % (রেঞ্জের)", "ক্যান্ডেল"),
    ("lwp", "Lower Wick % (রেঞ্জের)", "ক্যান্ডেল"), ("cdir", "Candle রং (+1 সবুজ / -1 লাল)", "ক্যান্ডেল"),
    ("bodyatr", "Body ÷ ATR", "ক্যান্ডেল"),
    ("hl2", "HL2", "দাম"), ("hlc3", "HLC3 (Typical)", "দাম"), ("ohlc4", "OHLC4", "দাম"),
    ("number", "— সংখ্যা (Number) —", "মান"),
    ("volume", "Volume", "ভলিউম"), ("volsma", "Volume SMA", "ভলিউম"),
    ("minsopen", "Minutes Since Open (9:15 থেকে)", "সময়"),
    ("ma1", "MA 1", "ইন্ডিকেটর"), ("ma2", "MA 2", "ইন্ডিকেটর"), ("ma3", "MA 3", "ইন্ডিকেটর"),
    ("rsi", "RSI", "ইন্ডিকেটর"), ("adx", "ADX", "ইন্ডিকেটর"), ("dip", "DI+", "ইন্ডিকেটর"), ("dim", "DI-", "ইন্ডিকেটর"),
    ("atr", "ATR", "ইন্ডিকেটর"),
    ("st1", "Supertrend 1", "ইন্ডিকেটর"), ("st1dir", "Supertrend 1 Dir (+1 উপরে / -1 নিচে)", "ইন্ডিকেটর"),
    ("st2", "Supertrend 2", "ইন্ডিকেটর"), ("st2dir", "Supertrend 2 Dir (+1 / -1)", "ইন্ডিকেটর"),
    ("macd_l", "MACD Line", "ইন্ডিকেটর"), ("macd_s", "MACD Signal", "ইন্ডিকেটর"), ("macd_h", "MACD Histogram", "ইন্ডিকেটর"),
    ("vwap", "VWAP (দিনের শুরু থেকে)", "ইন্ডিকেটর"),
    ("bbup", "BB Upper", "ইন্ডিকেটর"), ("bbmid", "BB Middle", "ইন্ডিকেটর"), ("bblow", "BB Lower", "ইন্ডিকেটর"),
    ("bbw", "BB Width %", "ইন্ডিকেটর"),
    ("donhi", "Highest High (আগের N বার)", "ইন্ডিকেটর"), ("donlo", "Lowest Low (আগের N বার)", "ইন্ডিকেটর"),
    ("orbh", "ORB High", "ORB / দিন"), ("orbl", "ORB Low", "ORB / দিন"), ("orbm", "ORB Mid", "ORB / দিন"),
    ("orbr", "ORB Range (High-Low)", "ORB / দিন"),
    ("dayo", "Day Open", "ORB / দিন"), ("dayh", "Day High (এ পর্যন্ত)", "ORB / দিন"), ("dayl", "Day Low (এ পর্যন্ত)", "ORB / দিন"),
    ("pdh", "Prev Day High", "আগের দিন"), ("pdl", "Prev Day Low", "আগের দিন"), ("pdc", "Prev Day Close", "আগের দিন"),
    ("gap", "Gap % (Day Open vs Prev Close)", "আগের দিন"),
    ("pivp", "Pivot P", "Pivot / CPR"), ("r1", "Pivot R1", "Pivot / CPR"), ("r2", "Pivot R2", "Pivot / CPR"),
    ("r3", "Pivot R3", "Pivot / CPR"), ("s1", "Pivot S1", "Pivot / CPR"), ("s2", "Pivot S2", "Pivot / CPR"),
    ("s3", "Pivot S3", "Pivot / CPR"),
    ("cprt", "CPR Top", "Pivot / CPR"), ("cprb", "CPR Bottom", "Pivot / CPR"), ("cprw", "CPR Width %", "Pivot / CPR"),
    ("swh", "Swing High (নিশ্চিত, ৩ বার)", "প্রাইস অ্যাকশন / SMC"), ("swl", "Swing Low (নিশ্চিত, ৩ বার)", "প্রাইস অ্যাকশন / SMC"),
    ("bos", "BOS (+1 ওপরে ভাঙল / -1 নিচে)", "প্রাইস অ্যাকশন / SMC"), ("choch", "CHoCH (+1 বুলিশ / -1 বেয়ারিশ রিভার্সাল)", "প্রাইস অ্যাকশন / SMC"),
    ("fvgb", "বুলিশ FVG সাইজ % (এই বারে)", "প্রাইস অ্যাকশন / SMC"), ("fvgs", "বেয়ারিশ FVG সাইজ % (এই বারে)", "প্রাইস অ্যাকশন / SMC"),
    ("insb", "Inside Bar (1/0)", "প্রাইস অ্যাকশন / SMC"), ("nr7", "NR7 — ৭ বারের সবচেয়ে ছোট রেঞ্জ (1/0)", "প্রাইস অ্যাকশন / SMC"),
    ("rvol", "Relative Volume (Volume ÷ Vol SMA)", "প্রাইস অ্যাকশন / SMC"),
]
SRC_IDS = {s[0] for s in SOURCES}
OPS = [("cross_above", "Crosses Above"), ("cross_below", "Crosses Below"), ("above", "Is Above"),
       ("below", "Is Below"), ("rising", "Is Rising"), ("falling", "Is Falling")]


def compute_sources(D, tf, ind):
    key = json.dumps([tf, ind], sort_keys=True)
    if key in D._cache:
        return D._cache[key]
    B = get_bars(D, tf)
    n = B.n
    S = {"close": B.c, "open": B.o, "high": B.h, "low": B.l, "hl2": (B.h + B.l) / 2,
         "hlc3": (B.h + B.l + B.c) / 3, "ohlc4": (B.o + B.h + B.l + B.c) / 4, "volume": B.v,
         "minsopen": (B.mod - OPEN_MIN).astype(float)}
    rng_ = B.h - B.l
    top_, bot_ = np.maximum(B.o, B.c), np.minimum(B.o, B.c)
    safe_ = np.where(rng_ > 0, rng_, np.nan)
    S["body"], S["rng"], S["uw"], S["lw"] = np.abs(B.c - B.o), rng_, B.h - top_, bot_ - B.l
    S["bodyp"] = np.nan_to_num(S["body"] / safe_ * 100)
    S["uwp"] = np.nan_to_num(S["uw"] / safe_ * 100)
    S["lwp"] = np.nan_to_num(S["lw"] / safe_ * 100)
    S["cdir"] = np.sign(B.c - B.o)
    S["volsma"] = pd.Series(B.v).rolling(max(int(ind["vol"]["len"]), 1)).mean().to_numpy()

    for k in ("ma1", "ma2", "ma3"):
        p = ind[k]
        S[k] = tf_apply(D, B, p["tf"], lambda X, p=p: {"v": ma_calc(price_src(X, p["src"]), p["type"], p["len"])})["v"]
    p = ind["rsi"]
    S["rsi"] = tf_apply(D, B, p["tf"], lambda X: {"v": rsi_calc(price_src(X, p["src"]), int(p["len"]))})["v"]
    p = ind["adx"]
    r = tf_apply(D, B, p["tf"], lambda X: dict(zip(("adx", "dip", "dim"), dmi_calc(X.h, X.l, X.c, int(p["len"]), int(p["smooth"])))))
    S["adx"], S["dip"], S["dim"] = r["adx"], r["dip"], r["dim"]
    p = ind["atr"]
    S["atr"] = tf_apply(D, B, p["tf"], lambda X: {"v": rma_arr(tr_calc(X.h, X.l, X.c), int(p["len"]))})["v"]
    with np.errstate(all="ignore"):
        S["bodyatr"] = S["body"] / S["atr"]
    for k in ("st1", "st2"):
        p = ind[k]
        r = tf_apply(D, B, p["tf"], lambda X, p=p: dict(zip(("st", "dir"), supertrend_calc(X.h, X.l, X.c, int(p["len"]), float(p["mult"])))))
        S[k], S[k + "dir"] = r["st"], r["dir"]
    p = ind["macd"]

    def _macd(X):
        c = pd.Series(X.c)
        m = c.ewm(span=int(p["fast"]), adjust=False).mean() - c.ewm(span=int(p["slow"]), adjust=False).mean()
        s = m.ewm(span=int(p["sig"]), adjust=False).mean()
        return {"l": m.to_numpy(), "s": s.to_numpy(), "h": (m - s).to_numpy()}
    r = tf_apply(D, B, p["tf"], _macd)
    S["macd_l"], S["macd_s"], S["macd_h"] = r["l"], r["s"], r["h"]
    p = ind["bb"]

    def _bb(X):
        s = pd.Series(price_src(X, p["src"]))
        mid = s.rolling(int(p["len"])).mean()
        sd = s.rolling(int(p["len"])).std(ddof=0)
        up, lo = mid + float(p["mult"]) * sd, mid - float(p["mult"]) * sd
        with np.errstate(divide="ignore", invalid="ignore"):
            w = ((up - lo) / mid * 100)
        return {"up": up.to_numpy(), "mid": mid.to_numpy(), "low": lo.to_numpy(), "w": w.to_numpy()}
    r = tf_apply(D, B, p["tf"], _bb)
    S["bbup"], S["bbmid"], S["bblow"], S["bbw"] = r["up"], r["mid"], r["low"], r["w"]
    nn = max(int(ind["don"]["n"]), 1)
    S["donhi"] = pd.Series(B.h).rolling(nn).max().shift(1).to_numpy()
    S["donlo"] = pd.Series(B.l).rolling(nn).min().shift(1).to_numpy()

    # VWAP (ইনডেক্সে ভলিউম না থাকলে টাইপিক্যাল প্রাইসের গড়)
    tp = (B.h + B.l + B.c) / 3
    df = pd.DataFrame({"d": B.day, "pv": tp * B.v, "v": B.v, "tp": tp})
    g = df.groupby("d")
    cv, cpv = g["v"].cumsum().to_numpy(), g["pv"].cumsum().to_numpy()
    cnt, ctp = (g.cumcount() + 1).to_numpy(), g["tp"].cumsum().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        S["vwap"] = np.where(cv > 0, cpv / np.where(cv > 0, cv, 1), ctp / cnt)

    # দিনের লেভেল
    S["dayo"] = pd.Series(B.o).groupby(B.day).transform("first").to_numpy()
    S["dayh"] = pd.Series(B.h).groupby(B.day).cummax().to_numpy()
    S["dayl"] = pd.Series(B.l).groupby(B.day).cummin().to_numpy()
    dpos = np.searchsorted(D.du, B.day)
    pH = np.r_[np.nan, D.dH[:-1]][dpos]
    pL = np.r_[np.nan, D.dL[:-1]][dpos]
    pC = np.r_[np.nan, D.dC[:-1]][dpos]
    S["pdh"], S["pdl"], S["pdc"] = pH, pL, pC
    with np.errstate(divide="ignore", invalid="ignore"):
        S["gap"] = (S["dayo"] - pC) / pC * 100
        P = (pH + pL + pC) / 3
        S["pivp"] = P
        S["r1"], S["s1"] = 2 * P - pL, 2 * P - pH
        S["r2"], S["s2"] = P + (pH - pL), P - (pH - pL)
        S["r3"], S["s3"] = pH + 2 * (P - pL), pL - 2 * (pH - P)
        bc = (pH + pL) / 2
        tc = 2 * P - bc
        S["cprt"], S["cprb"] = np.maximum(tc, bc), np.minimum(tc, bc)
        S["cprw"] = (S["cprt"] - S["cprb"]) / P * 100

    # ORB
    mins = int(ind["orb"]["mins"])
    m = D.mod < OPEN_MIN + mins
    if m.any():
        odf = pd.DataFrame({"d": D.day[m], "h": D.h[m], "l": D.l[m]}).groupby("d").agg(h=("h", "max"), l=("l", "min"))
        orh = pd.Series(B.day).map(odf["h"]).to_numpy(float)
        orl = pd.Series(B.day).map(odf["l"]).to_numpy(float)
    else:
        orh = np.full(n, np.nan)
        orl = np.full(n, np.nan)
    valid = B.mod >= OPEN_MIN + mins
    orh = np.where(valid, orh, np.nan)
    orl = np.where(valid, orl, np.nan)
    S["orbh"], S["orbl"], S["orbm"], S["orbr"] = orh, orl, (orh + orl) / 2, orh - orl

    # ---- প্রাইস অ্যাকশন / SMC (কোনো রিপেইন্ট নেই: সুইং ৩ বার পরে নিশ্চিত হয়, দিন-ভিত্তিক রিসেট) ----
    NSW = 3
    swh = np.full(n, np.nan)
    swl = np.full(n, np.nan)
    bos = np.zeros(n)
    choch = np.zeros(n)
    fvgb = np.zeros(n)
    fvgs = np.zeros(n)
    insb = np.zeros(n)
    nr7 = np.zeros(n)
    day_ = B.day
    hi_, lo_, cl_ = B.h, B.l, B.c
    cur_h = cur_l = np.nan
    trend = 0
    used_h = used_l = False
    for i in range(n):
        if i == 0 or day_[i] != day_[i - 1]:
            cur_h = cur_l = np.nan
            trend = 0
            used_h = used_l = False
            d0 = i
        k = i - NSW                                  # k বারের সুইং এখন নিশ্চিত
        if k - NSW >= d0:
            if hi_[k] == np.max(hi_[k - NSW:i + 1]) and hi_[k] > np.max(hi_[k - NSW:k]):
                cur_h, used_h = hi_[k], False
            if lo_[k] == np.min(lo_[k - NSW:i + 1]) and lo_[k] < np.min(lo_[k - NSW:k]):
                cur_l, used_l = lo_[k], False
        swh[i], swl[i] = cur_h, cur_l
        if not used_h and np.isfinite(cur_h) and cl_[i] > cur_h:
            bos[i] = 1
            used_h = True
            if trend == -1:
                choch[i] = 1
            trend = 1
        elif not used_l and np.isfinite(cur_l) and cl_[i] < cur_l:
            bos[i] = -1
            used_l = True
            if trend == 1:
                choch[i] = -1
            trend = -1
        if i >= d0 + 2:
            if lo_[i] > hi_[i - 2]:
                fvgb[i] = (lo_[i] - hi_[i - 2]) / cl_[i] * 100
            if hi_[i] < lo_[i - 2]:
                fvgs[i] = (lo_[i - 2] - hi_[i]) / cl_[i] * 100
        if i > d0 and hi_[i] < hi_[i - 1] and lo_[i] > lo_[i - 1]:
            insb[i] = 1
        if i >= d0 + 6 and (hi_[i] - lo_[i]) <= np.min(hi_[i - 6:i + 1] - lo_[i - 6:i + 1]) + 1e-12:
            nr7[i] = 1
    S["swh"], S["swl"], S["bos"], S["choch"], S["fvgb"], S["fvgs"], S["insb"], S["nr7"] = swh, swl, bos, choch, fvgb, fvgs, insb, nr7
    with np.errstate(all="ignore"):
        S["rvol"] = B.v / S["volsma"]

    if len(D._cache) >= 6:
        D._cache.pop(next(iter(D._cache)))
    D._cache[key] = (S, B)
    return S, B


