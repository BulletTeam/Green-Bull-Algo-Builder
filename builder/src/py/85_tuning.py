# =====================================================================
#  টিউনিং (প্যারামিটার সুইপ)
# =====================================================================

def set_path(obj, path, val):
    ks = path.split(".")
    o = obj
    for k in ks[:-1]:
        o = o[int(k)] if isinstance(o, list) else o[k]
    last = ks[-1]
    if isinstance(o, list):
        o[int(last)] = val
    else:
        o[last] = val


def run_sweep(D, base, params, progress=None):
    base = norm_settings(base)
    combos = list(itertools.product(*[p["values"] for p in params]))
    if len(combos) > 150:
        raise ValueError("সর্বোচ্চ ১৫০টি কম্বিনেশন চলে (এখন %d)। মান কমান।" % len(combos))
    rows = []
    for i, cmb in enumerate(combos):
        s = copy.deepcopy(base)
        for p, v in zip(params, cmb):
            set_path(s, p["path"], v)
        r = backtest(D, s, light=True)
        rows.append({"vals": list(cmb), "all": r["stats"]["all"], "train": r["stats"]["train"], "test": r["stats"]["test"]})
        if progress:
            progress((i + 1) / len(combos))
    return {"rows": rows, "labels": [p.get("label", p["path"]) for p in params], "paths": [p["path"] for p in params]}



