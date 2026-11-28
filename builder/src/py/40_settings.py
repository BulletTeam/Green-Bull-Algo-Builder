# =====================================================================
#  ডিফল্ট সেটিংস
# =====================================================================

def slot(l="close", op="above", r="number", num=0.0, ls=0, rs=0, mul=1.0, join="AND", on=True):
    return {"on": on, "join": join, "l": l, "ls": ls, "op": op, "r": r, "rs": rs, "mul": mul, "num": num}


def default_case(i=1):
    return {
        "name": "Case %d" % i, "on": True, "action": "BUY", "opt": "CE",
        "strike": {"mode": "offset", "off": 0, "prem": 300, "band": 30},
        "entry": [slot("close", "cross_above", "orbh"), slot("rsi", "above", "number", 55),
                  slot("close", "above", "vwap")],
        "exit": [slot("close", "cross_below", "orbm", join="OR")],
        "sl": {"mode": "pct", "val": 20}, "tp": {"mode": "pct", "val": 40},
        "trail": {"mode": "none", "val": 15, "act": 0, "trig": 10, "move": 5, "unit": "pct"}, "be": {"on": False, "val": 20},
        "time_stop": 0,
        "tg": {"on": False, "unit": "pct", "t1": {"val": 20, "qty": 50}, "t2": {"on": False, "val": 40, "qty": 25},
               "a1": {"mode": "be", "val": 5}, "a2": {"mode": "t1"}},
    }


def default_settings():
    return {
        "general": {
            "tf": (5 if 5 % BASE_TF == 0 else BASE_TF), "from": "", "to": "", "lots": 1, "capital": 100000,
            "win_from": "09:20", "win_to": "14:30", "sqoff": "15:15",
            "max_trades": 3, "day_loss": 0, "day_profit": 0,
            "fresh": True, "min_dte": 0, "skip_expiry_day": False,
            "brokerage": 20, "slip": 0.5, "charges_pct": 0.15,
            "sl_first": True, "split": 0, "chart": "spot",
            "size_mode": "fixed", "risk_pct": 1.0, "max_lots": 20, "delay": 0,
        },
        "ind": {
            "ma1": {"type": "ema", "len": 5, "tf": 0, "src": "close"},
            "ma2": {"type": "ema", "len": 20, "tf": 0, "src": "close"},
            "ma3": {"type": "ema", "len": 50, "tf": 0, "src": "close"},
            "rsi": {"len": 14, "tf": 0, "src": "close"},
            "adx": {"len": 14, "smooth": 14, "tf": 0},
            "atr": {"len": 14, "tf": 0},
            "st1": {"len": 10, "mult": 1.0, "tf": 0},
            "st2": {"len": 10, "mult": 3.0, "tf": 0},
            "macd": {"fast": 12, "slow": 26, "sig": 9, "tf": 0},
            "bb": {"len": 20, "mult": 2.0, "src": "close", "tf": 0},
            "vol": {"len": 20},
            "don": {"n": 20},
            "orb": {"mins": 15},
        },
        "cases": [default_case(1)],
    }


def deep_merge(base, inc):
    out = copy.deepcopy(base)
    if not isinstance(inc, dict):
        return out
    for k, v in inc.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def norm_settings(inp):
    d = default_settings()
    st = {"general": deep_merge(d["general"], (inp or {}).get("general", {})),
          "ind": deep_merge(d["ind"], (inp or {}).get("ind", {}))}
    cases = (inp or {}).get("cases")
    if cases is None:
        cases = d["cases"]
    st["cases"] = []
    for i, c in enumerate(cases):
        dc = default_case(i + 1)
        m = deep_merge({k: v for k, v in dc.items() if k not in ("entry", "exit")}, {k: v for k, v in c.items() if k not in ("entry", "exit")})
        for part in ("entry", "exit"):
            m[part] = [deep_merge(slot(), s) for s in c.get(part, dc[part])]
        st["cases"].append(m)
    G = st["general"]
    for k in ("tf", "lots", "max_trades", "min_dte", "split"):
        G[k] = int(float(G[k]))
    for k in ("capital", "day_loss", "day_profit", "brokerage", "slip", "charges_pct", "risk_pct"):
        G[k] = float(G[k])
    G["max_lots"] = max(1, int(float(G.get("max_lots", 20) or 20)))
    G["delay"] = max(0, min(5, int(float(G.get("delay", 0) or 0))))
    if G.get("size_mode") not in ("fixed", "compound", "risk"):
        G["size_mode"] = "fixed"
    if G["tf"] % BASE_TF != 0 or G["tf"] < BASE_TF:
        raise ValueError("চার্ট TF %d মিনিটের গুণিতক হতে হবে (ডেটার ক্যান্ডেল %d মিনিট)" % (BASE_TF, BASE_TF))
    for c in st["cases"]:
        tg = c["tg"]
        tg["on"] = bool(tg.get("on"))
        if tg.get("unit") not in ("pct", "pts", "rr"):
            tg["unit"] = "pct"
        for k in ("t1", "t2"):
            tg[k]["val"] = float(tg[k].get("val", 0) or 0)
            tg[k]["qty"] = min(max(float(tg[k].get("qty", 0) or 0), 1.0), 99.0)
        tg["t2"]["on"] = bool(tg["t2"].get("on"))
        if tg["on"] and tg["t1"]["val"] <= 0:
            raise ValueError("%s: Target 1-এর মান ০-র বেশি হতে হবে" % c["name"])
        if tg["on"] and tg["t2"]["on"] and tg["t2"]["val"] <= tg["t1"]["val"]:
            raise ValueError("%s: Target 2 অবশ্যই Target 1-এর চেয়ে বড় হতে হবে" % c["name"])
        tg["a1"]["val"] = float(tg["a1"].get("val", 0) or 0)
        if tg["a1"].get("mode") not in ("none", "be", "lock"):
            tg["a1"]["mode"] = "be"
        if tg["a2"].get("mode") not in ("none", "t1", "be"):
            tg["a2"]["mode"] = "t1"
        for part in ("entry", "exit"):
            for s in c[part]:
                if s["l"] not in SRC_IDS or s["l"] == "number":
                    raise ValueError("%s: বাম সোর্স ঠিক নেই (%s)" % (c["name"], s["l"]))
                if s["r"] not in SRC_IDS:
                    raise ValueError("%s: ডান সোর্স ঠিক নেই (%s)" % (c["name"], s["r"]))
                for k in ("ls", "rs"):
                    s[k] = max(int(float(s.get(k, 0))), 0)
                s["mul"] = float(s["mul"])
                s["num"] = float(s["num"])
    return st


