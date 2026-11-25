"""অপশন ১-মিনিট ডেটা (ITM5–OTM5) নামিয়ে gb_opt.db-তে রাখে।

কীভাবে বাছে: প্রতিটি শেষ-হওয়া এক্সপায়ারিতে (সাপ্তাহিক/মাসিক) নির্দিষ্ট দিন আগে থেকে,
প্রতিটি দিনের স্পটের (low..high) ATM থেকে ওপরে-নিচে OPT_BAND স্ট্রাইকের মধ্যে যে কন্ট্র্যাক্ট,
শুধু ওই দিনের বারগুলো রাখে। আবার চালালে যেগুলো সম্পূর্ণ নেমেছে সেগুলো বাদ যায়।

চালানো (backend ফোল্ডার থেকে):
  python options.py N50 --from 2026-10-01 --to 2026-10-31    এক মাসের নমুনা (আগে এটা)
  python options.py N50                                       একটি ইনডেক্সের সব
  python options.py                                           চারটাই
  python options.py status                                    কতটা নেমেছে + ফাইলের আকার
  python options.py compact                                   ফাইল ছোট করা (বড় ডাউনলোড শেষে)
"""
import bisect
import datetime as dt
import os
import sys
import time

from config import (INSTR, OPT_BAND, OPT_DB_FILE, OPT_SESSION_END, OPT_START, SESSION_START,
                    MONTHLY_WINDOW, WEEKLY_WINDOW)
from downloader import last_complete_day, minute_of
import store
import upstox

EPOCH = dt.date(1970, 1, 1)


def day_index(d):
    return (d - EPOCH).days


def spot_days(instr):
    """{দিন-সংখ্যা: (low, high)} — স্পট ডেটা gb.db থেকে।"""
    c = store.conn()
    try:
        rows = c.execute("SELECT t/1440, MIN(l), MAX(h) FROM bars WHERE instr=? GROUP BY t/1440", (instr,)).fetchall()
    finally:
        c.close()
    return {r[0]: (r[1], r[2]) for r in rows}


def nearest(strikes, x):
    i = bisect.bisect_left(strikes, x)
    if i == 0:
        return 0
    if i == len(strikes):
        return len(strikes) - 1
    return i if strikes[i] - x < x - strikes[i - 1] else i - 1


def band_for(strikes, low, high):
    i0, i1 = nearest(strikes, low), nearest(strikes, high)
    return strikes[max(0, i0 - OPT_BAND)], strikes[min(len(strikes) - 1, i1 + OPT_BAND)]


def keep_rows(raw, strike, bands):
    out = []
    for r in raw:
        try:
            t = minute_of(r[0])
        except (ValueError, TypeError, IndexError):
            continue
        if not (SESSION_START <= t % 1440 <= OPT_SESSION_END):
            continue
        b = bands.get(t // 1440)
        if b is None or not (b[0] <= strike <= b[1]):
            continue
        oi = int(float(r[6])) if len(r) > 6 and r[6] is not None else 0
        out.append((t, round(float(r[1]) * 100), round(float(r[2]) * 100), round(float(r[3]) * 100),
                    round(float(r[4]) * 100), int(float(r[5])), oi))
    out.sort()      # Upstox নতুন-থেকে-পুরনো দেয়; পুরনো-থেকে-নতুন সাজালে ফাইল প্রায় অর্ধেক ছোট হয়
    return out


def do_expiry(code, ex, spot, oc, last_day):
    """একটি এক্সপায়ারি। ফেরত: (বার্তা, নতুন বার, অসম্পূর্ণ কন্ট্র্যাক্ট)।"""
    key = INSTR[code]["key"]
    st, cons = upstox.get_contracts(key, ex)
    cons = [c for c in cons if c.get("strike_price") is not None and c.get("instrument_key")]
    if st != 200 or not cons:
        return "কন্ট্র্যাক্ট তালিকা পাওয়া যায়নি (status %s)" % st, 0, 1
    weekly = any(c.get("weekly") for c in cons)
    window = WEEKLY_WINDOW if weekly else MONTHLY_WINDOW
    d0 = max(ex - dt.timedelta(days=window), dt.date.fromisoformat(OPT_START))
    d1 = min(ex, last_day)
    strikes = sorted({float(c["strike_price"]) for c in cons})

    bands = {}
    d = d0
    while d <= d1:
        di = day_index(d)
        if di in spot:
            bands[di] = band_for(strikes, spot[di][0], spot[di][1])
        d += dt.timedelta(days=1)
    if not bands:
        return "এই সময়ের স্পট ডেটা নেই", 0, 0
    lo = min(b[0] for b in bands.values())
    hi = max(b[1] for b in bands.values())
    sel = sorted((c for c in cons if lo <= float(c["strike_price"]) <= hi),
                 key=lambda c: (float(c["strike_price"]), c.get("instrument_type") or ""))

    new_rows = 0
    incomplete = 0
    skipped = 0
    for c in sel:
        cid = store.opt_contract(oc, code, c)
        if store.opt_is_done(oc, cid):
            skipped += 1
            continue
        ok, raw = upstox.fetch_option_1m(c["instrument_key"], d0, d1)
        rows = keep_rows(raw, float(c["strike_price"]), bands)
        store.opt_save(oc, cid, rows, ok)
        new_rows += len(rows)
        if not ok:
            incomplete += 1
    kind = "সাপ্তাহিক" if weekly else "মাসিক"
    msg = "%s | %d কন্ট্র্যাক্ট (আগে থেকে হয়েছে %d) | নতুন %d বার | অসম্পূর্ণ %d" % (
        kind, len(sel), skipped, new_rows, incomplete)
    return msg, new_rows, incomplete


def run(code, f_from=None, f_to=None):
    xs = upstox.get_expiries(INSTR[code]["key"])
    if xs is None:
        print("[%s] এক্সপায়ারি তালিকা আনা যায়নি" % code)
        return
    start = dt.date.fromisoformat(OPT_START)
    xs = [x for x in xs if x >= start and (f_from is None or x >= f_from) and (f_to is None or x <= f_to)]
    spot = spot_days(code)
    if not spot:
        print("[%s] স্পট ডেটা নেই। আগে python downloader.py চালান।" % code)
        return
    last_day = last_complete_day()
    oc = store.opt_conn()
    t0 = time.time()
    total = 0
    try:
        print("[%s] %d টি এক্সপায়ারি" % (code, len(xs)))
        for n, ex in enumerate(xs, 1):
            msg, rows, _ = do_expiry(code, ex, spot, oc, last_day)
            total += rows
            print("[%s] %d/%d %s: %s | %.0f মিনিট" % (code, n, len(xs), ex, msg, (time.time() - t0) / 60))
    finally:
        oc.close()
    print("[%s] শেষ: নতুন %d বার, %.1f মিনিট" % (code, total, (time.time() - t0) / 60))


def status():
    if not os.path.exists(OPT_DB_FILE):
        print("gb_opt.db এখনো নেই")
        return
    oc = store.opt_conn()
    try:
        for code in INSTR:
            n_c, n_done = oc.execute(
                "SELECT COUNT(*), COALESCE(SUM(s.done),0) FROM contracts c LEFT JOIN ostate s ON s.cid=c.cid WHERE c.instr=?",
                (code,)).fetchone()
            n_b = oc.execute(
                "SELECT COUNT(*) FROM obars WHERE cid IN (SELECT cid FROM contracts WHERE instr=?)", (code,)).fetchone()[0]
            print("[%s] কন্ট্র্যাক্ট %d (সম্পূর্ণ %d) | বার %d" % (code, n_c, n_done, n_b))
    finally:
        oc.close()
    print("ফাইলের আকার: %.1f MB" % (os.path.getsize(OPT_DB_FILE) / 1e6))


def compact():
    """ফাইল ছোট করে (VACUUM)। বড় ডাউনলোড শেষে একবার চালান। ফাঁকা জায়গা লাগে ফাইলের সমান।"""
    oc = store.opt_conn()
    before = os.path.getsize(OPT_DB_FILE)
    oc.execute("VACUUM")
    oc.close()
    print("ফাইল ছোট: %.1f MB → %.1f MB" % (before / 1e6, os.path.getsize(OPT_DB_FILE) / 1e6))


def main():
    try:
        _main()
    except RuntimeError as e:
        sys.exit(str(e))


def _main():
    args = sys.argv[1:]
    if args and args[0] == "status":
        status()
        return
    if args and args[0] == "compact":
        compact()
        return
    f_from = f_to = None
    for flag in ("--from", "--to"):
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                sys.exit("%s এর পরে তারিখ দিন, যেমন 2026-10-01" % flag)
            v = dt.date.fromisoformat(args[i + 1])
            if flag == "--from":
                f_from = v
            else:
                f_to = v
            args = args[:i] + args[i + 2:]
    codes = [a.upper() for a in args] or list(INSTR)
    for code in codes:
        if code not in INSTR:
            sys.exit("অজানা ইনডেক্স: %s (বেছে নিন: %s)" % (code, ", ".join(INSTR)))
    try:
        for code in codes:
            run(code, f_from, f_to)
    except upstox.TokenError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        print("\nবন্ধ করা হলো। আবার চালালে যেখান থেকে থেমেছিল সেখান থেকে চলবে।")


if __name__ == "__main__":
    main()
