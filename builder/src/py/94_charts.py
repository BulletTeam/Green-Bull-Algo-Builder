# =====================================================================
#  একটি ট্রেডের চার্ট ডেটা (স্পট + প্রিমিয়াম)
# =====================================================================

PRICE_OVERLAY = {"ma1", "ma2", "ma3", "st1", "st2", "vwap", "bbup", "bbmid", "bblow", "donhi", "donlo", "orbh", "orbl",
                 "orbm", "dayo", "dayh", "dayl", "pdh", "pdl", "pdc", "pivp", "r1", "r2", "r3", "s1", "s2", "s3",
                 "cprt", "cprb", "swh", "swl"}
PLAIN_PRICE = {"close", "open", "high", "low", "hl2", "hlc3", "ohlc4"}


def chart_data(D, raw, body):
    st = norm_settings(raw)
    G = st["general"]
    S, B = compute_sources(D, G["tf"], st["ind"])
    d = day_int(body["date"])
    i0 = int(np.searchsorted(B.day, d, "left"))
    i1 = int(np.searchsorted(B.day, d, "right"))
    if i1 <= i0:
        raise ValueError("এই দিনের স্পট ডেটা নেই")
    labels = {x[0]: x[1] for x in SOURCES}
    cs = None
    for c in st["cases"]:
        if c["name"] == body.get("case"):
            cs = c
    cases = [cs] if cs else [c for c in st["cases"] if c["on"]]
    ids, hl = [], {}
    for c in cases:
        for part in ("entry", "exit"):
            for sl_ in c[part]:
                if not sl_["on"]:
                    continue
                for k in ("l", "r"):
                    v = sl_[k]
                    if v != "number" and v in S and v not in PLAIN_PRICE and v not in ids:
                        ids.append(v)
                if sl_["r"] == "number" and sl_["l"] not in PRICE_OVERLAY and sl_["l"] not in PLAIN_PRICE:
                    hl.setdefault(sl_["l"], [])
                    if float(sl_["num"]) not in hl[sl_["l"]]:
                        hl[sl_["l"]].append(float(sl_["num"]))
    PMC = G.get("chart") == "prem"
    pov, ppan = [], []
    if PMC:
        sd_ = body["side"].split()
        ct_ = D.opt.get((day_int(body["expiry"]), sd_[1], float(sd_[2])))
        if ct_ is not None:
            V = contract_view(D, ct_)
            S2, B2 = compute_sources(V, G["tf"], st["ind"])
            j0 = int(np.searchsorted(B2.day, d, "left"))
            j1 = int(np.searchsorted(B2.day, d, "right"))
            T2 = B2.t[j0:j1] * 60

            def series2(arr):
                a = arr[j0:j1]
                return [dict(t=int(T2[k]), v=float(a[k])) for k in range(len(a)) if np.isfinite(a[k])]
            pov = [dict(id=v, name=labels.get(v, v), data=series2(S2[v])) for v in ids if v in PRICE_OVERLAY]
            ppan = [dict(id=v, name=labels.get(v, v), data=series2(S2[v]), hl=hl.get(v, []))
                    for v in ids if v not in PRICE_OVERLAY][:4]
            V._cache.clear()
    T = B.t[i0:i1] * 60
    spot = [dict(t=int(T[k]), o=float(B.o[i0 + k]), h=float(B.h[i0 + k]), l=float(B.l[i0 + k]), c=float(B.c[i0 + k]))
            for k in range(i1 - i0)]

    def series(arr):
        a = arr[i0:i1]
        return [dict(t=int(T[k]), v=float(a[k])) for k in range(len(a)) if np.isfinite(a[k])]
    overlays = [dict(id=v, name=labels.get(v, v), data=series(S[v])) for v in ids if v in PRICE_OVERLAY]
    panes = [dict(id=v, name=labels.get(v, v), data=series(S[v]), hl=hl.get(v, []))
             for v in ids if v not in PRICE_OVERLAY][:4]
    if PMC:
        overlays, panes = [], []
    # প্রিমিয়াম
    side = body["side"].split()
    typ, stk = side[1], float(side[2])
    ex = day_int(body["expiry"])
    ct = D.opt.get((ex, typ, stk))
    prem, lines = [], []
    te = d * 1440 + hm(body["in"])
    tx = d * 1440 + hm(body["out"])
    if ct is not None:
        m = (ct[0] >= d * 1440) & (ct[0] < (d + 1) * 1440)
        idx = np.flatnonzero(m)
        prem = [dict(t=int(ct[0][k]) * 60, o=float(ct[1][k]), h=float(ct[2][k]), l=float(ct[3][k]), c=float(ct[4][k])) for k in idx]
        j = np.flatnonzero(ct[0] == te)
        if len(j) and cs:
            raw_e = float(ct[1][j[0]])
            sd = 1 if cs["action"] == "BUY" else -1
            sm, sv = cs["sl"]["mode"], float(cs["sl"]["val"])
            tm, tv = cs["tp"]["mode"], float(cs["tp"]["val"])
            slv = tpv = None
            if sm == "pct":
                slv = raw_e * (1 - sd * sv / 100)
            elif sm == "pts":
                slv = raw_e - sd * sv
            if tm == "pct":
                tpv = raw_e * (1 + sd * tv / 100)
            elif tm == "pts":
                tpv = raw_e + sd * tv
            elif tm == "rr" and slv is not None:
                tpv = raw_e + sd * tv * abs(raw_e - slv)
            lines.append(dict(v=raw_e, name="এন্ট্রি", color="#4f9cff"))
            if slv is not None:
                lines.append(dict(v=slv, name="SL", color="#ff6161"))
            tgc = cs.get("tg", {})
            if tgc.get("on") and G["lots"] >= 2:
                u_ = tgc.get("unit", "pct")

                def tgp(v_):
                    if u_ == "pct":
                        return raw_e * (1 + sd * v_ / 100)
                    if u_ == "pts":
                        return raw_e + sd * v_
                    return raw_e + sd * v_ * abs(raw_e - slv) if slv is not None else None
                p1 = tgp(float(tgc["t1"]["val"]))
                if p1 is not None:
                    lines.append(dict(v=p1, name="T1", color="#f5c542"))
                if tgc["t2"].get("on"):
                    p2 = tgp(float(tgc["t2"]["val"]))
                    if p2 is not None:
                        lines.append(dict(v=p2, name="T2", color="#f59e42"))
            if tpv is not None:
                lines.append(dict(v=tpv, name="টার্গেট", color="#2ecc71"))
    # স্পটে মার্কার: যে বারে সিগন্যাল শেষ / যে বারে এক্সিট
    def spot_idx(tm_):
        k = int(np.searchsorted(B.e[i0:i1], tm_, side="left"))
        return min(k, len(spot) - 1)
    ke = spot_idx(te)
    kx = min(int(np.searchsorted(B.t[i0:i1], tx, side="right")) - 1, len(spot) - 1)
    kx = max(kx, 0)
    marks = {"spot": [dict(t=spot[ke]["t"], p=spot[ke]["c"], k="in"), dict(t=spot[kx]["t"], p=spot[kx]["c"], k="out")],
             "prem": [dict(t=te * 60, p=float(body.get("entry", 0)), k="in"), dict(t=tx * 60, p=float(body.get("exit", 0)), k="out")]}
    for lg in (body.get("legs") or [])[:-1]:       # শেষ লেগ = মূল এক্সিট; বাকিগুলো আংশিক বুকিং
        try:
            marks["prem"].append(dict(t=(d * 1440 + hm(lg["t"])) * 60, p=float(lg["px"]), k="part", q=int(lg["q"]), why=lg["why"]))
        except (KeyError, ValueError, TypeError):
            pass
    marks["slh"] = [dict(t=(d * 1440 + hm(a)) * 60, p=float(b)) for a, b in (body.get("slh") or []) if isinstance(a, str)]
    if body.get("sl0") is not None:
        marks["sl0"] = float(body["sl0"])
    # --- ভলিউম, FVG বক্স, মাল্টি-ডে প্রেক্ষাপট ---
    vol = []
    if float(np.nansum(B.v[i0:i1])) > 0:
        vol = [dict(t=int(T[k]), v=float(B.v[i0 + k]), up=bool(B.c[i0 + k] >= B.o[i0 + k])) for k in range(i1 - i0)]
    fvg = []
    for k in range(2, i1 - i0):
        a_, b_, c_ = i0 + k - 2, i0 + k - 1, i0 + k
        if B.l[c_] > B.h[a_]:
            lo_, hi_, kind = B.h[a_], B.l[c_], "bull"
        elif B.h[c_] < B.l[a_]:
            lo_, hi_, kind = B.h[c_], B.l[a_], "bear"
        else:
            continue
        end = i1 - i0 - 1
        for m_ in range(k + 1, i1 - i0):           # গ্যাপ ভরাট হলে বক্স সেখানেই শেষ
            if (kind == "bull" and B.l[i0 + m_] <= lo_) or (kind == "bear" and B.h[i0 + m_] >= hi_):
                end = m_
                break
        if (hi_ - lo_) / max(B.c[c_], 1e-9) * 100 >= 0.05:
            fvg.append(dict(a=int(T[k - 2]), b=int(T[end]), lo=float(lo_), hi=float(hi_), k=kind))
    ctx = []
    days_prev = [x for x in np.unique(B.day[:i0])][-2:] if i0 > 0 else []
    for dd_ in days_prev:
        p0 = int(np.searchsorted(B.day, dd_, "left")); p1 = int(np.searchsorted(B.day, dd_, "right"))
        ctx.append(dict(date=day_str(int(dd_)), bars=[[float(B.o[q]), float(B.h[q]), float(B.l[q]), float(B.c[q])] for q in range(p0, p1)]))
    ctx.append(dict(date=day_str(d), bars=[[float(B.o[q]), float(B.h[q]), float(B.l[q]), float(B.c[q])] for q in range(i0, i1)], today=True))
    pd_ = ctx[-2]["bars"] if len(ctx) >= 2 else None
    pdh = max(b_[1] for b_ in pd_) if pd_ else None
    pdl = min(b_[2] for b_ in pd_) if pd_ else None
    return {"spot": spot, "overlays": overlays, "panes": panes, "prem": prem, "lines": lines, "marks": marks,
            "vol": vol, "fvg": fvg[-12:], "ctx": ctx, "pdh": pdh, "pdl": pdl,
            "pov": pov, "ppan": ppan, "pmode": PMC,
            "tf": int(G["tf"]), "base": getattr(D, "base", BASE_TF), "open": OPEN_MIN * 60 + d * 86400}

