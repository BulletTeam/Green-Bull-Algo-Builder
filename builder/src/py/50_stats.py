# =====================================================================
#  পরিসংখ্যান
# =====================================================================

def calc_stats(trs, capital):
    n = len(trs)
    z = dict(n=0, win=0.0, net=0.0, gross=0.0, costs=0.0, pf=0.0, exp=0.0, avg_w=0.0, avg_l=0.0,
             dd=0.0, dd_pct=0.0, mcl=0, mcw=0, best=0.0, worst=0.0, rr=0.0, rec=0.0, hold=0.0,
             avg_r=None, pm=0.0, nm=0, dd_len=0)
    if n == 0:
        return z
    pn = np.array([t["net"] for t in trs], float)
    wins, loss = pn[pn > 0], pn[pn <= 0]
    lsum = abs(loss.sum())
    pf = (wins.sum() / lsum) if lsum > 0 else (99.0 if wins.sum() > 0 else 0.0)
    eq = np.cumsum(pn)
    peaks = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    ddv = eq - peaks
    dd = float(ddv.min())
    mcl = mcw = cur = cw = 0
    for x in pn:
        cur = cur + 1 if x <= 0 else 0
        cw = cw + 1 if x > 0 else 0
        mcl, mcw = max(mcl, cur), max(mcw, cw)
    dl = best_dl = 0
    for x in ddv:
        dl = dl + 1 if x < 0 else 0
        best_dl = max(best_dl, dl)
    aw = float(wins.mean()) if len(wins) else 0.0
    al = float(loss.mean()) if len(loss) else 0.0
    rs = [t["r"] for t in trs if t.get("r") is not None]
    mon = {}
    for t in trs:
        mon[t["date"][:7]] = mon.get(t["date"][:7], 0.0) + t["net"]
    pm = 100.0 * sum(1 for v in mon.values() if v > 0) / len(mon)
    net = float(pn.sum())
    return dict(n=n, win=float(100 * (pn > 0).mean()), net=net,
                gross=float(sum(t["gross"] for t in trs)), costs=float(sum(t["costs"] for t in trs)),
                pf=float(min(pf, 99.0)), exp=float(pn.mean()), avg_w=aw, avg_l=al,
                dd=dd, dd_pct=float(100 * dd / capital) if capital else 0.0, mcl=int(mcl), mcw=int(mcw),
                best=float(pn.max()), worst=float(pn.min()),
                rr=float(aw / abs(al)) if al < 0 else 0.0, rec=float(net / abs(dd)) if dd < 0 else 0.0,
                hold=float(np.mean([t.get("hold", 0) for t in trs])),
                avg_r=float(np.mean(rs)) if rs else None, pm=float(pm), nm=len(mon), dd_len=int(best_dl))


def _pctl(a, q):
    return float(np.percentile(a, q)) if len(a) else 0.0


DOWN = ["সোম", "মঙ্গল", "বুধ", "বৃহ", "শুক্র", "শনি", "রবি"]


def analyze(trades, cap, base):
    """ট্রেড তালিকা থেকে বিস্তারিত বিশ্লেষণ: টাইমিং, DD, RR, MAE/MFE, SL/TP what-if"""
    if not trades:
        return {}
    out = {}

    def grp(keyf, order=None):
        g = {}
        for t in trades:
            g.setdefault(keyf(t), []).append(t)
        ks = order if order else sorted(g)
        return [dict(k=k, **calc_stats(g[k], cap)) for k in ks if k in g]

    def mins(t):
        return int(t["in"][:2]) * 60 + int(t["in"][3:5])

    def slotk(t):
        m = mins(t)
        b = (m - OPEN_MIN) // 30 * 30 + OPEN_MIN
        return "%s-%s" % (hm_str(b), hm_str(b + 30))

    def bucket(v, edges, labels):
        for e, l in zip(edges, labels):
            if v <= e:
                return l
        return labels[-1]

    out["by_slot"] = grp(slotk)
    out["by_dow"] = grp(lambda t: t["dow"], DOWN)
    out["by_reason"] = grp(lambda t: t["reason"])
    out["by_case"] = grp(lambda t: t["case"])
    out["by_type"] = grp(lambda t: t["side"].split()[1])
    dl = ["০ দিন (এক্সপায়ারি দিন)", "১ দিন", "২ দিন", "৩-৫ দিন", "৬-১০ দিন", "১১-২০ দিন", "২১+ দিন"]
    out["by_dte"] = grp(lambda t: bucket(t["dte"], [0, 1, 2, 5, 10, 20], dl), dl)
    hl = ["≤৯ মিনিট", "১০-১৯ মিনিট", "২০-৩৯ মিনিট", "৪০-৫৯ মিনিট", "১-২ ঘণ্টা", "২ ঘণ্টার বেশি"]
    out["by_hold"] = grp(lambda t: bucket(t["hold"], [9, 19, 39, 59, 120], hl), hl)
    pl = ["<১০০", "১০০-২৫০", "২৫০-৪০০", "৪০০-৭০০", "৭০০-১০০০", "১০০০+"]
    out["by_prem"] = grp(lambda t: bucket(t["entry"], [100, 250, 400, 700, 1000], pl), pl)

    # ড্রডাউন
    pn = np.array([t["net"] for t in trades], float)
    eq = np.cumsum(pn)
    pk = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    dd = eq - pk
    k = int(np.argmin(dd))
    pi = int(np.argmax(eq[:k + 1] == pk[k])) if dd[k] < 0 else 0
    rec = None
    for j in range(k + 1, len(eq)):
        if eq[j] >= pk[k]:
            rec = j
            break
    out["dd"] = [round(float(x), 1) for x in dd]
    out["dd_info"] = {"max": float(dd[k]), "pct": float(100 * dd[k] / cap) if cap else 0.0,
                      "peak_date": trades[pi]["date"] if dd[k] < 0 else None, "trough_date": trades[k]["date"],
                      "rec_date": trades[rec]["date"] if rec is not None else None,
                      "trades_in_dd": int(k - pi), "recovered": rec is not None}
    # দিন অনুযায়ী
    dm = {}
    for t in trades:
        dm[t["date"]] = dm.get(t["date"], 0.0) + t["net"]
    dv = [dm[d] for d in sorted(dm)]
    cl = mx = 0
    for x in dv:
        cl = cl + 1 if x <= 0 else 0
        mx = max(mx, cl)
    out["days"] = {"n": len(dv), "win": float(100 * np.mean([x > 0 for x in dv])), "best": float(max(dv)),
                   "worst": float(min(dv)), "avg": float(np.mean(dv)), "mcl": int(mx),
                   "tpd": float(len(trades) / len(dv))}
    # MAE / MFE
    mae = np.array([t["mae"] for t in trades], float)
    mfe = np.array([t["mfe"] for t in trades], float)
    ret = np.array([t["ret"] for t in trades], float)
    w = pn > 0
    rr_ = np.array([t.get("rret", t["ret"]) for t in trades], float)    # স্লিপেজ ছাড়া, MFE-র সাথে একই ভিত্তি
    gb = (mfe - rr_)[w]
    out["exc"] = {
        "mae_w": [_pctl(mae[w], q) for q in (50, 75, 90, 100)],
        "mae_l": [_pctl(mae[~w], q) for q in (50, 75, 90, 100)],
        "mfe_w": [_pctl(mfe[w], q) for q in (25, 50, 75, 90)],
        "mfe_l": [_pctl(mfe[~w], q) for q in (25, 50, 75, 90)],
        "give_med": _pctl(gb, 50), "give_avg": float(gb.mean()) if len(gb) else 0.0,
        "l_touch": {str(x): float(100 * np.mean(mfe[~w] >= x)) if (~w).any() else 0.0 for x in (5, 10, 20, 30)},
        "nw": int(w.sum()), "nl": int((~w).sum())}
    return out


