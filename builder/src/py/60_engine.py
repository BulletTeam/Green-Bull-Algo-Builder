# =====================================================================
#  ব্যাকটেস্ট ইঞ্জিন
# =====================================================================


def contract_view(D, ct):
    """একটা অপশন কন্ট্র্যাক্টের নিজের প্রিমিয়াম চার্টকে স্পটের মতো ডেটা-অবজেক্ট বানায়"""
    t, o, h, l, c = ct
    mod = t % 1440
    m = (mod >= OPEN_MIN) & (mod <= OPEN_MIN + 374)
    V = Data()
    V._bars, V._cache = {}, {}
    V.t, V.o, V.h, V.l, V.c = t[m], o[m], h[m], l[m], c[m]
    V.v = np.zeros(len(V.t))
    V.day, V.mod = V.t // 1440, V.t % 1440
    if len(V.t) == 0:
        return V
    V.du = np.unique(V.day)
    st = np.flatnonzero(np.r_[True, V.day[1:] != V.day[:-1]])
    en = np.r_[st[1:], len(V.day)]
    V.dO, V.dH = V.o[st], np.maximum.reduceat(V.h, st)
    V.dL, V.dC = np.minimum.reduceat(V.l, st), V.c[en - 1]
    return V


def prem_signals(D, st, exset):
    """প্রতিটা কন্ট্র্যাক্টের প্রিমিয়াম চার্টে সিগন্যাল/এক্সিট সময় বের করে (ছোট অ্যারে হিসেবে ক্যাশ হয়)"""
    G = st["general"]
    ck = json.dumps([G["tf"], G["fresh"], G["win_from"], G["win_to"], st["ind"],
                     [[c["opt"], c["on"], c["entry"], c["exit"]] for c in st["cases"]]], sort_keys=True)
    pc = D.__dict__.setdefault("_pc", {})
    store = pc.setdefault(ck, {})
    if len(pc) > 3:
        pc.pop(next(iter(pc)))
    w0, w1 = hm(G["win_from"]), hm(G["win_to"])
    for (ex, typ, stk), ct in D.opt.items():
        if ex not in exset or (ex, typ, stk) in store:
            continue
        res = {}
        V = contract_view(D, ct)
        if len(V.t) > 5:
            S, B = compute_sources(V, G["tf"], st["ind"])
            emod = B.e % 1440
            wm = (emod >= w0) & (emod <= w1)
            first = np.r_[True, B.day[1:] != B.day[:-1]]
            for ci, cs in enumerate(st["cases"]):
                if not cs["on"] or cs["opt"] != typ:
                    continue
                ent = eval_cond(S, cs["entry"], B.n)
                if ent is None:
                    continue
                if G["fresh"]:
                    prev = np.r_[False, ent[:-1]]
                    prev[first] = False
                    ent = ent & ~prev
                ent = ent & wm
                xx = eval_cond(S, cs["exit"], B.n)
                res[ci] = (B.e[ent], B.c[ent], B.e[xx] if xx is not None else np.zeros(0, "int64"))
        V._cache.clear()
        store[(ex, typ, stk)] = res
    return store


SRC_CACHE = {}
ADV_CACHE = {}


def backtest(D, raw, light=False, progress=None, now=None):
    st = norm_settings(raw)
    G = st["general"]
    PM = G.get("chart") == "prem"
    ck = (G["tf"], json.dumps(st["ind"], sort_keys=True))
    if SRC_CACHE.get("D") is D and SRC_CACHE.get("k") == ck:
        S, B = SRC_CACHE["v"]
    else:
        S, B = compute_sources(D, G["tf"], st["ind"])
        SRC_CACHE.update(D=D, k=ck, v=(S, B), cond={})
    n = B.n
    w0, w1, sq = hm(G["win_from"]), hm(G["win_to"]), hm(G["sqoff"])
    emod = B.e % 1440
    wmask = (emod >= w0) & (emod <= w1)
    first_of_day = np.r_[True, B.day[1:] != B.day[:-1]]

    cases = []
    for ci, cs in enumerate(st["cases"]):
        if not cs["on"] or PM:
            continue
        cc = json.dumps([cs["entry"], cs["exit"]], sort_keys=True)
        hit = SRC_CACHE["cond"].get(cc)
        if hit is None:
            hit = (eval_cond(S, cs["entry"], n), eval_cond(S, cs["exit"], n))
            if len(SRC_CACHE["cond"]) > 6:
                SRC_CACHE["cond"].clear()
            SRC_CACHE["cond"][cc] = hit
        ent = hit[0]
        if ent is None:
            continue
        if G["fresh"]:
            prev = np.r_[False, ent[:-1]]
            prev[first_of_day] = False
            sig = ent & ~prev
        else:
            sig = ent
        ex = hit[1]
        xs = set(B.e[ex].tolist()) if ex is not None else set()
        cases.append((ci, cs, sig & wmask, xs))

    d_from = day_int(G["from"]) if G["from"] else -10 ** 9
    d_to = day_int(G["to"]) if G["to"] else 10 ** 9
    exps = D.expiries
    eligible = []
    for d in D.du.tolist():
        if d < d_from or d > d_to:
            continue
        k = bisect.bisect_left(exps, d + G["min_dte"])
        if k >= len(exps):
            continue
        ex = exps[k]
        if G["skip_expiry_day"] and ex == d:
            continue
        lo, hi = D.exp_range[ex]
        if lo <= d * 1440 + 1439 and hi >= d * 1440:
            eligible.append((d, ex))

    PS = prem_signals(D, st, {ex for _, ex in eligible}) if PM else None
    pcases = [(ci, cs) for ci, cs in enumerate(st["cases"]) if cs["on"]] if PM else []
    skipped = {"ডেটা নেই (কন্ট্র্যাক্টে বার নেই)": 0, "স্ট্রাইক পাওয়া যায়নি": 0}
    trades = []
    slip = G["slip"]
    sl_first = bool(G["sl_first"])
    size_state = {"cum": 0.0}

    def locate(ex, typ, stk, te):
        ct = D.opt.get((ex, typ, float(stk)))
        if ct is None:
            return None
        j = int(np.searchsorted(ct[0], te))
        if j < len(ct[0]) and ct[0][j] == te:
            return ct, j
        return None

    def pick(cs, ex, te, spot_ref):
        typ = cs["opt"]
        stk = D.strikes.get((ex, typ))
        if stk is None:
            return None
        sm = cs["strike"]
        if sm["mode"] == "prem":
            X = float(sm["prem"])
            best = None
            for s_ in stk:
                r = locate(ex, typ, s_, te)
                if r is None:
                    continue
                pr = r[0][1][r[1]]
                if pr <= 0:
                    continue
                diff = abs(pr - X)
                if best is None or diff < best[0]:
                    best = (diff, r[0], r[1], float(s_))
            return None if best is None else (best[1], best[2], best[3])
        atm = int(np.argmin(np.abs(stk - spot_ref)))
        off = int(float(sm["off"]))
        k = atm + (off if typ == "CE" else -off)
        if k < 0 or k >= len(stk):
            return "nostrike"
        s_ = float(stk[k])
        r = locate(ex, typ, s_, te)
        if r is None:
            return None
        return (r[0], r[1], s_)

    def run_trade(cs, xs, ct, j, te, day_end, lot):
        t, o, h, l, c = ct
        side = 1 if cs["action"] == "BUY" else -1
        raw_e = float(o[j])
        sl = tp = None
        sm, sv = cs["sl"]["mode"], float(cs["sl"]["val"])
        if sm == "pct":
            sl = raw_e * (1 - side * sv / 100)
        elif sm == "pts":
            sl = raw_e - side * sv
        if sl is not None and side == 1 and sl < 0.05:
            sl = 0.05              # লং-এ SL ঋণাত্মক/শূন্য প্রিমিয়াম হতে পারে না
        sl0 = sl
        tm, tv = cs["tp"]["mode"], float(cs["tp"]["val"])
        if tm == "pct":
            tp = raw_e * (1 + side * tv / 100)
        elif tm == "pts":
            tp = raw_e + side * tv
        elif tm == "rr" and sl is not None:
            tp = raw_e + side * tv * abs(raw_e - sl)
        # ---- আংশিক প্রফিট বুকিং (T1/T2) — লট পূর্ণসংখ্যায়, অন্তত ১ লট রানারের জন্য থাকে ----
        L = G["lots"]
        _cur = max(G["capital"] + size_state["cum"], 0.2 * G["capital"])
        if G["size_mode"] == "compound":
            L = max(1, min(G["max_lots"], int(G["lots"] * _cur / G["capital"])))
        elif G["size_mode"] == "risk" and sl0 is not None and abs(raw_e - sl0) * lot > 0:
            L = max(1, min(G["max_lots"], int(_cur * G["risk_pct"] / 100.0 // (abs(raw_e - sl0) * lot))))
        tg = cs.get("tg", {})
        books = []
        if tg.get("on") and L >= 2:
            unit = tg.get("unit", "pct")

            def tpx(v):
                if unit == "pct":
                    return raw_e * (1 + side * v / 100)
                if unit == "pts":
                    return raw_e + side * v
                if unit == "rr" and sl0 is not None:
                    return raw_e + side * v * abs(raw_e - sl0)
                return None
            spec = [("T1", float(tg["t1"]["val"]), float(tg["t1"]["qty"]))]
            if tg["t2"].get("on"):
                spec.append(("T2", float(tg["t2"]["val"]), float(tg["t2"]["qty"])))
            left = L - 1
            for tag, v, q in spec:
                if left <= 0:
                    break
                p = tpx(v)
                if p is None or p <= 0:
                    continue
                if books and (p - books[-1][0]) * side <= 0:
                    continue
                if tp is not None and (p - tp) * side >= 0:
                    continue
                n = max(1, min(int(L * q / 100.0 + 0.4999), left))     # ৩ লটের ৫০% = ১ লট
                books.append([p, n, tag])
                left -= n
        a1, a2 = tg.get("a1", {}), tg.get("a2", {})
        tr = cs["trail"]
        tmode = tr.get("mode", "none")
        trail_v = float(tr.get("val", 15))
        trail_act = float(tr.get("act", 0) or 0)
        t_pts = tr.get("unit", "pct") == "pts"
        t_trig = float(tr.get("trig", 10) or 0)
        t_move = float(tr.get("move", 5) or 0)
        trig_pts = t_trig if t_pts else raw_e * t_trig / 100.0
        move_pts = t_move if t_pts else raw_e * t_move / 100.0
        be_on = bool(cs["be"]["on"])
        be_v = float(cs["be"]["val"])
        tstop = int(float(cs.get("time_stop", 0) or 0))
        peak = raw_e
        mfe = mae = 0.0
        nb = len(t)
        jj = j
        rem = L
        bk = 0
        legs = []
        reason = px = None
        exit_idx = None

        def tighten(cur, new):
            if cur is None:
                return new
            return max(cur, new) if side == 1 else min(cur, new)
        slh = []
        last_sl = sl
        while jj < nb and t[jj] < day_end and (now is None or t[jj] <= now):
            s = int(t[jj])
            oo, hh, ll = float(o[jj]), float(h[jj]), float(l[jj])
            if now is not None and s == now:
                hh = ll = oo       # চলতি মিনিটের শুধু open জানা (লাইভে হাই/লো এখনো গঠনাধীন)
            full = None
            if jj > j:
                if s % 1440 >= sq:
                    full = ("Square-off", oo)
                elif s in xs:
                    full = ("Signal Exit", oo)
                elif tstop and s - te >= tstop:
                    full = ("Time Stop", oo)
            hits = []
            if full is None:
                if side == 1:
                    sl_hit = sl is not None and ll <= sl
                    tp_hit = tp is not None and hh >= tp
                    gap_sl = jj > j and sl is not None and oo <= sl
                    gap_tp = jj > j and tp is not None and oo >= tp
                else:
                    sl_hit = sl is not None and hh >= sl
                    tp_hit = tp is not None and ll <= tp
                    gap_sl = jj > j and sl is not None and oo >= sl
                    gap_tp = jj > j and tp is not None and oo <= tp
                kk = bk
                while kk < len(books):
                    p = books[kk][0]
                    g = jj > j and ((oo >= p) if side == 1 else (oo <= p))
                    hit = (hh >= p) if side == 1 else (ll <= p)
                    if hit or g:
                        hits.append((kk, oo if g else p))
                        kk += 1
                    else:
                        break
                if gap_sl:
                    full, hits = ("SL (gap)", oo), []
                elif gap_tp:
                    full, hits = ("Target (gap)", oo), []
                elif sl_hit and tp_hit:
                    full = ("SL", sl) if sl_first else ("Target", tp)
                    if sl_first:
                        hits = []
                elif sl_hit:
                    full = ("SL", sl)
                    if sl_first:
                        hits = []
                elif tp_hit:
                    full = ("Target", tp)
            # MAE/MFE: বারের ভেতরের ক্রম অজানা, তাই ইঞ্জিনের নিজের ধারণা (sl_first) মেনেই হিসাব।
            # ট্রেড বন্ধের পরের দাম ধরা হয় না; এক্সিট বারের খোলার দাম ধরা হয়।
            f_full, a_full = (hh, ll) if side == 1 else (ll, hh)
            if full is None:
                fav, adv = f_full, a_full
            elif full[0] == "SL":
                fav, adv = (oo if sl_first else f_full), sl
            elif full[0] == "Target":
                fav = tp
                adv = oo if (sl is not None and ((ll <= sl) if side == 1 else (hh >= sl))) else a_full
            else:
                fav = adv = oo
            mfe = max(mfe, (fav - raw_e) * side)
            mae = max(mae, (raw_e - adv) * side)
            for kk, fp in hits:
                legs.append((books[kk][1], fp, s, books[kk][2]))
                rem -= books[kk][1]
                bk = kk + 1
            if full is not None:
                legs.append((rem, full[1], s, full[0]))
                reason, px = full
                exit_idx = jj
                break
            # টার্গেট লাগার পর SL সরানো (পরের বার থেকে কার্যকর)
            for kk, _ in hits:
                tag = books[kk][2]
                if tag == "T1":
                    m1 = a1.get("mode", "be")
                    if m1 == "be":
                        sl = tighten(sl, raw_e)
                    elif m1 == "lock":
                        sl = tighten(sl, raw_e * (1 + side * float(a1.get("val", 5) or 0) / 100.0))
                elif tag == "T2":
                    m2 = a2.get("mode", "t1")
                    if m2 == "t1" and books:
                        sl = tighten(sl, books[0][0])
                    elif m2 == "be":
                        sl = tighten(sl, raw_e)
            # বারের শেষে ট্রেইলিং / ব্রেকইভেন আপডেট
            peak = max(peak, hh) if side == 1 else min(peak, ll)
            if be_on and (peak - raw_e) * side / raw_e * 100 >= be_v:
                sl = tighten(sl, raw_e)
            fav = (peak - raw_e) * side
            lvl = None
            if tmode == "pct" and fav / raw_e * 100 >= trail_act:
                lvl = peak * (1 - side * trail_v / 100)
            elif tmode == "step" and trig_pts > 0:
                k = int(fav // trig_pts)
                if k >= 1:
                    lvl = (sl0 if sl0 is not None else raw_e) + side * k * move_pts
            if lvl is not None:
                sl = tighten(sl, lvl)
            if sl is not None and sl != last_sl:
                slh.append((int(t[jj + 1]) if jj + 1 < nb else s, float(sl)))
                last_sl = sl
            jj += 1
        is_open = False
        if exit_idx is None:
            exit_idx = jj - 1
            if now is not None and (now % 1440) < sq:
                is_open = True       # ট্রেড এখনো চলছে (পেপার/লাইভ) — চলতি দামে MTM
                reason = "OPEN"
                px = float(o[exit_idx]) if int(t[exit_idx]) == now else float(c[exit_idx])
            else:
                reason, px = "Day End", float(c[exit_idx])
            legs.append((rem, px, int(t[exit_idx]), reason))
        entry_eff = raw_e + side * slip
        qty = L * lot
        gross = 0.0
        costs = G["brokerage"]
        sell_turn = 0.0
        wsum = 0.0
        ltxt = []
        raw_w = 0.0
        for (q_l, p_l, t_l, why) in legs:
            raw_w += p_l * q_l
            q_ = q_l * lot
            ex_eff = p_l - side * slip
            gross += (ex_eff - entry_eff) * side * q_
            costs += G["brokerage"]
            sell_turn += (ex_eff if side == 1 else entry_eff) * q_
            wsum += ex_eff * q_
            ltxt.append(dict(q=int(q_l), px=round(ex_eff, 2), t=hm_str(t_l), why=why))
        costs += G["charges_pct"] / 100.0 * sell_turn
        exit_eff = (legs[0][1] - side * slip) if len(legs) == 1 else (wsum / qty if qty else 0.0)
        return dict(exit_t=int(t[exit_idx]), exit_px=float(px), entry_eff=entry_eff, exit_eff=exit_eff,
                    reason=reason, qty=qty, gross=float(gross), costs=float(costs), net=float(gross - costs), is_open=is_open, rem=int(rem), sl_now=(float(sl) if sl is not None else None), slh=slh, sl_init=(float(sl0) if sl0 is not None else None), tp_now=(float(tp) if tp is not None else None),
                    bars=int(exit_idx - j + 1), mae=float(mae / raw_e * 100) if raw_e else 0.0,
                    mfe=float(mfe / raw_e * 100) if raw_e else 0.0,
                    rret=float((raw_w / L - raw_e) * side / raw_e * 100) if raw_e and L else 0.0, legs=ltxt if len(ltxt) > 1 else None,
                    risk=float(abs(raw_e - sl0) * qty) if sl0 is not None else None,
                    ret=float((exit_eff - entry_eff) * side / entry_eff * 100) if entry_eff else 0.0)

    total = max(len(eligible), 1)
    for di, (d, ex) in enumerate(eligible):
        if progress and di % 15 == 0:
            progress(di / total)
        i0 = int(np.searchsorted(B.day, d, "left"))
        i1 = int(np.searchsorted(B.day, d, "right"))
        if i1 <= i0 or not (cases or pcases):
            continue
        cand = [np.flatnonzero(sg[i0:i1]) + i0 for (_, _, sg, _) in cases]
        evs = []
        if PM:
            dpi = int(np.searchsorted(D.du, d))
            for ci, cs in pcases:
                typ, sm = cs["opt"], cs["strike"]
                stks = D.strikes.get((ex, typ))
                lst = []
                if stks is not None and len(stks):
                    if sm["mode"] == "prem":
                        X, bd = float(sm["prem"]), float(sm.get("band", 30)) / 100.0
                        pool = [float(x) for x in stks]
                    else:
                        atm = int(np.argmin(np.abs(stks - D.dO[dpi])))
                        kk = atm + (int(float(sm["off"])) if typ == "CE" else -int(float(sm["off"])))
                        pool = [float(stks[kk])] if 0 <= kk < len(stks) else []
                        X, bd = None, None
                    for sk in pool:
                        rr = PS.get((ex, typ, sk), {}).get(ci)
                        if rr is None:
                            continue
                        a = np.flatnonzero((rr[0] >= d * 1440) & (rr[0] < (d + 1) * 1440))
                        for q in a:
                            pr = float(rr[1][q])
                            if X is not None and (pr <= 0 or abs(pr - X) > X * bd):
                                continue
                            lst.append((int(rr[0][q]), abs(pr - X) if X is not None else 0.0, sk, ci, cs))
                lst.sort(key=lambda e: (e[0], e[1]))
                evs.append(lst)
        cursor = -1
        ntr = 0
        dpnl = 0.0
        day_end = (d + 1) * 1440
        lot = D.exp_lot.get(ex, 1)
        while ntr < G["max_trades"]:
            if PM:
                bestp = None
                for lst in evs:
                    for e in lst:
                        if e[0] > cursor:
                            if bestp is None or e[0] < bestp[0]:
                                bestp = e
                            break
                if bestp is None:
                    break
                te, _, sk, ci, cs = bestp
                r0 = locate(ex, cs["opt"], sk, te)
                if r0 is None:
                    skipped["ডেটা নেই (কন্ট্র্যাক্টে বার নেই)"] += 1
                    cursor = te
                    continue
                ct, j = r0
                if ct[1][j] <= 0:
                    cursor = te
                    continue
                xs = set(PS[(ex, cs["opt"], sk)][ci][2].tolist())
                sp_i = max(int(np.searchsorted(D.t, te)) - 1, 0)
                spot_px = float(D.c[sp_i])
                stk = sk
            else:
                best = None
                for k in range(len(cases)):
                    idxs = cand[k]
                    if len(idxs) == 0:
                        continue
                    pos = int(np.searchsorted(B.e[idxs], cursor, side="right"))
                    if pos < len(idxs):
                        ii = int(idxs[pos])
                        if best is None or B.e[ii] < B.e[best[0]]:
                            best = (ii, k)
                if best is None:
                    break
                ii, k = best
                ci, cs, sg, xs = cases[k]
                te = int(B.e[ii]) + G["delay"] * int(D.base)          # delay>0: দেরিতে এন্ট্রি (স্ট্রেস টেস্ট)
                if now is not None and te > now:
                    break
                if te >= day_end:
                    break
                pk = pick(cs, ex, te, float(B.c[ii]))
                if pk is None or pk == "nostrike":
                    skipped["ডেটা নেই (কন্ট্র্যাক্টে বার নেই)" if pk is None else "স্ট্রাইক পাওয়া যায়নি"] += 1
                    cursor = te
                    continue
                ct, j, stk = pk
                if ct[1][j] <= 0:
                    cursor = te
                    continue
                spot_px = float(B.c[ii])
            r = run_trade(cs, xs, ct, j, te, day_end, lot)
            trades.append({
                "date": day_str(d), "case": cs["name"], "side": "%s %s %d" % (cs["action"], cs["opt"], stk),
                "expiry": day_str(ex), "in": hm_str(te), "out": hm_str(r["exit_t"]),
                "entry": round(r["entry_eff"], 2), "exit": round(r["exit_eff"], 2),
                "reason": r["reason"], "lots": int(r["qty"] // max(lot, 1)), "qty": r["qty"],
                "gross": round(r["gross"], 2), "costs": round(r["costs"], 2), "net": round(r["net"], 2),
                "ret": round(r["ret"], 2), "spot": round(spot_px, 2), "bars": r["bars"],
                "hold": int(r["exit_t"] - te), "ib": int(ii), "mae": round(r["mae"], 2), "mfe": round(r["mfe"], 2),
                "rret": round(r["rret"], 2),
                "r": round(r["net"] / r["risk"], 2) if r["risk"] else None,
                "dte": int(ex - d), "dow": DOWN[(d + 3) % 7], "legs": r["legs"],
                "open": bool(r["is_open"]), "rem": r["rem"], "slx": r.get("sl_now"), "tpx": r.get("tp_now"),
                "slh": [[hm_str(a), round(b, 2)] for a, b in r.get("slh", [])], "sl0": r.get("sl_init")})
            ntr += 1
            dpnl += r["net"]
            cursor = r["exit_t"]
            size_state["cum"] += r["net"]
            if (G["day_loss"] > 0 and dpnl <= -G["day_loss"]) or (G["day_profit"] > 0 and dpnl >= G["day_profit"]):
                break

    days_list = [day_str(d) for d, _ in eligible]
    cut = int(len(days_list) * G["split"] / 100.0)
    split_date = days_list[cut] if 0 < cut < len(days_list) else None
    tr_tr = [t for t in trades if split_date and t["date"] < split_date] if split_date else trades
    tr_te = [t for t in trades if split_date and t["date"] >= split_date] if split_date else []
    cap = G["capital"]
    out = {"stats": {"all": calc_stats(trades, cap), "train": calc_stats(tr_tr, cap), "test": calc_stats(tr_te, cap)},
           "meta": {"days": len(days_list), "from": days_list[0] if days_list else None,
                    "to": days_list[-1] if days_list else None, "split_date": split_date,
                    "skipped": skipped, "cases_active": (len(pcases) if PM else len(cases)),
                    "warn": (["আংশিক প্রফিট বুকিং চালু, কিন্তু লট ১ — ভাগ করা যায় না। লট কমপক্ষে ২ দিন।"]
                             if G["lots"] < 2 and any(c["on"] and c["tg"]["on"] for c in st["cases"]) else [])}}
    if light == "trades":
        return {"trades": trades, "meta": out["meta"]}
    if light:
        return out
    out["trades"] = trades
    out["an"] = analyze(trades, cap, getattr(D, "base", BASE_TF))
    try:
        out["an"]["adv"] = advanced_stats(trades, cap, days_list, (SRC_CACHE.get("v") or (None,))[0] if SRC_CACHE.get("D") is D else None, D)
    except Exception as e:
        out["an"]["adv"] = {"err": str(e)}
    eq, cum = [], 0.0
    for t in trades:
        cum += t["net"]
        eq.append(round(cum, 2))
    out["equity"] = eq
    yr, mo = {}, {}
    for t in trades:
        yr.setdefault(t["date"][:4], []).append(t)
        mo.setdefault(t["date"][:7], []).append(t)
    out["yearly"] = [dict(k=k, **calc_stats(v, cap)) for k, v in sorted(yr.items())]
    out["monthly"] = [dict(k=k, **calc_stats(v, cap)) for k, v in sorted(mo.items())]
    return out



def advanced_stats(trades, cap, days_list, Sx=None, D=None):
    """Sharpe/Sortino/Calmar/SQN, E-ratio, আন্ডারওয়াটার, রেজিম, মার্জিন-ROI"""
    if not trades:
        return {}
    by = {}
    for t in trades:
        by[t["date"]] = by.get(t["date"], 0.0) + t["net"]
    days = list(days_list) if days_list else sorted(by)
    dp = np.array([by.get(d, 0.0) for d in days], float)
    nd = len(dp)
    mu, sd = float(dp.mean()), float(dp.std(ddof=1)) if nd > 1 else 0.0
    dn = dp[dp < 0]
    dsd = float(np.sqrt((np.minimum(dp, 0) ** 2).mean())) if nd else 0.0
    sharpe = mu / sd * np.sqrt(252) if sd > 0 else 0.0
    sortino = mu / dsd * np.sqrt(252) if dsd > 0 else 0.0
    eq = np.cumsum(dp)
    pk = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    ddv = eq - pk
    mdd = float(ddv.min())
    yrs = max(nd / 252.0, 1e-9)
    ann = float(eq[-1]) / yrs
    calmar = ann / abs(mdd) if mdd < 0 else 0.0
    # আন্ডারওয়াটার
    uw = best = 0
    for x in ddv:
        uw = uw + 1 if x < 0 else 0
        best = max(best, uw)
    # SQN (R না থাকলে নেট ব্যবহার)
    rs = np.array([t["r"] for t in trades if t.get("r") is not None], float)
    base_r = rs if len(rs) >= max(10, 0.8 * len(trades)) else np.array([t["net"] for t in trades], float)
    sqn = float(np.sqrt(len(base_r)) * base_r.mean() / base_r.std(ddof=1)) if len(base_r) > 1 and base_r.std(ddof=1) > 0 else 0.0
    # E-ratio = গড় MFE ÷ গড় MAE (শতাংশে, দুটোই ধনাত্মক ধরে)
    mfe = np.array([abs(t["mfe"]) for t in trades], float)
    mae = np.array([abs(t["mae"]) for t in trades], float)
    eratio = float(mfe.mean() / mae.mean()) if mae.mean() > 0 else 0.0
    dep = np.array([t["entry"] * t["qty"] for t in trades], float)
    out = {"sharpe": float(sharpe), "sortino": float(sortino), "calmar": float(calmar), "sqn": sqn, "eratio": eratio,
           "ann_net": ann, "ann_pct": 100.0 * ann / cap if cap else 0.0, "uw_max": int(best), "uw_now": int(uw), "days": nd,
           "avg_deployed": float(dep.mean()), "roi_deployed": float(100.0 * sum(t["net"] for t in trades) / dep.sum()) if dep.sum() > 0 else 0.0,
           "daily_win": float(100.0 * (dp > 0).sum() / max(1, (dp != 0).sum()))}
    # রেজিম
    reg = {}
    if Sx is not None:
        def bucket(name, cuts, labels):
            a = Sx.get(name)
            if a is None:
                return None
            g = {}
            for t in trades:
                try:
                    v = float(a[t["ib"]])
                except Exception:
                    continue
                if not np.isfinite(v):
                    continue
                k = labels[int(np.searchsorted(cuts, v, side="right"))]
                g.setdefault(k, []).append(t)
            rows = []
            for k in labels:
                if k in g:
                    st = calc_stats(g[k], cap)
                    rows.append({"k": k, "n": st["n"], "win": st["win"], "net": st["net"], "pf": st["pf"], "exp": st["exp"]})
            return rows
        try:
            reg["adx"] = bucket("adx", [20, 30], ["ADX<20 (রেঞ্জ)", "ADX 20-30", "ADX>30 (ট্রেন্ড)"])
            a = Sx.get("atr")
            if a is not None:
                vv = np.array([a[t["ib"]] for t in trades], float)
                ok = vv[np.isfinite(vv)]
                if len(ok) >= 9:
                    q1, q2 = np.percentile(ok, [33.3, 66.7])
                    reg["atr"] = bucket("atr", [q1, q2], ["কম অস্থিরতা (ATR)", "মাঝারি অস্থিরতা", "বেশি অস্থিরতা"])
            reg["rsi"] = bucket("rsi", [40, 60], ["RSI<40", "RSI 40-60", "RSI>60"])
        except Exception:
            pass
    # গ্যাপ রেজিম ও টাইম-ডিকে E-ratio ও টপ-৫ DD (স্পট ডেটা D লাগে)
    try:
        if D is not None:
            gg = {}
            for t in trades:
                j = int(np.searchsorted(D.du, day_int(t["date"])))
                if j <= 0 or j >= len(D.du):
                    continue
                gp = (D.dO[j] / D.dC[j - 1] - 1) * 100
                k = "Gap-Up (>0.3%)" if gp > 0.3 else ("Gap-Down (<-0.3%)" if gp < -0.3 else "Flat গ্যাপ")
                gg.setdefault(k, []).append(t)
            rows = []
            for k in ("Gap-Up (>0.3%)", "Flat গ্যাপ", "Gap-Down (<-0.3%)"):
                if k in gg:
                    st_ = calc_stats(gg[k], cap)
                    rows.append({"k": k, "n": st_["n"], "win": st_["win"], "net": st_["net"], "pf": st_["pf"], "exp": st_["exp"]})
            reg["gap"] = rows
            ed = []
            for h_ in (5, 15, 30, 60):
                fav, adv, n_ = [], [], 0
                for t in trades:
                    sp_ = t["side"].split()
                    if len(sp_) < 3:
                        continue
                    ct = D.opt.get((day_int(t["expiry"]), sp_[1], float(sp_[2])))
                    if ct is None:
                        continue
                    te = day_int(t["date"]) * 1440 + hm(t["in"])
                    j0 = int(np.searchsorted(ct[0], te))
                    if j0 >= len(ct[0]) or ct[0][j0] != te:
                        continue
                    j1 = int(np.searchsorted(ct[0], te + h_, "right"))
                    e0 = float(ct[1][j0])
                    if e0 <= 0 or j1 <= j0:
                        continue
                    hh, ll = float(ct[2][j0:j1].max()), float(ct[3][j0:j1].min())
                    sd = 1 if sp_[0] == "BUY" else -1
                    f_, a_ = ((hh - e0), (e0 - ll)) if sd == 1 else ((e0 - ll), (hh - e0))
                    fav.append(100 * max(f_, 0) / e0)
                    adv.append(100 * max(a_, 0) / e0)
                if fav:
                    ed.append({"min": h_, "n": len(fav), "mfe": float(np.mean(fav)), "mae": float(np.mean(adv)), "ratio": float(np.mean(fav) / np.mean(adv)) if np.mean(adv) > 0 else 0.0})
            out["eratio_decay"] = ed
            # টপ-৫ ড্রডাউন + স্থবিরতা + সিঙ্কড চার্ট
            dts = list(days_list) if days_list else sorted(by)
            eqd = np.cumsum(dp)
            pk = np.maximum.accumulate(np.r_[0.0, eqd])[1:]
            ddd = eqd - pk
            per, i = [], 0
            while i < len(ddd):
                if ddd[i] < 0:
                    j = i
                    while j < len(ddd) and ddd[j] < 0:
                        j += 1
                    seg = ddd[i:j]
                    k_ = int(np.argmin(seg)) + i
                    per.append({"from": dts[i - 1] if i > 0 else dts[0], "trough": dts[k_], "to": dts[j] if j < len(dts) else None, "dd": float(seg.min()), "days": int(j - i + 1)})
                    i = j
                else:
                    i += 1
            per.sort(key=lambda x: x["dd"])
            out["top_dd"] = per[:5]
            last_pk = 0
            stag = 0
            for i in range(len(eqd)):
                if eqd[i] >= pk[i] - 1e-9 and eqd[i] > (eqd[last_pk] if last_pk < len(eqd) else -1e18) - 1e-9:
                    stag = max(stag, (day_int(dts[i]) - day_int(dts[last_pk])))
                    last_pk = i
            stag = max(stag, day_int(dts[-1]) - day_int(dts[last_pk]))
            out["stagnation_days"] = int(stag)
            sp0 = None
            spot_b = []
            for dd_ in dts:
                j = int(np.searchsorted(D.du, day_int(dd_)))
                v = float(D.dC[min(j, len(D.dC) - 1)])
                sp0 = v if sp0 is None else sp0
                spot_b.append(round(cap * (v / sp0 - 1), 0))
            step = max(1, len(dts) // 400)
            out["sync"] = {"dates": dts[::step], "eq": [round(float(x), 0) for x in eqd[::step]], "dd": [round(float(x), 0) for x in ddd[::step]], "spot": spot_b[::step]}
    except Exception as e:
        out["adv_err"] = str(e)
    out["regime"] = {k: v for k, v in reg.items() if v}
    # হিটম্যাপ: সময়-স্লট × বার, মাস × বছর
    hmap = {}
    for t in trades:
        k = (t["dow"], t["in"][:2] + ":" + ("00" if int(t["in"][3:5]) < 30 else "30"))
        hmap[k] = hmap.get(k, 0.0) + t["net"]
    out["heat_slot"] = [{"dow": k[0], "slot": k[1], "net": v} for k, v in hmap.items()]
    mh = {}
    for t in trades:
        mh[(t["date"][:4], int(t["date"][5:7]))] = mh.get((t["date"][:4], int(t["date"][5:7])), 0.0) + t["net"]
    out["heat_month"] = [{"y": k[0], "m": k[1], "net": v} for k, v in sorted(mh.items())]
    out["scatter"] = [[round(abs(t["mae"]), 1), round(abs(t["mfe"]), 1), 1 if t["net"] > 0 else 0] for t in trades][:1500]
    return out


def stress_test(D, raw):
    """স্লিপেজ/খরচ/দেরিতে-এন্ট্রি স্ট্রেস: আসল ইঞ্জিনে বাড়িয়ে আবার চালানো"""
    base = norm_settings(raw)
    rows = []
    for lab, ms, mb, dl in (("এখনকার খরচ", 1, 1, 0), ("স্লিপেজ ×2", 2, 1, 0), ("স্লিপেজ ×3", 3, 1, 0), ("স্লিপেজ ×3 + ব্রোকারেজ ×1.5", 3, 1.5, 0),
                            ("+১ ক্যান্ডেল দেরিতে এন্ট্রি", 1, 1, 1), ("+২ ক্যান্ডেল দেরি + স্লিপেজ ×2", 2, 1, 2)):
        s = copy.deepcopy(base)
        s["general"]["slip"] = float(base["general"]["slip"]) * ms
        s["general"]["brokerage"] = float(base["general"]["brokerage"]) * mb
        s["general"]["delay"] = dl
        r = backtest(D, s, light=True)
        a = r["stats"]["all"]
        rows.append({"k": lab, "n": a["n"], "net": a["net"], "pf": a["pf"], "win": a["win"], "dd": a["dd"], "exp": a["exp"]})
    return {"rows": rows}



