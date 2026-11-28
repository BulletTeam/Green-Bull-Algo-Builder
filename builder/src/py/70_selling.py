# =====================================================================
#  অপশন সেলিং ল্যাব: Black-Scholes Greeks/IV, মাল্টি-লেগ বাস্কেট ইঞ্জিন, টেমপ্লেট, মার্জিন
# =====================================================================
_RF = 0.07


def _ncdf(x):
    x = np.asarray(x, float)
    t = 1.0 / (1.0 + 0.2316419 * np.abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    pdf = np.exp(-0.5 * x * x) / np.sqrt(2 * np.pi)
    c = 1.0 - pdf * poly
    return np.where(x >= 0, c, 1.0 - c)


def _npdf(x):
    return np.exp(-0.5 * np.asarray(x, float) ** 2) / np.sqrt(2 * np.pi)


def bs_price(S, K, T, sig, typ, r=_RF):
    S, K, T, sig = (np.asarray(a, float) for a in (S, K, T, sig))
    T = np.maximum(T, 1e-8)
    sig = np.maximum(sig, 1e-6)
    sq = sig * np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    if typ == "CE":
        return S * _ncdf(d1) - K * np.exp(-r * T) * _ncdf(d2)
    return K * np.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def bs_iv(price, S, K, T, typ, r=_RF):
    """ভেক্টরাইজড বাইসেকশন IV (বার্ষিক, দশমিকে, যেমন 0.14). অসম্ভব দাম হলে nan"""
    price, S, K, T = (np.asarray(a, float) for a in (price, S, K, T))
    price, S, K, T = np.broadcast_arrays(price, S, K, T)
    lo = np.full(price.shape, 0.005)
    hi = np.full(price.shape, 4.0)
    for _ in range(46):
        mid = 0.5 * (lo + hi)
        p = bs_price(S, K, T, mid, typ, r)
        up = p < price
        lo = np.where(up, mid, lo)
        hi = np.where(up, hi, mid)
    iv = 0.5 * (lo + hi)
    intr = np.maximum((S - K) if typ == "CE" else (K - S), 0.0)
    bad = (price <= 0) | (price < intr * 0.999) | (iv > 3.99) | (iv < 0.0051)
    return np.where(bad, np.nan, iv)


def bs_greeks(S, K, T, sig, typ, r=_RF):
    S, K, T, sig = (np.asarray(a, float) for a in (S, K, T, sig))
    T = np.maximum(T, 1e-8)
    sig = np.maximum(sig, 1e-6)
    sq = sig * np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    pdf = _npdf(d1)
    delta = _ncdf(d1) if typ == "CE" else _ncdf(d1) - 1.0
    gamma = pdf / (S * sq)
    vega = S * pdf * np.sqrt(T) / 100.0                    # প্রতি ১ ভোল-পয়েন্টে
    th = -S * pdf * sig / (2 * np.sqrt(T))
    th = th - r * K * np.exp(-r * T) * _ncdf(d2) if typ == "CE" else th + r * K * np.exp(-r * T) * _ncdf(-d2)
    return dict(delta=delta, gamma=gamma, vega=vega, theta=th / 365.0)      # theta: প্রতি ক্যালেন্ডার-দিন


def _T_years(ex, t):
    return max(ex * 1440 + 930 - int(t), 1) / 525600.0


def sell_templates():
    S = lambda a, o, m, v, lots=1, sl=0, role="core", exp=0: {"act": a, "opt": o, "strike": {"mode": m, "val": v}, "lots": lots, "sl": sl, "tp": 0, "role": role, "exp": exp}
    return {
        "short_straddle": {"name": "Short Straddle", "view": "নিউট্রাল / রেঞ্জ", "desc": "ATM CE + ATM PE বিক্রি। সর্বোচ্চ থিটা, তাই লেগ-SL ও SL-to-Cost জরুরি।",
                           "legs": [S("SELL", "CE", "atm", 0, sl=30), S("SELL", "PE", "atm", 0, sl=30)]},
        "short_strangle": {"name": "Short Strangle", "view": "নিউট্রাল (চওড়া রেঞ্জ)", "desc": "OTM CE + OTM PE বিক্রি (≈0.20Δ)। উচ্চ Win-rate, চওড়া ব্রেকইভেন।",
                           "legs": [S("SELL", "CE", "delta", 0.20, sl=30), S("SELL", "PE", "delta", 0.20, sl=30)]},
        "iron_condor": {"name": "Iron Condor", "view": "সীমিত ঝুঁকি, নিউট্রাল", "desc": "Short Strangle + দূরের বাই CE/PE হেজ। সর্বোচ্চ লস নির্দিষ্ট, মার্জিন কম।",
                        "legs": [S("BUY", "CE", "prem", 10, role="hedge"), S("BUY", "PE", "prem", 10, role="hedge"), S("SELL", "CE", "delta", 0.20, sl=30), S("SELL", "PE", "delta", 0.20, sl=30)]},
        "iron_butterfly": {"name": "Iron Butterfly", "view": "টাইট রেঞ্জ / এক্সপায়ারি", "desc": "ATM Straddle বিক্রি + ±৪ স্ট্রাইকে বাই উইং।",
                           "legs": [S("BUY", "CE", "atm", 6, role="hedge"), S("BUY", "PE", "atm", 6, role="hedge"), S("SELL", "CE", "atm", 0, sl=30), S("SELL", "PE", "atm", 0, sl=30)]},
        "bull_put": {"name": "Bull Put Spread", "view": "মৃদু বুলিশ", "desc": "OTM PE বিক্রি + আরও দূরের PE কেনা (ক্রেডিট স্প্রেড)।",
                     "legs": [S("BUY", "PE", "atm", 6, role="hedge"), S("SELL", "PE", "atm", 2, sl=35)]},
        "bear_call": {"name": "Bear Call Spread", "view": "মৃদু বেয়ারিশ", "desc": "OTM CE বিক্রি + আরও দূরের CE কেনা (ক্রেডিট স্প্রেড)।",
                      "legs": [S("BUY", "CE", "atm", 6, role="hedge"), S("SELL", "CE", "atm", 2, sl=35)]},
        "ratio_spread": {"name": "Call Ratio Spread (1×2)", "view": "আংশিক বুলিশ", "desc": "১× ATM CE কেনা + ২× OTM CE বিক্রি। ওপরে অসীম ঝুঁকি — সতর্ক।",
                         "legs": [S("BUY", "CE", "atm", 0, 1, role="hedge"), S("SELL", "CE", "atm", 3, 2, sl=40)]},
        "jade_lizard": {"name": "Jade Lizard", "view": "আংশিক বুলিশ", "desc": "OTM PE বিক্রি + (OTM CE বিক্রি + আরও দূরের CE কেনা) বেয়ার কল স্প্রেড।",
                        "legs": [S("BUY", "CE", "atm", 8, role="hedge"), S("SELL", "CE", "atm", 3, sl=35), S("SELL", "PE", "atm", 3, sl=35)]},
        "calendar": {"name": "Calendar Spread (CE)", "view": "লো IV / সময়-ভিত্তিক", "desc": "কাছের এক্সপায়ারিতে ATM CE বিক্রি + পরের এক্সপায়ারিতে ATM CE কেনা (ইন্ট্রাডে)।",
                     "legs": [S("BUY", "CE", "atm", 0, role="hedge", exp=1), S("SELL", "CE", "atm", 0, sl=35)]},
    }


def sell_defaults():
    return {
        "name": "Short Straddle 09:20", "template": "short_straddle",
        "legs": copy.deepcopy(sell_templates()["short_straddle"]["legs"]),
        "entry": {"time": "09:20", "min_dte": 0, "max_dte": 7, "dows": [0, 1, 2, 3, 4], "ivp_min": 0, "ivp_max": 100, "gap_max": 0},
        "exit": {"time": "15:15", "basket_sl": 0, "basket_tp": 0, "lock_at": 0, "lock_to": 0, "trail_step": 0, "trail_lock": 0},
        "rules": {"sl_to_cost": True, "reentry_max": 0, "reentry_mode": "asap", "adjust": False, "adj_ratio": 2.0, "adj_delta": 0.0, "adj_max": 2,
                  "auto_hedge": False, "hedge_prem": 10},
        "size": {"mode": "fixed", "lots": 1, "risk_pct": 1.0},
        "margin": {"pct": 13.0, "hedge_benefit": 1.0},
        "general": {"capital": 500000, "from": "", "to": "", "slip": 0.5, "brokerage": 20, "charges_pct": 0.15, "split": 70},
    }


def sell_norm(raw):
    d = sell_defaults()
    raw = raw or {}
    for k in ("entry", "exit", "rules", "size", "margin", "general"):
        d[k].update(raw.get(k) or {})
    for k in ("name", "template"):
        if raw.get(k):
            d[k] = str(raw[k])
    if raw.get("legs"):
        d["legs"] = []
        for lg in raw["legs"]:
            s = lg.get("strike") or {}
            d["legs"].append({"act": "SELL" if str(lg.get("act", "SELL")).upper() == "SELL" else "BUY",
                              "opt": "PE" if str(lg.get("opt", "CE")).upper() == "PE" else "CE",
                              "strike": {"mode": s.get("mode", "atm") if s.get("mode") in ("atm", "pts", "prem", "delta") else "atm", "val": float(s.get("val", 0) or 0)},
                              "lots": max(1, int(lg.get("lots", 1) or 1)), "sl": float(lg.get("sl", 0) or 0), "tp": float(lg.get("tp", 0) or 0),
                              "role": "hedge" if lg.get("role") == "hedge" else "core", "exp": int(lg.get("exp", 0) or 0)})
    if not d["legs"]:
        raise ValueError("কমপক্ষে একটি লেগ দিন")
    for k in ("min_dte", "max_dte", "ivp_min", "ivp_max", "gap_max"):
        d["entry"][k] = float(d["entry"][k])
    for k in ("basket_sl", "basket_tp", "lock_at", "lock_to", "trail_step", "trail_lock"):
        d["exit"][k] = float(d["exit"][k] or 0)
    if hm(d["entry"]["time"]) >= hm(d["exit"]["time"]):
        raise ValueError("এন্ট্রির সময় স্কোয়ার-অফের আগে হতে হবে")
    for k in ("capital", "slip", "brokerage", "charges_pct", "split"):
        d["general"][k] = float(d["general"][k])
    return d


def _bar_at(ct, t):
    """(o,h,l,c) বা None — ঠিক t সময়ের বার"""
    tt = ct[0]
    j = int(np.searchsorted(tt, t))
    if j < len(tt) and tt[j] == t:
        return float(ct[1][j]), float(ct[2][j]), float(ct[3][j]), float(ct[4][j])
    return None


def _spot_at(D, t):
    i = int(np.searchsorted(D.t, t, "right")) - 1
    return float(D.o[i]) if i >= 0 and D.t[i] == t else (float(D.c[i]) if i >= 0 else None)


def _find_ct(D, ex, typ, stk, te):
    ct = D.opt.get((ex, typ, float(stk)))
    if ct is None:
        return None
    j = int(np.searchsorted(ct[0], te))
    if j >= len(ct[0]) or ct[0][j] - te > 10:
        return None
    return ct, j


def _pick_strike(D, ex, typ, spec, te, spot, avoid_beyond=None):
    stk = D.strikes.get((ex, typ))
    if stk is None or not len(stk):
        return None
    mode, val = spec["mode"], float(spec["val"])
    atm = int(np.argmin(np.abs(stk - spot)))
    if mode == "atm":
        k = atm + (int(val) if typ == "CE" else -int(val))
        return float(stk[k]) if 0 <= k < len(stk) else None
    if mode == "pts":
        tgt = float(stk[atm]) + (val if typ == "CE" else -val)
        return float(stk[int(np.argmin(np.abs(stk - tgt)))])
    cand = []
    T = _T_years(ex, te)
    for s_ in stk[max(0, atm - 45): atm + 46]:
        r = _find_ct(D, ex, typ, s_, te)
        if r is None:
            continue
        pr = float(r[0][1][r[1]])
        if pr <= 0:
            continue
        cand.append((float(s_), pr))
    if not cand:
        return None
    if mode == "prem":
        return min(cand, key=lambda x: abs(x[1] - val))[0]
    # delta
    ks = np.array([c[0] for c in cand])
    ps = np.array([c[1] for c in cand])
    iv = bs_iv(ps, spot, ks, T, typ)
    dl = np.abs(bs_greeks(spot, ks, T, np.where(np.isfinite(iv), iv, 0.2), typ)["delta"])
    dl = np.where(np.isfinite(iv), dl, 9.0)
    return float(ks[int(np.argmin(np.abs(dl - val)))])


def sell_margin(legs, spot, cfg, lot):
    """আনুমানিক মার্জিন: নেকেড সেলিং = pct% × নোশনাল; হেজড হলে স্প্রেডের সর্বোচ্চ-লসে সীমাবদ্ধ।"""
    pct = float(cfg["margin"]["pct"]) / 100.0
    side = {}
    for lg in legs:
        q = lg["qty"] * (1 if lg["act"] == "BUY" else -1)
        side.setdefault(lg["opt"], []).append((lg["strike"], q, lg["entry"]))
    m = {}
    prem = 0.0
    for typ, ls in side.items():
        shorts = [(s, -q) for s, q, e in ls if q < 0]
        longs = [(s, q) for s, q, e in ls if q > 0]
        sq = sum(q for s, q in shorts)
        if sq <= 0:
            continue
        naked = pct * spot * sq
        lq = sum(q for s, q in longs)
        if lq >= sq and longs:
            # প্রতিটি শর্টের সাথে সবচেয়ে কাছের লং জোড়া; সর্বোচ্চ লস = Σ |স্ট্রাইক-ব্যবধান| × qty
            width = 0.0
            lst = sorted(longs)
            rem = [list(x) for x in lst]
            for s, q in sorted(shorts):
                need = q
                for r_ in sorted(rem, key=lambda z: abs(z[0] - s)):
                    take = min(need, r_[1])
                    if take > 0:
                        # CE: লং স্ট্রাইক ওপরে থাকলে সীমিত ঝুঁকি; নিচে থাকলে (ITM-বাই) ঝুঁকি নেই
                        w = (r_[0] - s) if typ == "CE" else (s - r_[0])
                        width += max(w, 0.0) * take
                        r_[1] -= take
                        need -= take
                    if need <= 0:
                        break
                if need > 0:
                    width += pct * spot * need
            m[typ] = min(naked, width + 0.01 * spot * sq) * float(cfg["margin"].get("hedge_benefit", 1.0))
        else:
            m[typ] = naked
    if not m:
        return 0.0
    v = sorted(m.values(), reverse=True)
    return float(v[0] + (0.3 * v[1] if len(v) > 1 else 0.0))


def _sell_day_setup(D, cfg, d, ex, te, auto_hedge=True):
    """এন্ট্রি-মুহূর্তে লেগগুলো বেছে নেয়। ফেরত: (legs, spot) বা (None, কারণ)"""
    spot = _spot_at(D, te)
    if spot is None:
        return None, "স্পট বার নেই"
    exps = D.expiries
    k0 = bisect.bisect_left(exps, ex)
    out = []
    core_short = []
    for lg in cfg["legs"]:
        k = k0 + int(lg["exp"])
        if k >= len(exps):
            return None, "পরের এক্সপায়ারি নেই"
        e2 = exps[k]
        stk = _pick_strike(D, e2, lg["opt"], lg["strike"], te, spot)
        if stk is None:
            return None, "স্ট্রাইক পাওয়া যায়নি"
        r = _find_ct(D, e2, lg["opt"], stk, te)
        if r is None:
            return None, "কন্ট্র্যাক্টে বার নেই"
        out.append({"act": lg["act"], "opt": lg["opt"], "strike": float(stk), "ex": e2, "lots": lg["lots"], "sl": lg["sl"], "tp": lg["tp"], "role": lg["role"], "ct": r[0],
                    "j0": r[1], "spec": lg["strike"]})
    rules = cfg["rules"]
    if rules.get("auto_hedge"):
        for typ in ("CE", "PE"):
            sh = [x for x in out if x["opt"] == typ and x["act"] == "SELL"]
            lo = [x for x in out if x["opt"] == typ and x["act"] == "BUY"]
            if sh and not lo:
                far = max(sh, key=lambda x: x["strike"]) if typ == "CE" else min(sh, key=lambda x: x["strike"])
                stks = D.strikes.get((far["ex"], typ))
                best = None
                for s_ in stks:
                    if (typ == "CE" and s_ <= far["strike"]) or (typ == "PE" and s_ >= far["strike"]):
                        continue
                    r = _find_ct(D, far["ex"], typ, s_, te)
                    if r is None:
                        continue
                    pr = float(r[0][1][r[1]])
                    if pr <= 0:
                        continue
                    df = abs(pr - float(rules["hedge_prem"]))
                    if best is None or df < best[0]:
                        best = (df, float(s_), r)
                if best:
                    out.append({"act": "BUY", "opt": typ, "strike": best[1], "ex": far["ex"], "lots": max(x["lots"] for x in sh), "sl": 0, "tp": 0, "role": "hedge",
                                "ct": best[2][0], "j0": best[2][1], "spec": {"mode": "prem", "val": float(rules["hedge_prem"])}})
    out.sort(key=lambda x: 0 if x["act"] == "BUY" else 1)       # হেজ আগে
    return out, spot


def sell_backtest(D, raw, progress=None, only_day=None):
    cfg = sell_norm(raw)
    G = cfg["general"]
    E, X, R = cfg["entry"], cfg["exit"], cfg["rules"]
    te_m, sq_m = hm(E["time"]), hm(X["time"])
    slip, brk, chg = G["slip"], G["brokerage"], G["charges_pct"]
    d_from = day_int(G["from"]) if G["from"] else -10 ** 9
    d_to = day_int(G["to"]) if G["to"] else 10 ** 9
    exps = D.expiries
    eligible = []
    for d in D.du.tolist():
        if d < d_from or d > d_to:
            continue
        if ((d + 3) % 7) not in [int(x) for x in E["dows"]]:
            continue
        k = bisect.bisect_left(exps, d + int(E["min_dte"]))
        if k >= len(exps):
            continue
        ex = exps[k]
        if ex - d > E["max_dte"]:
            continue
        lo, hi = D.exp_range[ex]
        if lo <= d * 1440 + 1439 and hi >= d * 1440:
            eligible.append((d, ex))
    if only_day is not None:
        eligible = [x for x in eligible if x[0] == only_day]
    # IV ইতিহাস (IVP ফিল্টার ও রিপোর্টের জন্য): প্রতিদিন এন্ট্রি-সময়ে ATM IV
    ivh = {}
    for d, ex in eligible:
        te = d * 1440 + te_m
        spot = _spot_at(D, te)
        stk = D.strikes.get((ex, "CE"))
        if spot is None or stk is None:
            continue
        s_ = float(stk[int(np.argmin(np.abs(stk - spot)))])
        r = _find_ct(D, ex, "CE", s_, te)
        rp = _find_ct(D, ex, "PE", s_, te)
        if r is None:
            continue
        T = _T_years(ex, te)
        v = bs_iv(float(r[0][1][r[1]]), spot, s_, T, "CE")
        if rp is not None:
            v2 = bs_iv(float(rp[0][1][rp[1]]), spot, s_, T, "PE")
            v = np.nanmean([v, v2]) if np.isfinite(v2) or np.isfinite(v) else v
        if np.isfinite(v):
            ivh[d] = float(v)
    ivd = sorted(ivh)
    trades = []
    skipped = {}
    sk = lambda w: skipped.__setitem__(w, skipped.get(w, 0) + 1)
    total = max(len(eligible), 1)
    prev_close = {}
    for di, (d, ex) in enumerate(eligible):
        if progress and di % 10 == 0:
            progress(di / total)
        te = d * 1440 + te_m
        sqt = d * 1440 + sq_m
        # IV-ফিল্টার
        iv_now = ivh.get(d)
        ivp = None
        if iv_now is not None:
            past = [ivh[x] for x in ivd if x < d][-60:]
            if len(past) >= 10:
                ivp = 100.0 * sum(1 for x in past if x <= iv_now) / len(past)
        if (E["ivp_min"] > 0 or E["ivp_max"] < 100) and (ivp is None or ivp < E["ivp_min"] or ivp > E["ivp_max"]):
            sk("IV ফিল্টার")
            continue
        if E["gap_max"] > 0:
            i0 = int(np.searchsorted(D.day, d))
            j0 = int(np.searchsorted(D.du, d))
            if j0 > 0 and i0 < len(D.o):
                gp = abs(D.o[i0] / D.dC[j0 - 1] - 1) * 100
                if gp > E["gap_max"]:
                    sk("গ্যাপ ফিল্টার")
                    continue
        legs0, spot = _sell_day_setup(D, cfg, d, ex, te)
        if legs0 is None:
            sk(spot)
            continue
        lot = D.exp_lot.get(ex, 1)
        # লট-সাইজিং
        lots = int(cfg["size"]["lots"])
        if cfg["size"]["mode"] == "risk" and X["basket_sl"] > 0:
            lots = max(1, int(G["capital"] * float(cfg["size"]["risk_pct"]) / 100.0 // X["basket_sl"]))
        mult = lots
        legs = []
        for i, lg in enumerate(legs0):
            side = -1 if lg["act"] == "SELL" else 1
            raw_e = float(lg["ct"][1][lg["j0"]])
            legs.append(dict(lg, side=side, qty=lg["lots"] * mult * lot, entry_raw=raw_e, entry=raw_e + side * slip, open=True, exit=None, exit_raw=None,
                             t_in=int(lg["ct"][0][lg["j0"]]), t_out=None, why=None, last=raw_e, sl_px=None, tp_px=None, orig=raw_e, re_left=int(R["reentry_max"])))
            L = legs[-1]
            if L["sl"] > 0:
                L["sl_px"] = raw_e * (1 + L["sl"] / 100.0) if L["act"] == "SELL" else max(raw_e * (1 - L["sl"] / 100.0), 0.05)
            if L["tp"] > 0:
                L["tp_px"] = raw_e * (1 - L["tp"] / 100.0) if L["act"] == "SELL" else raw_e * (1 + L["tp"] / 100.0)
        margin = sell_margin([dict(act=x["act"], opt=x["opt"], strike=x["strike"], qty=x["qty"], entry=x["entry_raw"]) for x in legs], spot, cfg, lot)
        sl_b, tp_b = X["basket_sl"] * mult, X["basket_tp"] * mult
        lock_at, lock_to = X["lock_at"] * mult, X["lock_to"] * mult
        tr_step, tr_lock = X["trail_step"] * mult, X["trail_lock"] * mult
        allt = np.unique(np.concatenate([x["ct"][0][(x["ct"][0] >= te) & (x["ct"][0] <= sqt)] for x in legs]))
        peak = -1e18
        low = 1e18
        pend = []
        nadj = 0
        done_reason = None
        events = []
        all_legs = list(legs)
        t_end = te
        for t in allt.tolist():
            t_end = t
            bars = {}
            for x in all_legs:
                if x["open"]:
                    b = _bar_at(x["ct"], t)
                    if b is not None:
                        bars[id(x)] = b
            stopped = []
            # পেন্ডিং রি-এন্ট্রি
            for pr in list(pend):
                x0 = pr["leg"]
                b = _bar_at(x0["ct"], t)
                if b is None or t <= pr["after"]:
                    continue
                go = pr["mode"] == "asap" or (pr["mode"] == "recost" and ((b[3] <= pr["cost"]) if x0["act"] == "SELL" else (b[3] >= pr["cost"])))
                if go:
                    side = x0["side"]
                    nl = dict(x0, entry_raw=b[0], entry=b[0] + side * slip, open=True, exit=None, exit_raw=None, t_in=t, t_out=None, why=None, last=b[0], re_left=pr["left"] - 1, reent=True)
                    if nl["sl"] > 0:
                        nl["sl_px"] = b[0] * (1 + nl["sl"] / 100.0) if nl["act"] == "SELL" else max(b[0] * (1 - nl["sl"] / 100.0), 0.05)
                    all_legs.append(nl)
                    bars[id(nl)] = b
                    pend.remove(pr)
                    events.append("%s রি-এন্ট্রি %s %s" % (hm_str(t % 1440), nl["opt"], int(nl["strike"])))
            # লেগ SL / TP
            for x in all_legs:
                if not x["open"] or id(x) not in bars:
                    continue
                o_, h_, l_, c_ = bars[id(x)]
                x["last"] = c_
                hit = None
                if x["act"] == "SELL":
                    if x["sl_px"] is not None and h_ >= x["sl_px"]:
                        hit = ("SL", max(x["sl_px"], o_))
                    elif x["tp_px"] is not None and l_ <= x["tp_px"]:
                        hit = ("TP", min(x["tp_px"], o_))
                else:
                    if x["sl_px"] is not None and l_ <= x["sl_px"]:
                        hit = ("SL", min(x["sl_px"], o_))
                    elif x["tp_px"] is not None and h_ >= x["tp_px"]:
                        hit = ("TP", max(x["tp_px"], o_))
                if hit:
                    x["open"] = False
                    x["exit_raw"] = hit[1]
                    x["exit"] = hit[1] - x["side"] * slip
                    x["t_out"] = t
                    x["why"] = "Leg " + hit[0]
                    stopped.append((x, hit[0]))
            for x, kind in stopped:
                events.append("%s %s %s %s" % (hm_str(t % 1440), x["opt"], int(x["strike"]), x["why"]))
                if kind == "SL":
                    if R["sl_to_cost"]:
                        for y in all_legs:
                            if y["open"] and y["role"] == "core" and y["act"] == "SELL" and y is not x:
                                y["sl_px"] = y["entry_raw"] if y["sl_px"] is None else min(y["sl_px"], y["entry_raw"])
                    if x["re_left"] > 0:
                        pend.append({"leg": x, "mode": R["reentry_mode"], "after": t, "left": x["re_left"], "cost": x["orig"]})
            # MTM
            open_l = [x for x in all_legs if x["open"]]
            mtm = 0.0
            for x in all_legs:
                px = x["exit"] if not x["open"] else x["last"]
                if not x["open"]:
                    mtm += (x["exit"] - x["entry"]) * x["side"] * x["qty"] * (-1 if False else 1)
                else:
                    mtm += (px - x["entry"]) * x["side"] * x["qty"]
            # side: SELL=-1 → (px-entry)*(-1) = entry-px  ✓.  BUY=+1 ✓
            peak, low = max(peak, mtm), min(low, mtm)
            if not open_l and not pend:
                done_reason = "সব লেগ SL/TP-তে"
                break
            if sl_b > 0 and mtm <= -sl_b:
                done_reason = "Basket SL"
            elif tp_b > 0 and mtm >= tp_b:
                done_reason = "Basket TP"
            else:
                lk = None
                if lock_at > 0 and peak >= lock_at:
                    lk = lock_to
                if tr_step > 0 and tr_lock > 0 and lock_at > 0 and peak >= lock_at:
                    lk = lock_to + int((peak - lock_at) // tr_step) * tr_lock
                if lk is not None and mtm <= lk:
                    done_reason = "Lock/Trail Profit"
            # অটো-অ্যাডজাস্টমেন্ট
            if done_reason is None and R["adjust"] and nadj < int(R["adj_max"]):
                sh = [x for x in open_l if x["act"] == "SELL" and x["role"] == "core"]
                ce = [x for x in sh if x["opt"] == "CE"]
                pe = [x for x in sh if x["opt"] == "PE"]
                if ce and pe:
                    pc, pp = ce[0]["last"], pe[0]["last"]
                    trig = False
                    if R["adj_ratio"] > 1 and (pc >= R["adj_ratio"] * pp or pp >= R["adj_ratio"] * pc):
                        trig = True
                    if R["adj_delta"] > 0 and not trig:
                        sp_ = _spot_at(D, t) or spot
                        nd = 0.0
                        for x in open_l:
                            T = _T_years(x["ex"], t)
                            iv = bs_iv(x["last"], sp_, x["strike"], T, x["opt"])
                            if np.isfinite(iv):
                                nd += float(bs_greeks(sp_, x["strike"], T, iv, x["opt"])["delta"]) * x["side"] * -1 * -1 * x["qty"] / lot / max(mult, 1)
                        trig = abs(nd) > R["adj_delta"]
                    if trig:
                        win = ce[0] if pc < pp else pe[0]            # লাভজনক (কম প্রিমিয়াম) লেগ
                        tgt = max(pc, pp)
                        sp_ = _spot_at(D, t) or spot
                        stk = D.strikes.get((win["ex"], win["opt"]))
                        best = None
                        for s_ in stk[max(0, int(np.argmin(np.abs(stk - sp_))) - 40): int(np.argmin(np.abs(stk - sp_))) + 41]:
                            r = _find_ct(D, win["ex"], win["opt"], s_, t)
                            if r is None or r[0][0][r[1]] != t:
                                continue
                            pr_ = float(r[0][1][r[1]])
                            if best is None or abs(pr_ - tgt) < best[0]:
                                best = (abs(pr_ - tgt), float(s_), r, pr_)
                        if best and best[1] != win["strike"]:
                            side = win["side"]
                            win["open"] = False
                            win["exit_raw"] = win["last"]
                            win["exit"] = win["last"] - side * slip
                            win["t_out"] = t
                            win["why"] = "Adjust (roll)"
                            nl = dict(win, strike=best[1], ct=best[2][0], entry_raw=best[3], entry=best[3] + side * slip, open=True, exit=None, exit_raw=None,
                                      t_in=t, t_out=None, why=None, last=best[3], reent=False, adj=True)
                            nl["sl_px"] = best[3] * (1 + nl["sl"] / 100.0) if nl["sl"] > 0 else None
                            all_legs.append(nl)
                            nadj += 1
                            events.append("%s অ্যাডজাস্ট: %s %d → %d" % (hm_str(t % 1440), win["opt"], int(win["strike"]), int(best[1])))
            if done_reason:
                break
        # শেষ: সেল লেগ আগে, হেজ পরে ক্লোজ (খরচের হিসাবে একই)
        for x in sorted([y for y in all_legs if y["open"]], key=lambda y: 0 if y["act"] == "SELL" else 1):
            x["open"] = False
            x["exit_raw"] = x["last"]
            x["exit"] = x["last"] - x["side"] * slip
            x["t_out"] = t_end
            x["why"] = done_reason or "Square-off"
        gross = costs = turn = 0.0
        ltxt = []
        for x in all_legs:
            g = (x["exit"] - x["entry"]) * x["side"] * x["qty"]
            gross += g
            costs += 2 * brk
            turn += (x["entry"] if x["act"] == "SELL" else x["exit"]) * x["qty"]
            ltxt.append({"act": x["act"], "opt": x["opt"], "strike": int(x["strike"]), "qty": int(x["qty"]), "in": hm_str(x["t_in"] % 1440), "out": hm_str(x["t_out"] % 1440),
                         "entry": round(x["entry"], 2), "exit": round(x["exit"], 2), "pnl": round(g, 2), "why": x["why"], "role": x["role"]})
        costs += chg / 100.0 * turn
        net = gross - costs
        reason = done_reason or "Square-off"
        trades.append({"date": day_str(d), "case": cfg["name"], "side": cfg["template"], "expiry": day_str(ex), "in": hm_str(te % 1440), "out": hm_str(t_end % 1440),
                       "reason": reason, "lots": mult, "qty": int(sum(x["qty"] for x in legs if x["act"] == "SELL")), "gross": round(gross, 2), "costs": round(costs, 2),
                       "net": round(net, 2), "legs": ltxt, "events": events, "spot": round(spot, 2), "dte": int(ex - d), "dow": DOWN[(d + 3) % 7],
                       "margin": round(margin, 0), "rom": round(100.0 * net / margin, 2) if margin else None, "mtm_peak": round(peak, 0), "mtm_low": round(low, 0),
                       "iv": round(iv_now * 100, 2) if iv_now else None, "ivp": round(ivp, 0) if ivp is not None else None,
                       "hold": int(t_end - te), "r": round(net / sl_b, 2) if sl_b else None, "ret": 0.0, "entry": 0.0, "exit": 0.0, "bars": 0})
    cap = G["capital"]
    days_list = [day_str(d) for d, _ in eligible]
    cut = int(len(days_list) * G["split"] / 100.0)
    sd = days_list[cut] if 0 < cut < len(days_list) else None
    tr_tr = [t for t in trades if sd and t["date"] < sd] if sd else trades
    tr_te = [t for t in trades if sd and t["date"] >= sd] if sd else []
    out = {"stats": {"all": calc_stats(trades, cap), "train": calc_stats(tr_tr, cap), "test": calc_stats(tr_te, cap)},
           "meta": {"days": len(days_list), "from": days_list[0] if days_list else None, "to": days_list[-1] if days_list else None, "split_date": sd, "skipped": skipped,
                    "template": cfg["template"]},
           "trades": trades}
    if trades:
        eq, c_ = [], 0.0
        for t in trades:
            c_ += t["net"]
            eq.append(round(c_, 2))
        out["equity"] = eq
        mg = [t["margin"] for t in trades if t["margin"]]
        avg_m = float(np.mean(mg)) if mg else 0.0
        net = out["stats"]["all"]["net"]
        out["margin"] = {"avg": avg_m, "max": float(max(mg)) if mg else 0.0, "rom_total": float(100 * net / avg_m) if avg_m else 0.0,
                         "dd_pct_margin": float(100 * out["stats"]["all"]["dd"] / avg_m) if avg_m else 0.0}
        yr = max(len(days_list) / 252.0, 1e-9)
        out["margin"]["rom_annual"] = out["margin"]["rom_total"] / yr
        for kk, keyf in (("by_reason", lambda t: t["reason"]), ("by_dow", lambda t: t["dow"]), ("by_dte", lambda t: str(t["dte"]))):
            g = {}
            for t in trades:
                g.setdefault(keyf(t), []).append(t)
            out[kk] = [dict(k=k, **{a: b for a, b in calc_stats(v, cap).items() if a in ("n", "win", "net", "pf", "exp")}) for k, v in sorted(g.items())]
        mo = {}
        for t in trades:
            mo.setdefault(t["date"][:7], []).append(t)
        out["monthly"] = [dict(k=k, **calc_stats(v, cap)) for k, v in sorted(mo.items())]
        by = {}
        for t in trades:
            by[t["date"]] = by.get(t["date"], 0.0) + t["net"]
        dp = np.array([by.get(x, 0.0) for x in days_list], float)
        if len(dp) > 2 and dp.std(ddof=1) > 0:
            out["sharpe"] = float(dp.mean() / dp.std(ddof=1) * np.sqrt(252))
            dsd = float(np.sqrt((np.minimum(dp, 0) ** 2).mean()))
            out["sortino"] = float(dp.mean() / dsd * np.sqrt(252)) if dsd > 0 else 0.0
    return out


def sell_chain(D, raw, body):
    """একটি দিন-সময়ে স্ট্রাইকওয়ার্ক প্রিমিয়াম, IV (স্কিউ) ও ডেল্টা (OI ডেটা আমাদের ডেটাবেসে নেই)"""
    d = day_int(body["date"])
    tm = hm(body.get("time", "09:20"))
    exps = D.expiries
    k = bisect.bisect_left(exps, d)
    if k >= len(exps):
        raise ValueError("এক্সপায়ারি পাওয়া যায়নি")
    ex = exps[k]
    a = int(np.searchsorted(D.day, d, "left"))
    b = int(np.searchsorted(D.day, d, "right"))
    if b <= a:
        raise ValueError("এই দিনের স্পট নেই")
    T = D.t[a:b]
    i = min(int(np.searchsorted(T % 1440, tm)), len(T) - 1)
    te = int(T[i])
    spot = float(D.o[a + i])
    Ty = _T_years(ex, te)
    rows = []
    for typ in ("CE", "PE"):
        for s_ in D.strikes.get((ex, typ), []):
            if abs(s_ - spot) > 0.08 * spot:
                continue
            ct = D.opt.get((ex, typ, float(s_)))
            if ct is None:
                continue
            j = int(np.searchsorted(ct[0], te))
            if j >= len(ct[0]) or ct[0][j] != te:
                continue
            pr = float(ct[4][j])
            iv = float(bs_iv(pr, spot, float(s_), Ty, typ))
            dl = float(bs_greeks(spot, float(s_), Ty, iv, typ)["delta"]) if np.isfinite(iv) else None
            rows.append({"strike": float(s_), "opt": typ, "price": round(pr, 2), "iv": round(iv * 100, 2) if np.isfinite(iv) else None,
                         "delta": None if dl is None else round(dl, 3)})
    return {"spot": spot, "time": hm_str(te % 1440), "expiry": day_str(ex), "rows": rows}



def sell_preview(D, raw, date_str=None):
    """একটি দিনের এন্ট্রি-মুহূর্তের বাস্কেট (পে-অফ চার্টের জন্য): লেগ, প্রিমিয়াম, IV, গ্রিকস"""
    cfg = sell_norm(raw)
    E = cfg["entry"]
    exps = D.expiries
    days = [d for d in D.du.tolist()]
    if date_str:
        d = day_int(date_str)
    else:
        d = days[-1]
    if d not in set(days):
        d = max([x for x in days if x <= d] or [days[-1]])
    k = bisect.bisect_left(exps, d + int(E["min_dte"]))
    if k >= len(exps):
        raise ValueError("এই তারিখে এক্সপায়ারি পাওয়া গেল না")
    ex = exps[k]
    te = d * 1440 + hm(E["time"])
    legs, spot = _sell_day_setup(D, cfg, d, ex, te)
    if legs is None:
        raise ValueError("এই দিনে বাস্কেট বানানো যায়নি: %s" % spot)
    lot = D.exp_lot.get(ex, 1)
    res = []
    ivs = []
    for lg in legs:
        px = float(lg["ct"][1][lg["j0"]])
        T = _T_years(lg["ex"], te)
        iv = float(bs_iv(px, spot, lg["strike"], T, lg["opt"]))
        g = bs_greeks(spot, lg["strike"], T, iv if np.isfinite(iv) else 0.2, lg["opt"])
        res.append({"act": lg["act"], "opt": lg["opt"], "strike": lg["strike"], "lots": lg["lots"], "qty": lg["lots"] * lot, "premium": round(px, 2),
                    "iv": round(iv * 100, 2) if np.isfinite(iv) else None, "delta": round(float(g["delta"]), 3), "theta": round(float(max(g["theta"], -px)), 2),
                    "vega": round(float(g["vega"]), 2), "gamma": round(float(g["gamma"]), 5), "T_days": round(T * 365, 2), "role": lg["role"], "ex": day_str(lg["ex"])})
        ivs.append(iv)
    atm_iv = float(np.nanmean([v for v in ivs if np.isfinite(v)])) if any(np.isfinite(v) for v in ivs) else 0.2
    m = sell_margin([dict(act=x["act"], opt=x["opt"], strike=x["strike"], qty=x["qty"], entry=x["premium"]) for x in res], spot, cfg, lot)
    stk = D.strikes.get((ex, "CE"))
    return {"date": day_str(d), "expiry": day_str(ex), "spot": round(spot, 2), "lot": lot, "legs": res, "atm_iv": round(atm_iv * 100, 2), "margin": round(m, 0),
            "T_days": round(_T_years(ex, te) * 365, 2), "rf": _RF, "strike_step": float(np.median(np.diff(stk))) if stk is not None and len(stk) > 1 else 100.0}



