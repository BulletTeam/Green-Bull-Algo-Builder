# =====================================================================
#  পোর্টফোলিও ব্যাকটেস্টার: একাধিক স্ট্র্যাটেজি একসাথে + কোরিলেশন ম্যাট্রিক্স
# =====================================================================
def portfolio_run(D, payload, progress=None):
    items = payload.get("items") or []
    if len(items) < 2:
        raise ValueError("পোর্টফোলিওতে কমপক্ষে ২টি স্ট্র্যাটেজি দিন")
    cap = float(payload.get("capital") or 0)
    d_from, d_to = payload.get("from") or "", payload.get("to") or ""
    runs = []
    for k, it in enumerate(items):
        w = float(it.get("weight") or 1.0)
        raw = copy.deepcopy(it["settings"])
        if d_from:
            raw.setdefault("general", {})["from"] = d_from
        if d_to:
            raw.setdefault("general", {})["to"] = d_to
        if it.get("type") == "sell":
            r = sell_backtest(D, raw)
        else:
            r = backtest(D, raw, light="trades")
        trs = r["trades"]
        runs.append({"name": str(it.get("name") or "S%d" % (k + 1)), "w": w, "trades": trs,
                     "cap": float((raw.get("general") or {}).get("capital") or 100000)})
        if progress:
            progress((k + 1) / (len(items) + 0.3))
    if cap <= 0:
        cap = sum(r["cap"] for r in runs)
    days = sorted({t["date"] for r in runs for t in r["trades"]})
    if len(days) < 5:
        raise ValueError("যৌথ ট্রেডিং-দিন খুব কম (%d)। তারিখ-সীমা বাড়ান।" % len(days))
    idx = {d: i for i, d in enumerate(days)}
    M = np.zeros((len(runs), len(days)))
    merged = []
    for a, r in enumerate(runs):
        for t in r["trades"]:
            M[a, idx[t["date"]]] += t["net"] * r["w"]
            u = dict(t)
            u["net"] = t["net"] * r["w"]
            u["gross"] = t.get("gross", t["net"]) * r["w"]
            u["costs"] = t.get("costs", 0.0) * r["w"]
            u["strat"] = r["name"]
            merged.append(u)
    merged.sort(key=lambda t: (t["date"], t.get("in", "")))
    comb = M.sum(axis=0)

    def dd_of(x):
        e = np.cumsum(x)
        return float((e - np.maximum.accumulate(np.r_[0.0, e])[1:]).min())

    def sh(x):
        sd = x.std(ddof=1) if len(x) > 1 else 0.0
        return float(x.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
    rows = []
    for a, r in enumerate(runs):
        x = M[a]
        yrs = max(len(days) / 252.0, 1e-9)
        dd = dd_of(x)
        rows.append({"name": r["name"], "w": r["w"], "n": len(r["trades"]), "net": float(x.sum()), "dd": dd, "sharpe": sh(x),
                     "calmar": float(x.sum() / yrs / abs(dd)) if dd < 0 else 0.0})
    # কোরিলেশন (দৈনিক P&L)
    C = np.corrcoef(M) if M.shape[0] > 1 else np.ones((1, 1))
    C = np.nan_to_num(C, nan=0.0)
    yrs = max(len(days) / 252.0, 1e-9)
    cdd = dd_of(comb)
    st = calc_stats(merged, cap)
    sum_dd = float(sum(r["dd"] for r in rows))
    out = {"days": days, "names": [r["name"] for r in runs], "rows": rows, "corr": [[round(float(v), 3) for v in row] for row in C],
           "equity": [round(float(v), 2) for v in np.cumsum(comb)],
           "series": [[round(float(v), 2) for v in np.cumsum(M[a])] for a in range(len(runs))],
           "combined": {"n": st["n"], "net": st["net"], "win": st["win"], "pf": st["pf"], "dd": cdd, "sharpe": sh(comb),
                        "calmar": float(comb.sum() / yrs / abs(cdd)) if cdd < 0 else 0.0, "sum_dd": sum_dd,
                        "dd_benefit_pct": float(100 * (1 - cdd / sum_dd)) if sum_dd < 0 else 0.0, "cap": cap},
           "note": "ওজন = লট-গুণক ধরে নেট রৈখিকভাবে বাড়ানো (আনুমানিক)। কোরিলেশন দৈনিক P&L-এর পিয়ার্সন।"}
    return out


