# =====================================================================
#  AI Advisor — গণিত-ভিত্তিক সাজেশন ইঞ্জিন
#  কোনো অনুমান-সূত্র নয়: প্রতিটি প্রার্থী মান আসল ব্যাকটেস্ট ইঞ্জিনে চালিয়ে মাপা হয় (ট্রেইলিং, ব্রেকইভেন,
#  আংশিক বুকিং, খরচ, স্লিপেজ সহ)। বাছাই হয় শুধু TRAIN অংশে, TEST অংশ আলাদা রেখে যাচাই হয়।
# =====================================================================

def inr_(x):
    return "₹{:,}".format(int(round(x)))


def _canon(o):
    """৩.০ ও ৩ একই ধরতে: পূর্ণসংখ্যার মতো ফ্লোট → int (তুলনার চাবির জন্য)"""
    if isinstance(o, dict):
        return {k: _canon(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_canon(v) for v in o]
    if isinstance(o, float) and o == int(o):
        return int(o)
    return o


def _ck(s):
    return json.dumps(_canon(s), sort_keys=True)


_BN = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


def parse_request(text):
    """ইউজারের লেখা অনুরোধ (বাংলা/English/Banglish) → লক্ষ্য ও শর্ত। কীওয়ার্ড-নিয়মভিত্তিক, কোনো LLM নয়।
    ফেরত: dict(goal, cut, dd_cap_pct, dd_cap_abs, freeze, disable, sl_max, sl_min, strike_rule, keep_trades,
              understood[], ignored[])"""
    import re
    R = {"goal": "balanced", "cut": None, "dd_cap_pct": None, "dd_cap_abs": None, "freeze": set(), "disable": set(),
         "sl_max": None, "sl_min": None, "strike_rule": None, "keep_trades": None, "understood": [], "ignored": []}
    raw = (text or "").translate(_BN).lower()
    clauses = [c.strip() for c in re.split(r"[।\.\n;]+|\s+আর\s+|\s+and\s+|,", raw) if c.strip()]
    P = {"sl": ["stop loss", "stoploss", " sl", "sl ", "এসএল", "স্টপ"], "tp": ["target", "টার্গেট", " tp", "ফাইনাল"],
         "trail": ["trail", "ট্রেইল", "ট্রেল", "ট্রেলিং"], "be": ["breakeven", "break even", "ব্রেকইভেন", "ব্রেক ইভেন"],
         "tg": ["t1", "t2", "tg", "partial", "আংশিক", "প্রফিট বুক"], "strike": ["strike", "স্ট্রাইক"],
         "ts": ["time stop", "timestop", "টাইম স্টপ", "টাইম-স্টপ", "টাইমস্টপ"],
         "window": ["entry time", "এন্ট্রি সময়", "এন্ট্রির সময়", "window", "উইন্ডো"],
         "mt": ["max trade", "দিনে ট্রেড", "ডেইলি ট্রেড"]}
    NAME = {"sl": "SL", "tp": "টার্গেট", "trail": "ট্রেইলিং", "be": "ব্রেকইভেন", "tg": "T1/T2", "strike": "স্ট্রাইক",
            "ts": "টাইম-স্টপ", "window": "এন্ট্রি সময়", "mt": "দিনে ট্রেড-সংখ্যা"}
    NEG = ["বদলিও না", "বদলাবি না", "বদলাবে না", "বদলাস না", "ছুঁস না", "ছুঁয়ো না", "ছুয়ো না", "বদল করো না", "বদল করিস না", "ফিক্সড",
           "fixed", "যেমন আছে", "don't change", "dont change", "do not change", "unchanged", "keep as", "থাকুক", "লক"]
    OFF = ["চাই না", "চাই-না", "লাগবে না", "লাগবেনা", "বাদ", "নেই", "বন্ধ", "off", "remove", "disable", "without", "ছাড়া"]
    DDW = ["drawdown", "ড্রডাউন", "ড্র-ডাউন", "dd", "ঝুঁকি", "risk", "লস কম", "ক্ষতি কম"]
    LESS = ["কম", "reduce", "lower", "less", "ছোট", "নামা", "কমা", "কমিয়ে", "decrease", "minimi"]
    for c in clauses:
        hit = False
        num = re.findall(r"(\d+(?:\.\d+)?)\s*(%|শতাংশ|percent|টাকা|rs|₹)?", c)
        nums = [(float(a), b) for a, b in num]
        # --- ট্রেড সংখ্যা কমিও না ---
        if ("ট্রেড" in c or "trade" in c) and any(k in c for k in ["কমিও না", "কমাস না", "কমাবি না", "কমাবে না", "কম করো না", "কম করিস না", "don't reduce", "not reduce", "same number"]):
            R["keep_trades"] = 0.9
            R["understood"].append("ট্রেড সংখ্যা প্রায় একই রাখা (অন্তত ৯০%)")
            continue
        # --- লক্ষ্য ---
        if any(k in c for k in DDW) and any(k in c for k in LESS + ["মধ্যে", "within", "under", "limit", "সীমা", "max", "বেশি না", "নিচে"]):
            R["goal"] = "dd"
            capw = any(k in c for k in ["মধ্যে", "within", "under", "limit", "সীমা", "max", "বেশি না", "নিচে", "এর বেশি", "cap"])
            if nums:
                v, u = nums[0]
                if u in ("টাকা", "rs", "₹") or v > 100:
                    R["dd_cap_abs"] = v
                    R["understood"].append("ড্রডাউন ₹%d-এর মধ্যে রাখার চেষ্টা" % v)
                elif capw:
                    R["dd_cap_pct"] = v
                    R["understood"].append("ড্রডাউন মূলধনের %g%%-এর মধ্যে রাখার চেষ্টা" % v)
                else:
                    R["cut"] = min(v, 90.0)
                    R["understood"].append("ড্রডাউন প্রায় %g%% কমানো" % R["cut"])
            else:
                R["cut"] = 15.0 if any(k in c for k in ["একটু", "slightly", "little", "bit"]) else 40.0 if any(k in c for k in ["অনেক", "much", "significant", "বেশ"]) else 25.0
                R["understood"].append("ড্রডাউন কমানো (প্রায় %g%%, আপনি সংখ্যা দেননি)" % R["cut"])
            hit = True
        elif any(k in c for k in ["win rate", "winrate", "উইন রেট", "জেতার হার", "win%", "win %", "উইনরেট", "জেতা ট্রেড"]):
            R["goal"] = "win"; R["understood"].append("Win% বাড়ানো (প্রফিট-ফ্যাক্টর ১-এর ওপরে রেখে)"); hit = True
        elif any(k in c for k in ["profit factor", "প্রফিট ফ্যাক্টর", " pf", "pf "]):
            R["goal"] = "pf"; R["understood"].append("Profit Factor বাড়ানো"); hit = True
        elif any(k in c for k in ["লাভ বাড়", "profit বাড়", "বেশি লাভ", "more profit", "increase profit", "maximize", "মুনাফা", "net বাড়", "রিটার্ন বাড়", "return", "বেশি আয়"]):
            R["goal"] = "profit"; R["understood"].append("নেট লাভ বাড়ানো (ড্রডাউন বর্তমানের ~১.৩ গুণের মধ্যে রেখে)"); hit = True
        # --- SL সীমা ---
        if any(k in c for k in P["sl"]) and nums and not any(k in c for k in P["tp"]):
            v = nums[0][0]
            if any(k in c for k in ["বেশি না", "বেশি হবে না", "এর নিচে", "max", "maximum", "সর্বোচ্চ", "মধ্যে", "within", "under", "above না"]):
                R["sl_max"] = v; R["understood"].append("SL ≤ %g%%" % v); hit = True
            elif any(k in c for k in ["কম না", "কম হবে না", "min", "minimum", "অন্তত", "কমপক্ষে", "at least"]):
                R["sl_min"] = v; R["understood"].append("SL ≥ %g%%" % v); hit = True
        # --- স্ট্রাইক নিয়ম ---
        if any(k in c for k in P["strike"]) or "itm" in c or "otm" in c or "atm" in c:
            if "itm" in c and any(k in c for k in OFF):
                R["strike_rule"] = "no_itm"; R["understood"].append("ITM স্ট্রাইক বাদ"); hit = True
            elif "otm" in c and any(k in c for k in OFF):
                R["strike_rule"] = "no_otm"; R["understood"].append("OTM স্ট্রাইক বাদ"); hit = True
            elif ("atm" in c and any(k in c for k in ["only", "শুধু", "ই রাখ", "fixed", "ফিক্সড"])):
                R["strike_rule"] = "atm"; R["understood"].append("স্ট্রাইক শুধু ATM"); hit = True
        # --- বদলিও না / চাই না (প্যারামিটার ধরে) ---
        _hw = lambda w, t: (re.search(r"(?<![a-z0-9])" + re.escape(w.strip()) + r"(?![a-z0-9])", t) is not None) if (w.strip().isascii() and len(w.strip()) <= 3) else (w in t)
        for k, words in P.items():
            if any(_hw(w, c) for w in words):
                if any(n in c for n in NEG) and k not in R["freeze"]:
                    R["freeze"].add(k); R["understood"].append("%s যেমন আছে তেমনই থাকবে" % NAME[k]); hit = True
                elif any(o in c for o in OFF) and k in ("trail", "tp", "be", "tg", "ts"):
                    R["disable"].add(k); R["freeze"].add(k); R["understood"].append("%s ব্যবহার হবে না" % NAME[k]); hit = True
        if not hit:
            R["ignored"].append(c)
    return R


def _goal_score(R, a, cap, min_n, base_dd, base_win):
    """লক্ষ্য অনুযায়ী স্কোর। ড্রডাউন লক্ষ্য/সীমা থাকলে সীমা ছাড়ালে শাস্তি।"""
    if a["n"] < min_n:
        return None
    dd = abs(a["dd"])
    net = a["net"]
    g = R["goal"]
    if g == "dd":
        sc = net / (dd * 3.0 + 0.02 * cap)
    elif g == "profit":
        sc = net / (dd * 0.5 + 0.02 * cap)
    elif g == "win":
        sc = (net / (dd + 0.02 * cap)) * (a["win"] / 50.0) if a["pf"] >= 1 else (net / (dd + 0.02 * cap)) - 1
    elif g == "pf":
        sc = (a["pf"] - 1.0) * math.sqrt(a["n"]) / 10.0
    else:
        sc = net / (dd + 0.02 * cap)
    lim = None
    if R.get("dd_cap_abs"):
        lim = R["dd_cap_abs"]
    elif R.get("dd_cap_pct"):
        lim = cap * R["dd_cap_pct"] / 100.0
    elif g == "dd" and R.get("cut") and base_dd:
        lim = base_dd * (1 - R["cut"] / 100.0)
    elif g == "profit" and base_dd:
        lim = base_dd * 1.3
    if lim and dd > lim:
        f = (lim / dd) ** 2
        sc = sc * f if sc > 0 else sc / f
    return float(sc)


def _tr(a):
    return {k: float(a[k]) for k in ("n", "net", "pf", "dd", "win", "exp", "rec")}


def _score(a, cap, min_n):
    """লাভ ÷ (সর্বোচ্চ ড্রডাউন + ২% মূলধন)। ট্রেড খুব কম হলে বাতিল।"""
    if a["n"] < min_n:
        return None
    return float(a["net"] / (abs(a["dd"]) + 0.02 * cap))


def _smooth(sc):
    out = []
    for i, x in enumerate(sc):
        if x is None:
            out.append(None)
            continue
        l = sc[i - 1] if i > 0 and sc[i - 1] is not None else x
        r = sc[i + 1] if i + 1 < len(sc) and sc[i + 1] is not None else x
        out.append((l + 2 * x + r) / 4.0)
    return out


def _boot(nets, cap, seed=7):
    n = len(nets)
    if n < 8:
        return None
    rng = np.random.default_rng(seed)
    N = int(max(300, min(3000, 4_000_000 // n)))
    idx = rng.integers(0, n, (N, n))
    M = nets[idx]
    exp = M.mean(axis=1)
    tot = M.sum(axis=1)
    eq = np.cumsum(M, axis=1)
    pk = np.maximum.accumulate(np.maximum(eq, 0.0), axis=1)
    dd = (eq - pk).min(axis=1)
    sd = float(nets.std(ddof=1)) if n > 1 else 0.0
    return {"n": n, "N": N, "exp": float(nets.mean()), "exp_lo": float(np.percentile(exp, 2.5)),
            "exp_hi": float(np.percentile(exp, 97.5)), "p_pos": float((exp > 0).mean() * 100),
            "tot_lo": float(np.percentile(tot, 2.5)), "tot_hi": float(np.percentile(tot, 97.5)),
            "dd_p50": float(np.percentile(dd, 50)), "dd_p95": float(np.percentile(dd, 5)),
            "dd_worst": float(dd.min()), "t": float(nets.mean() / (sd / math.sqrt(n))) if sd > 0 else 0.0,
            "cap20": float(abs(np.percentile(dd, 5)) / 0.20)}


def _bucket_diag(trades):
    """সময়/দিন/DTE ভেদে ট্রেডের গড়-নেটের t-স্ট্যাট। শুধু জানানো হয়, নিজে থেকে প্রয়োগ হয় না।"""
    out = []
    def mins(t):
        return int(t["in"][:2]) * 60 + int(t["in"][3:5])
    groups = {
        "এন্ট্রির সময় (৩০ মিনিট)": lambda t: "%s-%s" % (hm_str(mins(t) // 30 * 30), hm_str(mins(t) // 30 * 30 + 30)),
        "সপ্তাহের দিন": lambda t: t["dow"],
        "DTE (এক্সপায়ারি পর্যন্ত দিন)": lambda t: ("০-১" if t["dte"] <= 1 else "২-৩" if t["dte"] <= 3 else "৪-৭" if t["dte"] <= 7 else "৮+"),
        "CE/PE": lambda t: t["side"].split()[1],
    }
    for gname, fn in groups.items():
        g = {}
        for t in trades:
            g.setdefault(fn(t), []).append(t["net"])
        for k, v in sorted(g.items()):
            v = np.array(v, float)
            if len(v) < 20:
                continue
            sd = float(v.std(ddof=1))
            tt = float(v.mean() / (sd / math.sqrt(len(v)))) if sd > 0 else 0.0
            out.append({"g": gname, "k": k, "n": int(len(v)), "net": float(v.sum()), "avg": float(v.mean()), "t": tt,
                        "win": float(100 * (v > 0).mean())})
    return out


def advise(D, raw, opts, progress=None):
    t0 = time.time()
    base = norm_settings(raw)
    G = base["general"]
    quick = (opts or {}).get("mode") == "quick"
    on_cases = [i for i, c in enumerate(base["cases"]) if c["on"]]
    if not on_cases:
        raise ValueError("কোনো Case চালু নেই")
    ci = int((opts or {}).get("case", on_cases[0]))
    if ci not in on_cases:
        ci = on_cases[0]
    cp = "cases.%d." % ci
    cap = G["capital"]
    if G["split"] <= 0:
        G["split"] = 70
    max_runs = 70 if quick else 170
    est = 38 if quick else 110
    runs = [0]
    R = parse_request((opts or {}).get("req", ""))
    _chk = (opts or {}).get("last") or (opts or {}).get("req", "")
    if _chk.strip() and not parse_request(_chk)["understood"]:
        raise ValueError("অনুরোধটা বুঝতে পারিনি: \"%s\"। যেমন লিখতে পারেন: \"ড্রডাউন ৩০%% কমাও\", \"লাভ বাড়াও\", \"win rate বাড়াও, SL ২৫%% এর বেশি না\", \"ট্রেইল চাই না\", \"স্ট্রাইক বদলিও না\", \"ITM চাই না\", \"ড্রডাউন ১০%% এর মধ্যে রাখো\"।" % _chk.strip()[:80])
    if ADV_CACHE.get("D") is not D:
        ADV_CACHE.clear()
        ADV_CACHE["D"] = D
    cache = ADV_CACHE.setdefault("c", {})
    if len(cache) > 3000:
        cache.clear()

    def ev(s):
        k = _ck(s)
        if k not in cache:
            cache[k] = backtest(D, s, light=True)
            runs[0] += 1
            if progress:
                progress(min(0.96, runs[0] / est))
        return cache[k]["stats"]

    b_st = ev(base)
    if b_st["train"]["n"] < 10:
        raise ValueError("TRAIN অংশে ট্রেড মাত্র %d টি — অ্যাডভাইজারের জন্য অন্তত ১০০+ ট্রেড দরকার। তারিখ-সীমা বাড়ান বা এন্ট্রি শিথিল করুন।" % b_st["train"]["n"])
    min_n = max(15, int((R["keep_trades"] or 0.5) * b_st["train"]["n"]))
    meta0 = cache[_ck(base)]["meta"]
    bdd, bwin = abs(b_st["train"]["dd"]), b_st["train"]["win"]
    scoref = lambda a, mn: _goal_score(R, a, cap, mn, bdd, bwin)
    cur = copy.deepcopy(base)
    changes, stages = [], []
    fz = set(R["freeze"])
    ALIAS = {"t1": "tg", "t2": "tg", "a1": "tg", "wf": "window", "wt": "window"}

    # অনুরোধে "চাই না" বলা প্যারামিটার আগে বন্ধ করা (পরিবর্তন হিসেবে দেখানো হবে)
    def force(key, label, muts, frm, to):
        nonlocal cur
        before = ev(cur)
        nxt = copy.deepcopy(cur)
        for pth, v in muts:
            set_path(nxt, pth, v)
        if _ck(nxt) == _ck(cur):
            return
        after = ev(nxt)
        changes.append({"key": key, "label": label, "from": frm, "to": to, "muts": muts,
                        "d_train": float(after["train"]["net"] - before["train"]["net"]),
                        "d_test": float(after["test"]["net"] - before["test"]["net"]), "plateau": "আপনার অনুরোধে",
                        "dd_from": float(before["train"]["dd"]), "dd_to": float(after["train"]["dd"])})
        cur = nxt
    cpf = "cases.%d." % ci
    c0 = base["cases"][ci]
    if "trail" in R["disable"] and c0["trail"]["mode"] != "none":
        force("trail", "ট্রেইলিং SL", [(cpf + "trail.mode", "none")], c0["trail"]["mode"], "বন্ধ")
    if "tp" in R["disable"] and c0["tp"]["mode"] != "none":
        force("tp", "ফাইনাল টার্গেট", [(cpf + "tp.mode", "none")], "%s %s" % (c0["tp"]["mode"], c0["tp"]["val"]), "নেই")
    if "be" in R["disable"] and c0["be"]["on"]:
        force("be", "ব্রেকইভেন", [(cpf + "be.on", False)], "চালু", "বন্ধ")
    if "tg" in R["disable"] and c0["tg"]["on"]:
        force("tg", "T1/T2 বুকিং", [(cpf + "tg.on", False)], "চালু", "বন্ধ")
    if "ts" in R["disable"] and c0.get("time_stop"):
        force("ts", "টাইম-স্টপ", [(cpf + "time_stop", 0)], str(c0.get("time_stop")), "বন্ধ")
    if R["strike_rule"] == "atm" and c0["strike"]["mode"] == "offset":
        force("strike", "স্ট্রাইক", [(cpf + "strike.off", 0)], "%+d" % int(float(c0["strike"]["off"])), "ATM")
        fz.add("strike")

    def get(path, s=None):
        o = s if s is not None else cur
        for k in path.split("."):
            o = o[int(k)] if isinstance(o, list) else o[k]
        return o

    def run_stage(key, label, cands, note=None, state=None, lots_override=None):
        """cands: [(disp, [(path, val)...], ordered)]। state: যে সেটিংসের ওপর চালাবে (default cur)"""
        nonlocal cur
        basis = state if state is not None else cur
        rows, ckey = [], _ck(basis)
        if runs[0] >= max_runs:
            return None
        if ALIAS.get(key, key) in fz:
            return basis if state is not None else None
        seen = set()
        for disp, muts, ordd in cands:
            s = copy.deepcopy(basis)
            for p, v in muts:
                set_path(s, p, v)
            kk = _ck(s)
            if kk in seen:
                continue
            seen.add(kk)
            st = ev(s)
            rows.append({"v": disp, "ord": ordd, "cur": kk == ckey, "muts": muts, "st": st})
        if not any(r["cur"] for r in rows):
            rows.insert(0, {"v": "বর্তমান", "ord": False, "cur": True, "muts": [], "st": ev(basis)})
        for r in rows:
            r["sc"] = scoref(r["st"]["train"], min_n)
            r["sc_te"] = scoref(r["st"]["test"], 3) if r["st"]["test"]["n"] else None
        ords = [r for r in rows if r["ord"]]
        sm = _smooth([r["sc"] for r in ords])
        for r, v in zip(ords, sm):
            r["sm"] = v
        for r in rows:
            r.setdefault("sm", r["sc"])
        cr = next(r for r in rows if r["cur"])
        valid = [r for r in rows if r["sm"] is not None]
        best = max(valid, key=lambda r: r["sm"]) if valid else cr
        cs_ = cr["sm"] if cr["sm"] is not None else -1e9
        picked = False
        if best is not cr and best["sm"] is not None and best["sm"] - cs_ >= max(0.03, 0.05 * abs(cs_)) and best["muts"]:
            picked = True
        plateau = "–"
        if picked and best["ord"]:
            i = ords.index(best)
            nb = [ords[j]["sc"] for j in (i - 1, i + 1) if 0 <= j < len(ords)]
            if len(nb) < 2:
                plateau = "প্রান্তে (রেঞ্জের বাইরেও ভালো হতে পারে)"
            elif best["sc"] and best["sc"] > 0 and all(x is not None and x >= 0.6 * best["sc"] for x in nb):
                plateau = "স্থিতিশীল ✓"
            else:
                plateau = "স্পাইক ⚠ (আশপাশের মান খারাপ — ওভারফিট ঝুঁকি)"
        out = {"key": key, "label": label, "note": note, "picked": picked, "plateau": plateau,
               "rows": [{"v": r["v"], "cur": r["cur"], "pick": bool(picked and r is best),
                         "sc": r["sc"], "tr": _tr(r["st"]["train"]), "te": _tr(r["st"]["test"])} for r in rows]}
        stages.append(out)
        if picked:
            gain = best["st"]["train"]["net"] - cr["st"]["train"]["net"]
            gain_te = best["st"]["test"]["net"] - cr["st"]["test"]["net"]
            changes.append({"key": key, "label": label, "from": cr["v"], "to": best["v"], "muts": best["muts"],
                            "d_train": float(gain), "d_test": float(gain_te), "plateau": plateau,
                            "dd_from": float(cr["st"]["train"]["dd"]), "dd_to": float(best["st"]["train"]["dd"])})
            nxt = copy.deepcopy(basis)
            for p, v in best["muts"]:
                set_path(nxt, p, v)
            if state is None:
                cur = nxt
            return nxt
        return basis if state is not None else None

    mode_s = cur["cases"][ci]["strike"]["mode"]
    # ---- স্ট্রাইক ----
    if mode_s == "offset":
        rng_ = (-2, -1, 0, 1, 2) if quick else (-3, -2, -1, 0, 1, 2, 3)
        if R["strike_rule"] == "no_itm":
            rng_ = tuple(v for v in rng_ if v >= 0)
        elif R["strike_rule"] == "no_otm":
            rng_ = tuple(v for v in rng_ if v <= 0)
        run_stage("strike", "স্ট্রাইক (ATM থেকে ধাপ; − = ITM, + = OTM)",
                  [("%+d" % v if v else "ATM", [(cp + "strike.off", v)], True) for v in rng_])
    else:
        run_stage("strike", "স্ট্রাইক (লক্ষ্য প্রিমিয়াম ₹)",
                  [("₹%d" % v, [(cp + "strike.prem", v)], True) for v in (100, 150, 200, 300, 400, 500)])

    def sl_stage():
        vals = (10, 15, 20, 25, 30, 40) if quick else (8, 10, 12, 15, 18, 20, 25, 30, 35, 40, 50)
        if R["sl_max"]:
            vals = tuple(v for v in vals if v <= R["sl_max"])
        if R["sl_min"]:
            vals = tuple(v for v in vals if v >= R["sl_min"])
        c = [("%d%%" % v, [(cp + "sl.mode", "pct"), (cp + "sl.val", float(v))], True) for v in vals]
        if not (R["sl_max"] or R["sl_min"]):
            c.append(("SL নেই", [(cp + "sl.mode", "none")], False))
        run_stage("sl", "স্টপ লস (এন্ট্রি প্রিমিয়ামের %)", c)

    def tp_stage():
        vals = (30, 50, 80, 120) if quick else (20, 30, 40, 50, 60, 80, 100, 150)
        c = [("%d%%" % v, [(cp + "tp.mode", "pct"), (cp + "tp.val", float(v))], True) for v in vals]
        c.append(("টার্গেট নেই", [(cp + "tp.mode", "none")], False))
        if cur["cases"][ci]["sl"]["mode"] != "none" and not quick:
            c += [("RR %.1f×" % v, [(cp + "tp.mode", "rr"), (cp + "tp.val", v)], False) for v in (1.5, 2.0, 3.0)]
        run_stage("tp", "ফাইনাল টার্গেট", c)

    def trail_stage():
        c = [("ট্রেইল নেই", [(cp + "trail.mode", "none")], False)]
        vs, acts = ((10, 15, 20), (0, 10)) if quick else ((8, 10, 15, 20, 25), (0, 10, 20))
        for v in vs:
            for a in acts:
                c.append(("পিক থেকে %d%% পিছনে (চালু +%d%% থেকে)" % (v, a),
                          [(cp + "trail.mode", "pct"), (cp + "trail.val", float(v)), (cp + "trail.act", float(a))], False))
        for tg_, mv in ((10, 5), (15, 10), (20, 10), (30, 15)):
            c.append(("স্টেপ: +%d%% সরলে SL +%d%%" % (tg_, mv),
                      [(cp + "trail.mode", "step"), (cp + "trail.unit", "pct"), (cp + "trail.trig", float(tg_)),
                       (cp + "trail.move", float(mv))], False))
        run_stage("trail", "ট্রেইলিং SL", c)

    def be_stage():
        c = [("ব্রেকইভেন বন্ধ", [(cp + "be.on", False)], False)]
        for v in (10, 15, 20, 30):
            c.append(("+%d%% হলে SL = এন্ট্রি" % v, [(cp + "be.on", True), (cp + "be.val", float(v))], False))
        run_stage("be", "ব্রেকইভেন", c)

    sl_stage()
    tp_stage()
    trail_stage()
    be_stage()

    # ---- আংশিক বুকিং T1/T2 ----
    tg_block = None
    if not quick and "tg" not in fz:
        L0 = cur["general"]["lots"]
        lots_eff = L0 if L0 >= 2 else 4
        st_t = copy.deepcopy(cur)
        st_t["general"]["lots"] = lots_eff
        state0 = copy.deepcopy(st_t)
        pre_n = len(changes)
        pre_st = len(stages)
        stT = copy.deepcopy(state0)
        cands = [("T1 বন্ধ", [(cp + "tg.on", False)], False)]
        for v in (15, 20, 25, 30, 40, 50):
            for q in (33, 50):
                cands.append(("T1 +%d%%-এ %d%% বুক" % (v, q),
                              [(cp + "tg.on", True), (cp + "tg.unit", "pct"), (cp + "tg.t1.val", float(v)),
                               (cp + "tg.t1.qty", float(q)), (cp + "tg.t2.on", False)], False))
        s1 = run_stage("t1", "Target 1 (আংশিক বুকিং)", cands, "লট ≥ ২ লাগে; এখানে %d লটে মাপা" % lots_eff, state=stT)
        s2 = s1
        if s1 is not None and s1["cases"][ci]["tg"]["on"]:
            t1v = s1["cases"][ci]["tg"]["t1"]["val"]
            c2 = [("T2 বন্ধ", [(cp + "tg.t2.on", False)], False)]
            for m_ in (1.5, 2.0, 3.0):
                for q in (25, 50):
                    c2.append(("T2 +%d%%-এ %d%% বুক" % (round(t1v * m_), q),
                               [(cp + "tg.t2.on", True), (cp + "tg.t2.val", float(round(t1v * m_))),
                                (cp + "tg.t2.qty", float(q)), (cp + "tg.a2.mode", "t1")], False))
            s2 = run_stage("t2", "Target 2 (আংশিক বুকিং)", c2, state=s1) or s1
            c3 = [("T1-এর পর SL সরবে না", [(cp + "tg.a1.mode", "none")], False),
                  ("T1-এর পর SL = এন্ট্রি", [(cp + "tg.a1.mode", "be")], False),
                  ("T1-এর পর SL = এন্ট্রি +৫% লক", [(cp + "tg.a1.mode", "lock"), (cp + "tg.a1.val", 5.0)], False)]
            s3 = run_stage("a1", "T1-এর পর SL কোথায়", c3, state=s2) or s2
            tg_final = s3
        else:
            tg_final = s1
        t_changes = changes[pre_n:]
        if L0 >= 2:
            cur = tg_final if tg_final is not None else cur
        else:
            cur_tg = cur   # লট ১ হলে মূল সাজেশনে T-সেটিং ঢোকে না
        tg_block = {"lots_used": lots_eff, "applied_in_main": L0 >= 2,
                    "changes": [c["label"] + ": " + str(c["to"]) for c in t_changes],
                    "d_train": float(sum(c["d_train"] for c in t_changes)),
                    "d_test": float(sum(c["d_test"] for c in t_changes)),
                    "settings": {k: tg_final["cases"][ci]["tg"][k] for k in ("on", "unit", "t1", "t2", "a1", "a2")} if tg_final else None,
                    "base_test": _tr(ev(state0)["test"]), "base_train": _tr(ev(state0)["train"]),
                    "new_test": _tr(ev(tg_final)["test"]) if tg_final else None,
                    "new_train": _tr(ev(tg_final)["train"]) if tg_final else None}
        if L0 < 2:
            # মূল সাজেশনের changes থেকে T-ধাপ সরানো (লট ১-এ প্রযোজ্য নয়), আলাদা ব্লকে দেখানো হবে
            del changes[pre_n:]
        for st_ in stages[pre_st:]:
            st_["sub"] = True

    # ---- সময়, ট্রেড-সংখ্যা ----
    if not quick:
        w1 = ("09:20", "09:30", "09:45", "10:00", "10:30")
        w2 = ("11:00", "12:00", "13:00", "14:00", "14:30")
        run_stage("ts", "টাইম-স্টপ (মিনিট, ০ = বন্ধ)",
                  [("বন্ধ" if v == 0 else "%d মিনিট" % v, [(cp + "time_stop", v)], False) for v in (0, 15, 30, 45, 60, 90)])
        run_stage("wf", "এন্ট্রি শুরু", [(v, [("general.win_from", v)], True) for v in w1])
        run_stage("wt", "এন্ট্রি শেষ", [(v, [("general.win_to", v)], True) for v in w2])
        run_stage("mt", "দিনে সর্বোচ্চ ট্রেড", [(str(v), [("general.max_trades", v)], True) for v in (1, 2, 3, 4, 5)])
        # দ্বিতীয় পাস: বদলানো ভিত্তিতে SL/TP/ট্রেইল আবার (পরস্পরের প্রভাব)
        if changes:
            p2 = len(stages)
            sl_stage(); tp_stage(); trail_stage()
            for st_ in stages[p2:]:
                st_["label"] += " — ২য় পাস (বদলানো ভিত্তিতে)"

    # ---- একই প্যারামিটার দুই পাসে বদলালে একত্র করা ----
    merged = {}
    for c in changes:
        m = merged.get(c["key"])
        if m is None:
            merged[c["key"]] = dict(c)
        else:
            m["to"], m["muts"], m["plateau"] = c["to"], c["muts"], c["plateau"]
            m["d_train"] += c["d_train"]
            m["d_test"] += c["d_test"]
            m["dd_to"] = c["dd_to"]
    changes[:] = [c for c in merged.values() if not (c["from"] == c["to"])]

    # ---- যাচাই ----
    fin = ev(cur)
    f_tr, f_te, f_all = fin["train"], fin["test"], fin["all"]
    b_tr, b_te, b_all = b_st["train"], b_st["test"], b_st["all"]
    msgs, level = [], "pass"
    sb_tr, sf_tr = scoref(b_tr, 1), scoref(f_tr, 1)
    sb_te, sf_te = scoref(b_te, 1), scoref(f_te, 1)
    if not changes:
        level = "keep"
        msgs.append("বর্তমান সেটিং ইতিমধ্যেই এই ডেটায় প্রায় সেরা — বদলানোর মতো স্পষ্ট উন্নতি পাওয়া যায়নি।")
    else:
        if f_te["n"] < 10:
            level = "warn"
            msgs.append("TEST অংশে ট্রেড মাত্র %d টি — যাচাই দুর্বল।" % f_te["n"])
        elif f_te["net"] <= 0:
            level = "fail"
            msgs.append("TEST অংশে সাজেশনের নেট ঋণাত্মক (%s ₹) — সম্ভবত ওভারফিট।" % round(f_te["net"]))
        elif sb_te is not None and sf_te is not None and sb_te > 0 and sf_te < 0.8 * sb_te:
            level = "fail"
            msgs.append("TEST-এ সাজেশন বর্তমানের চেয়ে খারাপ (স্কোর %.2f বনাম %.2f) — ওভারফিট সম্ভাবনা।" % (sf_te, sb_te))
        elif f_te["net"] < b_te["net"] and R["goal"] in ("balanced", "profit"):
            level = "warn"
            msgs.append("TEST-এ নেট কিছুটা কমেছে (%s → %s ₹) কিন্তু ড্রডাউন/স্কোর মিলিয়ে দেখুন।" % (round(b_te["net"]), round(f_te["net"])))
        else:
            msgs.append("TEST অংশে (যেখানে বাছাই করা হয়নি) সাজেশন বর্তমানের চেয়ে ভালো বা সমান — ইতিবাচক লক্ষণ।")
        bad = [c["label"] for c in changes if c["d_test"] < 0]
        for c in changes:
            c["test_ok"] = bool(c["d_test"] >= 0)
        if bad:
            level = "warn" if level == "pass" else level
            msgs.append("এই পরিবর্তনগুলো TEST অংশে উল্টো ফল দিয়েছে (শুধু TRAIN-এ ভালো): %s — এগুলো প্রয়োগে সতর্ক থাকুন, বা বাদ দিন।" % "; ".join(bad))
        if any("স্পাইক" in c["plateau"] for c in changes):
            level = "warn" if level == "pass" else level
            msgs.append("কিছু সাজেশন 'স্পাইক' — আশপাশের মান খারাপ, তাই ওই মান ভাগ্যের ফল হতে পারে।")

    # ---- অনুরোধ অনুযায়ী ফলাফলের রিপোর্ট (সৎভাবে: লক্ষ্য পূরণ হলো কি না) ----
    def pc(a, b):
        return (100.0 * (a - b) / abs(b)) if b else 0.0
    goal_rep = None
    if R["goal"] != "balanced" or R["understood"]:
        d_tr = pc(abs(f_tr["dd"]), abs(b_tr["dd"]))
        d_te = pc(abs(f_te["dd"]), abs(b_te["dd"])) if b_te["n"] and f_te["n"] else None
        goal_rep = {"goal": R["goal"], "dd_change_train": d_tr, "dd_change_test": d_te,
                    "net_change_train": pc(f_tr["net"], b_tr["net"]),
                    "net_change_test": pc(f_te["net"], b_te["net"]) if b_te["n"] else None,
                    "win_from": b_all["win"], "win_to": f_all["win"], "pf_from": b_all["pf"], "pf_to": f_all["pf"],
                    "trades_from": b_all["n"], "trades_to": f_all["n"]}
        if R["goal"] == "dd":
            tgt = None
            if R.get("dd_cap_abs"):
                tgt = R["dd_cap_abs"]
            elif R.get("dd_cap_pct"):
                tgt = cap * R["dd_cap_pct"] / 100.0
            elif R.get("cut"):
                tgt = abs(b_tr["dd"]) * (1 - R["cut"] / 100.0)
            if tgt is not None:
                ok_ = abs(f_tr["dd"]) <= tgt * 1.001
                goal_rep["target"] = float(tgt)
                goal_rep["target_met"] = bool(ok_)
                msgs.append(("✓ ড্রডাউন লক্ষ্য (%s) পূরণ হয়েছে (TRAIN: %s)।" if ok_ else
                             "✗ ড্রডাউন লক্ষ্য (%s) পূরণ করা গেল না — এই স্ট্র্যাটেজি ও পরীক্ষিত মানের মধ্যে সবচেয়ে কাছেরটা দেওয়া হলো (TRAIN: %s)।") % (inr_(tgt), inr_(abs(f_tr["dd"]))))
            if d_te is not None and d_te > 0:
                level = "warn" if level in ("pass", "keep") else level
                msgs.append("TEST অংশে ড্রডাউন কমেনি, বরং %.0f%% বেড়েছে — কমানোটা শুধু TRAIN-এ কাজ করেছে।" % d_te)
    res_fin = backtest(D, cur, light=False)
    res_base = backtest(D, base, light=False)
    tf_, tb_ = res_fin["trades"], res_base["trades"]
    mc_f = _boot(np.array([t["net"] for t in tf_], float), cap)
    mc_b = _boot(np.array([t["net"] for t in tb_], float), cap)
    an = res_fin.get("an", {})
    ex_ = an.get("exc", {})
    reach = {}
    if tf_:
        mf = np.array([t["mfe"] for t in tf_], float)
        reach = {str(x): float(100 * np.mean(mf >= x)) for x in (10, 15, 20, 30, 40, 50, 75, 100)}
    warns = []
    if f_all["n"] < 100:
        warns.append("মোট ট্রেড %d — ১০০-র কম সংখ্যায় যেকোনো সাজেশন ভরসাযোগ্য নয়।" % f_all["n"])
    nwin = int(round(f_all["n"] * f_all["win"] / 100.0))
    if nwin < 40:
        warns.append("জেতা ট্রেড মাত্র ~%d টি — লাভের গড়/ট্রেইল-টার্গেটের হিসাব অস্থির হবে।" % nwin)
    warns.append("সবকিছু ঐতিহাসিক ডেটার ওপর; লট সাইজ বদল, স্লিপেজ, লাইভ ফিল আর বাজার-চরিত্র বদলে ফল আলাদা হতে পারে। চালু করার আগে পেপার ট্রেড করুন।")
    if meta0.get("split_date"):
        warns.append("TRAIN/TEST ভাগ: %s তারিখের আগে TRAIN (বাছাই), পরে TEST (যাচাই)।" % meta0["split_date"])
    secs = time.time() - t0
    return {"case": cur["cases"][ci]["name"], "case_idx": ci, "quick": quick, "runs": runs[0], "secs": secs,
            "split_date": meta0.get("split_date"), "split": G["split"],
            "base": {"train": _tr(b_tr), "test": _tr(b_te), "all": _tr(b_all)},
            "final": {"train": _tr(f_tr), "test": _tr(f_te), "all": _tr(f_all)},
            "changes": changes, "stages": stages, "tg": tg_block,
            "req": {"text": (opts or {}).get("req", ""), "goal": R["goal"], "understood": R["understood"],
                    "ignored": R["ignored"]},
            "goal_rep": goal_rep,
            "validation": {"level": level, "msgs": msgs},
            "mc": {"final": mc_f, "base": mc_b},
            "diag": {"buckets": _bucket_diag(tf_), "reach": reach,
                     "mae_w": ex_.get("mae_w"), "mfe_w": ex_.get("mfe_w"), "give_med": ex_.get("give_med"),
                     "by_reason": an.get("by_reason", [])},
            "warnings": warns,
            "apply": [{"path": p, "value": v} for c in changes for p, v in c["muts"]],
            "score_note": "স্কোর = নেট লাভ ÷ (সর্বোচ্চ ড্রডাউন + ২% মূলধন); বাছাইয়ে পাশের মানের সাথে মসৃণ করা হয় যাতে একক 'ভাগ্যবান' মান না জেতে।"}



