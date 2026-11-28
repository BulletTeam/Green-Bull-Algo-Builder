# =====================================================================
#  Advisor v2 — রুল অ্যাব্লেশন, ফিল্টার মাইনিং, স্মার্ট সার্চ, রোলিং Walk-Forward
# =====================================================================
def _seg_edges(dates, k):
    """তারিখ-তালিকা (সাজানো, ইউনিক) → k টি প্রায় সমান ক্যালেন্ডার-ভাগের সীমা"""
    if not dates:
        return []
    n = len(dates)
    return [dates[min(n - 1, (n * i) // k)] for i in range(k)] + ["9999-12-31"]


def _slice(trades, lo, hi):
    return [t for t in trades if lo <= t["date"] < hi]


def _sc_basic(trs, cap, min_n=1):
    if len(trs) < min_n:
        return None
    st = calc_stats(trs, cap)
    return float(st["net"] / (abs(st["dd"]) + 0.02 * cap)), st



def mc_fan(trades, cap, n=500, seed=7):
    """বুটস্ট্র্যাপ Monte Carlo: ট্রেডের ক্রম ওলট-পালট করে ইকুইটির P5/P25/P50/P75/P95 ফ্যান ও DD বণ্টন"""
    if len(trades) < 10:
        return None
    a = np.array([t["net"] for t in trades], float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(a), size=(n, len(a)))
    paths = np.cumsum(a[idx], axis=1)
    pk = np.maximum.accumulate(np.concatenate([np.zeros((n, 1)), paths], axis=1), axis=1)[:, 1:]
    mdd = (paths - pk).min(axis=1)
    q = np.percentile(paths, [5, 25, 50, 75, 95], axis=0)
    step = max(1, paths.shape[1] // 120)
    return {"p5": q[0][::step].round(0).tolist(), "p25": q[1][::step].round(0).tolist(), "p50": q[2][::step].round(0).tolist(),
            "p75": q[3][::step].round(0).tolist(), "p95": q[4][::step].round(0).tolist(),
            "dd_p50": float(np.percentile(mdd, 50)), "dd_p95": float(np.percentile(mdd, 5)),
            "end_p5": float(q[0][-1]), "end_p50": float(q[2][-1]), "end_p95": float(q[4][-1]),
            "p_profit": float(100 * (paths[:, -1] > 0).mean()), "p_ruin": float(100 * (paths.min(axis=1) < -0.5 * cap).mean()), "n": len(a), "step": step}


def _daily_curve(trades, dates):
    by = {}
    for t in trades:
        by[t["date"]] = by.get(t["date"], 0.0) + t["net"]
    c, out = 0.0, []
    for d in dates:
        c += by.get(d, 0.0)
        out.append(round(c, 0))
    return out


def advise2(D, raw, opts, progress=None):
    t0 = time.time()
    base = norm_settings(raw)
    G = base["general"]
    cap = G["capital"]
    on_cases = [i for i, c in enumerate(base["cases"]) if c["on"]]
    if not on_cases:
        raise ValueError("কোনো Case চালু নেই")
    ci = int((opts or {}).get("case", on_cases[0]))
    if ci not in on_cases:
        ci = on_cases[0]
    for i, c in enumerate(base["cases"]):
        c["on"] = (i == ci)               # শুধু নির্বাচিত Case বিশ্লেষণ
    if G["split"] <= 0:
        G["split"] = 70
    cp = "cases.%d." % ci
    runs = [0]
    if ADV_CACHE.get("D") is not D:
        ADV_CACHE.clear()
        ADV_CACHE["D"] = D
    cache = ADV_CACHE.setdefault("t", {})
    if len(cache) > 1500:
        cache.clear()
    est = 260

    def run(s):
        k = _ck(s)
        if k not in cache:
            cache[k] = backtest(D, s, light="trades")
            runs[0] += 1
            if progress:
                progress(min(0.96, runs[0] / est))
        return cache[k]

    b = run(base)
    bt = b["trades"]
    if len(bt) < 25:
        raise ValueError("মোট ট্রেড মাত্র %d — v2 বিশ্লেষণের জন্য অন্তত ৫০+ ট্রেড দরকার। তারিখ-সীমা বাড়ান।" % len(bt))
    split_date = b["meta"].get("split_date")
    dates = sorted({t["date"] for t in bt})
    K = int(min(8, max(3, len(bt) // 25)))
    edges = _seg_edges(sorted({day_str(d) for d in D.du.tolist() if day_str(d) >= (b["meta"].get("from") or "")
                               and day_str(d) <= (b["meta"].get("to") or "9999")}), K)
    min_split = lambda trs: (_slice(trs, "0000", split_date), _slice(trs, split_date, "9999")) if split_date else (trs, [])

    def ssc(trs):
        r = _sc_basic(trs, cap)
        return (r[0] if r else None), (r[1] if r else calc_stats([], cap))

    def seg_pos(trs):
        pos = 0
        for i in range(K):
            sl_ = _slice(trs, edges[i], edges[i + 1])
            if sl_ and sum(t["net"] for t in sl_) > 0:
                pos += 1
        return pos / float(K)

    def robust(trs):
        """पूरा-সময়ের স্কোর × (০.৫ + ০.৫ × কতগুলো সময়-ভাগে লাভ)"""
        r = _sc_basic(trs, cap, 10)
        if r is None:
            return None
        return r[0] * (0.5 + 0.5 * seg_pos(trs))

    base_rob = robust(bt)
    out = {"case": base["cases"][ci]["name"], "case_idx": ci, "K": K, "n_base": len(bt), "split_date": split_date}

    # ---------------- ১) Rule Ablation ----------------
    abl = []
    ents = base["cases"][ci]["entry"]
    live_idx = [i for i, sl_ in enumerate(ents) if sl_.get("on", True)]
    if len(live_idx) > 1:
        for i in live_idx:
            s = copy.deepcopy(base)
            s["cases"][ci]["entry"][i]["on"] = False
            r = run(s)["trades"]
            tr_a, te_a = min_split(r)
            tr_b, te_b = min_split(bt)
            sa, sta = ssc(tr_a); sb_, stb = ssc(tr_b)
            ta, stte = ssc(te_a); tb, stteb = ssc(te_b)
            l, op, rr_ = ents[i]["l"], ents[i]["op"], ents[i]["r"]
            abl.append({"idx": i, "rule": "%s %s %s%s" % (l, op, rr_, (" " + str(ents[i].get("num"))) if rr_ == "number" else ""),
                        "n": len(r), "d_net": float(sum(t["net"] for t in r) - sum(t["net"] for t in bt)),
                        "d_train": float(sta["net"] - stb["net"]), "d_test": float(stte["net"] - stteb["net"]),
                        "pf": float(calc_stats(r, cap)["pf"]), "dd": float(calc_stats(r, cap)["dd"]),
                        "dd_base": float(calc_stats(bt, cap)["dd"]), "rob": robust(r)})
    out["ablation"] = abl

    # ---------------- ২) Filter Mining ----------------
    filt = []
    try:
        Sx = SRC_CACHE["v"][0] if SRC_CACHE.get("D") is D else None
        if Sx is None or SRC_CACHE.get("k") is None:
            backtest(D, base, light="trades")
            Sx = SRC_CACHE["v"][0]
    except Exception:
        Sx = None
    mined = []
    if Sx is not None and len(bt) >= 40:
        tr_b, te_b = min_split(bt)
        def val(name, t):
            a = Sx.get(name)
            if a is None:
                return None
            v = a[t["ib"]]
            return None if v is None or not np.isfinite(v) else float(v)
        feats = ["adx", "rsi", "bodyatr", "cprw", "gap", "bbw", "minsopen", "atr"]
        cands = []
        for f in feats:
            vals = np.array([val(f, t) if val(f, t) is not None else np.nan for t in tr_b], float)
            ok = np.isfinite(vals)
            if ok.sum() < 30:
                continue
            for q in (20, 30, 40, 50, 60, 70, 80):
                thr = float(np.round(np.percentile(vals[ok], q), 2))
                for op in ("above", "below"):
                    keep = ok & ((vals > thr) if op == "above" else (vals < thr))
                    if keep.sum() < max(20, 0.4 * len(tr_b)):
                        continue
                    kt = [t for t, k in zip(tr_b, keep) if k]
                    r_ = _sc_basic(kt, cap, 20)
                    if r_:
                        cands.append((r_[0], {"l": f, "op": op, "r": "number", "num": thr}))
        # তুলনামূলক: ভলিউম, DI, VWAP
        for desc, l_, op_, r_, mul in (("volume > volsma", "volume", "above", "volsma", 1.0), ("volume > 1.5×volsma", "volume", "above", "volsma", 1.5),
                                       ("DI+ > DI-", "dip", "above", "dim", 1.0), ("DI- > DI+", "dim", "above", "dip", 1.0),
                                       ("close > vwap", "close", "above", "vwap", 1.0), ("close < vwap", "close", "below", "vwap", 1.0)):
            keep, kt = [], []
            for t in tr_b:
                a, c_ = val(l_, t), val(r_, t)
                if a is None or c_ is None:
                    continue
                if (a > c_ * mul) if op_ == "above" else (a < c_ * mul):
                    kt.append(t)
            if len(kt) >= max(20, 0.4 * len(tr_b)):
                r2 = _sc_basic(kt, cap, 20)
                if r2:
                    cands.append((r2[0], {"l": l_, "op": op_, "r": r_, "mul": mul, "num": 0.0}))
        cands.sort(key=lambda x: -x[0])
        base_sc = _sc_basic(tr_b, cap)[0]
        seen = set()
        cur_s = copy.deepcopy(base)
        cur_trades = bt
        for rnd in range(2):          # সর্বোচ্চ ২টি ফিল্টার (লোভী পদ্ধতি), প্রতিটি আসল ইঞ্জিনে যাচাই
            best = None
            for sc0, sl_ in cands[:10]:
                key = json.dumps(sl_, sort_keys=True)
                if key in seen:
                    continue
                s = copy.deepcopy(cur_s)
                ns = slot(sl_["l"], sl_["op"], sl_["r"], sl_.get("num", 0.0), mul=sl_.get("mul", 1.0), join="AND")
                s["cases"][ci]["entry"].append(ns)
                r = run(s)["trades"]
                a_tr, a_te = min_split(r)
                p_tr, p_te = min_split(cur_trades)
                sa, sta = ssc(a_tr); sp_, stp = ssc(p_tr)
                ta_, stta = ssc(a_te); tp_, sttp = ssc(p_te)
                if sa is None or sp_ is None:
                    continue
                rec = {"rule": "%s %s %s" % (sl_["l"], sl_["op"], (sl_["r"] if sl_["r"] != "number" else sl_["num"]) if sl_.get("mul", 1.0) == 1.0 else "%s×%s" % (sl_["mul"], sl_["r"])),
                       "n": len(r), "d_train": float(sta["net"] - stp["net"]), "d_test": float(stta["net"] - sttp["net"]),
                       "dd_tr": float(sta["dd"]), "dd_tr_prev": float(stp["dd"]), "dd_te": float(stta["dd"]), "dd_te_prev": float(sttp["dd"]),
                       "n_tr": len(a_tr), "n_te": len(a_te), "pf_te": float(stta["pf"]), "slot": sl_, "round": rnd + 1,
                       "rob": robust(r), "rob_prev": robust(cur_trades)}
                rec["ok"] = bool(rec["d_test"] >= 0 and stta["net"] > 0 and (rec["rob"] or -9) > (rec["rob_prev"] or -9) * 1.03)
                seen.add(key)
                filt.append(rec)
                if rec["ok"] and (best is None or (sa > best[0])):
                    best = (sa, rec, s, r)
            if best is None:
                break
            cur_s, cur_trades = best[2], best[3]
            best[1]["chosen"] = True
        out["filter_settings_entry"] = cur_s["cases"][ci]["entry"]
    out["filters"] = filt

    # ---------------- ৩) স্মার্ট সার্চ (Stage A random + Stage B hill-climb) ----------------
    c0 = base["cases"][ci]
    L0 = G["lots"]
    grid = {
        "strike": [-2, -1, 0, 1, 2] if c0["strike"]["mode"] == "offset" else [100, 150, 200, 300, 400],
        "sl": [None, 8, 10, 12, 15, 20, 25, 30, 40],
        "tp": [None, 30, 50, 80, 120, ("rr", 1.5), ("rr", 2.0), ("rr", 3.0)],
        "trail": [None] + [("pct", v, a) for v in (8, 10, 15, 20) for a in (0, 10, 20)] + [("step", 10, 5), ("step", 15, 10), ("step", 20, 10), ("step", 30, 15)],
        "be": [None, 10, 15, 20],
        "ts": [0, 15, 30, 45, 60, 90],
    }
    order = list(grid)
    cur_i = {}
    def to_idx(k):
        c = c0
        if k == "strike":
            v = c["strike"]["off"] if c["strike"]["mode"] == "offset" else c["strike"]["prem"]
        elif k == "sl":
            v = None if c["sl"]["mode"] == "none" else (c["sl"]["val"] if c["sl"]["mode"] == "pct" else "x")
        elif k == "tp":
            v = None if c["tp"]["mode"] == "none" else (c["tp"]["val"] if c["tp"]["mode"] == "pct" else (("rr", c["tp"]["val"]) if c["tp"]["mode"] == "rr" else "x"))
        elif k == "trail":
            m = c["trail"]["mode"]
            v = None if m == "none" else (("pct", c["trail"]["val"], c["trail"]["act"]) if m == "pct" else ("step", c["trail"]["trig"], c["trail"]["move"]) if c["trail"].get("unit", "pct") == "pct" else "x")
        elif k == "be":
            v = (c["be"]["val"] if c["be"]["on"] else None)
        else:
            v = c.get("time_stop", 0) or 0
        for i, g in enumerate(grid[k]):
            if (isinstance(g, tuple) or g is None) and g == v:
                return i
            if not isinstance(g, tuple) and g is not None and v is not None and not isinstance(v, tuple) and v != "x" and abs(float(g) - float(v)) < 1e-9:
                return i
        return None
    for k in order:
        cur_i[k] = to_idx(k)

    def build(idx):
        s = copy.deepcopy(base)
        c = s["cases"][ci]
        k = idx["strike"]; g = grid["strike"][k] if k is not None else None
        if g is not None:
            if c["strike"]["mode"] == "offset":
                c["strike"]["off"] = g
            else:
                c["strike"]["prem"] = g
        k = idx["sl"]
        if k is not None:
            g = grid["sl"][k]
            c["sl"] = {"mode": "none", "val": c["sl"]["val"]} if g is None else {"mode": "pct", "val": float(g)}
        k = idx["tp"]
        if k is not None:
            g = grid["tp"][k]
            c["tp"] = {"mode": "none", "val": c["tp"]["val"]} if g is None else ({"mode": "rr", "val": g[1]} if isinstance(g, tuple) else {"mode": "pct", "val": float(g)})
        k = idx["trail"]
        if k is not None:
            g = grid["trail"][k]
            if g is None:
                c["trail"]["mode"] = "none"
            elif g[0] == "pct":
                c["trail"].update(mode="pct", val=float(g[1]), act=float(g[2]))
            else:
                c["trail"].update(mode="step", unit="pct", trig=float(g[1]), move=float(g[2]))
        k = idx["be"]
        if k is not None:
            g = grid["be"][k]
            c["be"]["on"] = g is not None
            if g is not None:
                c["be"]["val"] = float(g)
        k = idx["ts"]
        if k is not None:
            c["time_stop"] = grid["ts"][k]
        return s

    pool = {}           # json(idx) -> (idx, trades)
    def ev_idx(idx):
        kk = json.dumps(idx, sort_keys=True)
        if kk not in pool:
            pool[kk] = (dict(idx), run(build(idx))["trades"])
        return pool[kk]
    rng = np.random.default_rng(11)
    ev_idx(dict(cur_i))
    nA = 40
    for _ in range(nA):
        idx = {k: int(rng.integers(0, len(grid[k]))) for k in order}
        for k in order:                      # স্ট্রাইক মোড prem হলে বর্তমান-ভিত্তিক থাকুক
            pass
        ev_idx(idx)
    def top(n):
        sc = [(robust(tr) , kk) for kk, (ix, tr) in pool.items()]
        sc = [(a, kk) for a, kk in sc if a is not None]
        sc.sort(reverse=True)
        return [kk for a, kk in sc[:n]]
    for rnd in range(3):
        for kk in top(5):
            ix = dict(pool[kk][0])
            for k in order:
                for dlt in (-1, 1):
                    j = (ix[k] if ix[k] is not None else 0) + dlt
                    if 0 <= j < len(grid[k]):
                        ni = dict(ix)
                        ni[k] = j
                        ev_idx(ni)
        if runs[0] > 230:
            break

    # ---------------- ৪) Walk-Forward (পুল থেকে নির্বাচন, শুধু ট্রেন-অংশে) ----------------
    items = list(pool.items())
    base_kk = json.dumps(dict(cur_i), sort_keys=True)
    wfo = []
    oos_all, oos_base, is_sc = [], [], []
    tr_len = max(2, K - 2)
    for w in range(0, K - tr_len):
        lo, mid, hi = edges[w], edges[w + tr_len], edges[w + tr_len + 1]
        best_kk, best_sc = None, None
        for kk, (ix, tr) in items:
            r = _sc_basic(_slice(tr, lo, mid), cap, 8)
            if r and (best_sc is None or r[0] > best_sc):
                best_kk, best_sc = kk, r[0]
        if best_kk is None:
            continue
        o = _slice(pool[best_kk][1], mid, hi)
        ob = _slice(pool[base_kk][1], mid, hi)
        oos_all += o
        oos_base += ob
        wfo.append({"train": "%s→%s" % (lo[:7], mid[:7]), "test": "%s→%s" % (mid[:7], hi[:7] if hi != "9999-12-31" else "শেষ"),
                    "net_oos": float(sum(t["net"] for t in o)), "net_base": float(sum(t["net"] for t in ob)), "n_oos": len(o),
                    "net_is": float(sum(t["net"] for t in _slice(pool[best_kk][1], lo, mid)))})
    so, sb_ = calc_stats(oos_all, cap), calc_stats(oos_base, cap)
    is_net = sum(w["net_is"] for w in wfo)
    oos_net = sum(w["net_oos"] for w in wfo)
    wfe = (oos_net / (is_net * (1.0))) * (tr_len / 1.0) if is_net > 0 else 0.0     # প্রতি-সময়-ভাগে OOS ÷ IS
    out["wfo"] = {"windows": wfo, "oos": {"n": so["n"], "net": so["net"], "pf": so["pf"], "dd": so["dd"], "win": so["win"]},
                  "base": {"n": sb_["n"], "net": sb_["net"], "pf": sb_["pf"], "dd": sb_["dd"], "win": sb_["win"]},
                  "wfe": float(wfe), "tr_len": tr_len}

    # ---------------- ৫) চূড়ান্ত সাজেশন ----------------
    ranked = sorted([(robust(tr), kk) for kk, (ix, tr) in items if robust(tr) is not None], reverse=True)
    best_kk = ranked[0][1] if ranked else base_kk
    bix, btr = pool[best_kk]
    bs = build(bix)
    if out.get("filter_settings_entry"):
        pass
    # ফিল্টার + সেরা এক্সিট একসাথে চালিয়ে যাচাই
    comb = copy.deepcopy(bs)
    if out.get("filter_settings_entry"):
        comb["cases"][ci]["entry"] = out["filter_settings_entry"]
    comb_tr = run(comb)["trades"]

    # ---------------- ৩ক) ইন্ডিকেটর-প্যারামিটার ও এন্ট্রি-থ্রেশহোল্ড সার্চ (প্রতিবেশী-যাচাইসহ) ----------------
    IND_MAP = {"ma1": [("ind.ma1.len", [5, 8, 9, 10, 13, 20, 21, 34, 50])], "ma2": [("ind.ma2.len", [5, 8, 9, 10, 13, 20, 21, 34, 50])], "ma3": [("ind.ma3.len", [10, 20, 21, 34, 50, 100])],
               "rsi": [("ind.rsi.len", [7, 9, 14, 21])], "adx": [("ind.adx.len", [7, 10, 14, 20])], "dip": [("ind.adx.len", [7, 10, 14, 20])], "dim": [("ind.adx.len", [7, 10, 14, 20])],
               "atr": [("ind.atr.len", [7, 10, 14, 20])], "bodyatr": [("ind.atr.len", [7, 10, 14, 20])],
               "st1": [("ind.st1.len", [7, 10, 14]), ("ind.st1.mult", [1.0, 1.5, 2.0, 2.5, 3.0])], "st1dir": [("ind.st1.len", [7, 10, 14]), ("ind.st1.mult", [1.0, 1.5, 2.0, 2.5, 3.0])],
               "st2": [("ind.st2.len", [7, 10, 14]), ("ind.st2.mult", [2.0, 2.5, 3.0, 4.0])], "st2dir": [("ind.st2.len", [7, 10, 14]), ("ind.st2.mult", [2.0, 2.5, 3.0, 4.0])],
               "bbup": [("ind.bb.len", [14, 20, 30]), ("ind.bb.mult", [1.5, 2.0, 2.5])], "bblow": [("ind.bb.len", [14, 20, 30]), ("ind.bb.mult", [1.5, 2.0, 2.5])],
               "bbmid": [("ind.bb.len", [14, 20, 30])], "bbw": [("ind.bb.len", [14, 20, 30]), ("ind.bb.mult", [1.5, 2.0, 2.5])],
               "donhi": [("ind.don.n", [10, 15, 20, 30])], "donlo": [("ind.don.n", [10, 15, 20, 30])], "orbh": [("ind.orb.mins", [5, 15, 30])], "orbl": [("ind.orb.mins", [5, 15, 30])]}
    cands_i = []
    seen_p = set()
    for sl_i, sl_ in enumerate(comb["cases"][ci]["entry"]):
        if not sl_.get("on", True):
            continue
        for k_ in ("l", "r"):
            for pth, gv in IND_MAP.get(sl_[k_], []):
                if pth not in seen_p:
                    seen_p.add(pth)
                    cands_i.append(("ind", pth, gv))
        if sl_["r"] == "number":
            v0 = float(sl_.get("num", 0))
            if sl_["l"] in ("rsi",):
                gv = [round(v0 + d, 2) for d in (-10, -5, 5, 10)]
            elif v0 != 0:
                gv = [round(v0 * f, 2) for f in (0.7, 0.85, 1.15, 1.3)]
            else:
                gv = []
            if gv:
                cands_i.append(("slot", "cases.%d.entry.%d.num" % (ci, sl_i), gv))
    ind_rows = []
    cur_s, cur_tr = comb, comb_tr
    n_ind = 0
    for kind_, pth, gv in cands_i:
        if n_ind >= 30:
            break
        base_v = None
        try:
            o_ = cur_s
            for kk in pth.split("."):
                o_ = o_[int(kk)] if isinstance(o_, list) else o_[kk]
            base_v = o_
        except Exception:
            continue
        r0 = robust(cur_tr)
        best = None
        for v in gv:
            if v == base_v:
                continue
            s2 = copy.deepcopy(cur_s)
            set_path(s2, pth, v)
            try:
                tr2 = run(s2)["trades"]
            except Exception:
                continue
            n_ind += 1
            rb = robust(tr2)
            if rb is None or r0 is None:
                continue
            a_tr, a_te = min_split(tr2)
            p_tr, p_te = min_split(cur_tr)
            _, sta = ssc(a_tr); _, stp = ssc(p_tr); _, stta = ssc(a_te); _, sttp = ssc(p_te)
            rec = {"param": pth, "from": base_v, "to": v, "d_train": float(sta["net"] - stp["net"]), "d_test": float(stta["net"] - sttp["net"]), "n": len(tr2), "rob": rb, "rob_prev": r0}
            rec["ok"] = bool(rb > r0 * 1.05 and rec["d_test"] >= 0 and len(tr2) >= 0.6 * len(cur_tr))
            ind_rows.append(rec)
            if rec["ok"] and (best is None or rb > best[0]):
                best = (rb, v, s2, tr2, rec)
        if best:
            # প্ল্যাটো-যাচাই: নির্বাচিত মানের দুই পাশের মান অন্তত ৮৫% স্কোর ধরে রাখতে হবে
            gl = sorted(set([x for x in gv] + [base_v]), key=lambda z: float(z))
            ix = gl.index(best[1])
            nb = [gl[j] for j in (ix - 1, ix + 1) if 0 <= j < len(gl)]
            okp = True
            for v in nb:
                s3 = copy.deepcopy(cur_s)
                set_path(s3, pth, v)
                try:
                    r3 = robust(run(s3)["trades"])
                except Exception:
                    r3 = None
                if r3 is None or r3 < best[0] * 0.85:
                    okp = False
            best[4]["plateau"] = bool(okp)
            if okp:
                best[4]["chosen"] = True
                cur_s, cur_tr = best[2], best[3]
    out["ind_search"] = ind_rows
    if cur_s is not comb:
        comb, comb_tr = cur_s, cur_tr
    def pack(trs):
        a, b2 = min_split(trs)
        return {"all": _tr(calc_stats(trs, cap)), "train": _tr(calc_stats(a, cap)), "test": _tr(calc_stats(b2, cap)),
                "seg_pos": seg_pos(trs), "rob": robust(trs)}
    out["base"] = pack(bt)
    out["exit_best"] = pack(btr)
    out["final"] = pack(comb_tr)
    alld = sorted({t["date"] for t in bt} | {t["date"] for t in comb_tr})
    out["curves"] = {"dates": alld, "base": _daily_curve(bt, alld), "final": _daily_curve(comb_tr, alld), "split": split_date}
    out["mc"] = {"base": mc_fan(bt, cap), "final": mc_fan(comb_tr, cap)}
    # পরিবর্তনের তালিকা (settings diff)
    chg = []
    def addc(label, a, b2, muts):
        if a != b2:
            chg.append({"label": label, "from": a, "to": b2, "muts": muts})
    cb, cn = base["cases"][ci], comb["cases"][ci]
    addc("স্ট্রাইক", cb["strike"]["off"] if cb["strike"]["mode"] == "offset" else cb["strike"]["prem"],
         cn["strike"]["off"] if cn["strike"]["mode"] == "offset" else cn["strike"]["prem"],
         [(cp + "strike.off", cn["strike"]["off"])] if cn["strike"]["mode"] == "offset" else [(cp + "strike.prem", cn["strike"]["prem"])])
    addc("SL", (cb["sl"]["mode"], cb["sl"]["val"]), (cn["sl"]["mode"], cn["sl"]["val"]), [(cp + "sl.mode", cn["sl"]["mode"]), (cp + "sl.val", cn["sl"]["val"])])
    addc("টার্গেট", (cb["tp"]["mode"], cb["tp"]["val"]), (cn["tp"]["mode"], cn["tp"]["val"]), [(cp + "tp.mode", cn["tp"]["mode"]), (cp + "tp.val", cn["tp"]["val"])])
    tk = lambda c: (c["trail"]["mode"], c["trail"].get("val"), c["trail"].get("act"), c["trail"].get("trig"), c["trail"].get("move"))
    addc("ট্রেইলিং", tk(cb), tk(cn), [(cp + "trail.mode", cn["trail"]["mode"]), (cp + "trail.val", cn["trail"].get("val")), (cp + "trail.act", cn["trail"].get("act")),
                                    (cp + "trail.unit", cn["trail"].get("unit", "pct")), (cp + "trail.trig", cn["trail"].get("trig")), (cp + "trail.move", cn["trail"].get("move"))])
    addc("ব্রেকইভেন", (cb["be"]["on"], cb["be"]["val"]), (cn["be"]["on"], cn["be"]["val"]), [(cp + "be.on", cn["be"]["on"]), (cp + "be.val", cn["be"]["val"])])
    addc("টাইম-স্টপ", cb.get("time_stop", 0), cn.get("time_stop", 0), [(cp + "time_stop", cn.get("time_stop", 0))])
    for r_ in out["ind_search"]:
        if r_.get("chosen"):
            addc("ইন্ডিকেটর/থ্রেশহোল্ড " + r_["param"], r_["from"], r_["to"], [(r_["param"], r_["to"])])
    if out.get("filter_settings_entry") and len(out["filter_settings_entry"]) != len(cb["entry"]):
        addc("এন্ট্রি ফিল্টার", len(cb["entry"]), len(out["filter_settings_entry"]), [(cp + "entry", out["filter_settings_entry"])])
    out["changes"] = chg
    out["apply"] = [{"path": p, "value": v} for c in chg for p, v in c["muts"]]
    # রায়
    msgs, level = [], "pass"
    w = out["wfo"]
    if len(wfo) == 0:
        level = "warn"
        msgs.append("Walk-Forward চালানোর মতো পর্যাপ্ত সময়-ভাগ নেই — যাচাই দুর্বল।")
    else:
        if w["oos"]["net"] <= 0:
            level = "fail"
            msgs.append("Walk-Forward: অদেখা সময়ে (OOS) প্রক্রিয়াটির মোট নেট ঋণাত্মক — এই স্ট্র্যাটেজিতে টিউনিং ভরসাযোগ্য নয়।")
        elif w["oos"]["net"] < w["base"]["net"]:
            level = "warn"
            msgs.append("Walk-Forward: টিউনিং করা সেটিং অদেখা সময়ে বর্তমান সেটিংয়ের চেয়ে ভালো করেনি (OOS %s ₹ বনাম %s ₹)।" % (round(w["oos"]["net"]), round(w["base"]["net"])))
        else:
            msgs.append("Walk-Forward: অদেখা সময়ে টিউনিং বর্তমানের চেয়ে ভালো (OOS %s ₹ বনাম %s ₹)।" % (round(w["oos"]["net"]), round(w["base"]["net"])))
        if w["wfe"] < 0.5 and w["wfe"] != 0:
            level = "warn" if level == "pass" else level
            msgs.append("WFE %.0f%% (৫০%%-এর নিচে): ইন-স্যাম্পলের লাভ অদেখা সময়ে অর্ধেকের কম টিকেছে।" % (w["wfe"] * 100))
    fa, ba = out["final"], out["base"]
    if fa["seg_pos"] < 0.6:
        level = "warn" if level == "pass" else level
        msgs.append("সাজেশনে মাত্র %.0f%% সময়-ভাগে লাভ — ধারাবাহিকতা কম।" % (fa["seg_pos"] * 100))
    if fa["test"]["net"] < ba["test"]["net"]:
        level = "warn" if level == "pass" else level
        msgs.append("চূড়ান্ত সাজেশন TEST-অংশে (%s ₹) বর্তমানের (%s ₹) চেয়ে খারাপ।" % (round(fa["test"]["net"]), round(ba["test"]["net"])))
    if not chg:
        level = "keep"
        msgs.append("বর্তমান সেটিংই সার্চে সেরা — বদলানোর স্পষ্ট কারণ পাওয়া যায়নি।")
    out["validation"] = {"level": level, "msgs": msgs}
    out["runs"] = runs[0]
    out["secs"] = time.time() - t0
    out["pool"] = len(pool)
    out["warnings"] = ["সব ফল ঐতিহাসিক ডেটার ওপর; ট্রেড-সংখ্যা কম হলে (১০০-র নিচে) যেকোনো সাজেশন অস্থির।",
                       "সার্চে ~%d সেটিং-সংযোগ চালানো হয়েছে; বেশি চেষ্টা = বেশি ভাগ্যের ঝুঁকি, তাই রায় Walk-Forward-এর অদেখা অংশ দেখে নিন।" % len(pool)]
    return out


