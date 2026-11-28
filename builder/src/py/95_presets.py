# =====================================================================
#  প্রিসেট
# =====================================================================

def builtin_presets():
    P = {}
    c = default_case(1)
    c["name"] = "ORB Breakout (Buy CE)"
    P["ORB Breakout → Buy CE"] = {"cases": [c]}
    c = default_case(1)
    c["name"] = "ORB Breakdown (Buy PE)"
    c["opt"] = "PE"
    c["entry"] = [slot("close", "cross_below", "orbl"), slot("rsi", "below", "number", 45), slot("close", "below", "vwap")]
    c["exit"] = [slot("close", "cross_above", "orbm", join="OR")]
    P["ORB Breakdown → Buy PE"] = {"cases": [c]}
    c = default_case(1)
    c["name"] = "Prev Day High Break (Buy CE)"
    c["entry"] = [slot("close", "cross_above", "pdh"), slot("adx", "above", "number", 20), slot("close", "above", "ma3")]
    c["exit"] = [slot("close", "cross_below", "st1", join="OR")]
    P["Prev Day High Break → Buy CE"] = {"cases": [c]}
    c = default_case(1)
    c["name"] = "5 EMA Alert Candle (Buy PE)"
    c["opt"] = "PE"
    c["entry"] = [slot("low", "above", "ma1", ls=1, rs=1), slot("close", "below", "low", rs=1)]
    c["exit"] = [slot("close", "cross_above", "ma1", join="OR")]
    c["tp"] = {"mode": "rr", "val": 2}
    P["5 EMA Alert Candle → Buy PE"] = {"cases": [c]}
    c = default_case(1)
    c["name"] = "BB Breakout + RSI + ADX (Buy CE)"
    c["entry"] = [slot("close", "cross_above", "bbup"), slot("rsi", "above", "number", 65),
                  slot("volume", "above", "number", 10000, on=False), slot("adx", "rising", "number", 20)]
    c["exit"] = [slot("close", "cross_below", "st1", join="OR"), slot("close", "cross_below", "bbmid", join="OR")]
    c["sl"] = {"mode": "pct", "val": 20}
    c["tp"] = {"mode": "none", "val": 40}
    P["BB Breakout + RSI + ADX → Buy CE"] = {"cases": [c]}
    return P


PRESET_FILE = "presets.json"


def load_user_presets():
    try:
        with open(PRESET_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_user_presets(p):
    with open(PRESET_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=1)


