# =====================================================================
#  কন্ডিশন
# =====================================================================

def eval_slot(S, sl, n):
    l, r = sl["l"], sl["r"]
    if l not in S:
        raise ValueError("অজানা বাম সোর্স: %s" % l)
    lv = shift_arr(S[l], sl.get("ls", 0))
    if r == "number":
        rv = np.full(n, float(sl.get("num", 0)))
    else:
        if r not in S:
            raise ValueError("অজানা ডান সোর্স: %s" % r)
        rv = shift_arr(S[r], sl.get("rs", 0)) * float(sl.get("mul", 1))
    op = sl["op"]
    with np.errstate(invalid="ignore"):
        if op == "above":
            return lv > rv
        if op == "below":
            return lv < rv
        lp = shift_arr(lv, 1)
        if op == "rising":
            return lv > lp
        if op == "falling":
            return lv < lp
        rp = shift_arr(rv, 1)
        rp = np.where(np.isnan(rp), rv, rp)     # লেভেল সবে তৈরি হলে (যেমন ORB) আগের মান = বর্তমান ধরা হয়
        if op == "cross_above":
            return (lv > rv) & (lp <= rp)
        if op == "cross_below":
            return (lv < rv) & (lp >= rp)
    raise ValueError("অজানা অপারেটর: %s" % op)


def eval_cond(S, slots, n):
    acc = None
    for sl in slots:
        if not sl.get("on", True):
            continue
        v = eval_slot(S, sl, n)
        if acc is None:
            acc = v
        elif sl.get("join", "AND") == "OR":
            acc = acc | v
        else:
            acc = acc & v
    return acc


