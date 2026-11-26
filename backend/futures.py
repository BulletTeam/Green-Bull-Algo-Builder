"""ফিউচারের ১-মিনিট ভলিউম (ও OI) নামিয়ে gb.db-র fvol টেবিলে রাখে।
স্পট ইনডেক্সে ভলিউম থাকে না, তাই ভলিউম-নির্ভর স্ট্র্যাটেজির জন্য নিকটতম মাসিক ফিউচারের ভলিউম ব্যবহার হয়।
প্রতিটি মাসিক এক্সপায়ারির কন্ট্র্যাক্ট ওই এক্সপায়ারির আগের এক্সপায়ারির পরদিন থেকে মেয়াদ পর্যন্ত দিনগুলো ঢাকে।

চালানো (backend ফোল্ডার থেকে):
  python futures.py            চারটাই
  python futures.py N50 BN     নির্দিষ্ট ইনডেক্স
সীমা: Upstox-এর মেয়াদ-শেষ API-তে শুধু শেষ হওয়া কন্ট্র্যাক্ট; চলমান মাসের ভলিউম মেয়াদ শেষের পর আসে।
"""
import datetime as dt
import sys
import time

from config import INSTR, OPT_START, SESSION_START, OPT_SESSION_END
from downloader import last_complete_day, minute_of
import store
import upstox


def monthly_expiries(xs):
    by = {}
    for d in xs:
        by[(d.year, d.month)] = max(by.get((d.year, d.month), d), d)
    return sorted(by.values())


def keep_rows(raw):
    out = []
    for r in raw:
        try:
            t = minute_of(r[0])
        except (ValueError, TypeError, IndexError):
            continue
        if not (SESSION_START <= t % 1440 <= OPT_SESSION_END):
            continue
        v = int(float(r[5])) if r[5] is not None else 0
        oi = int(float(r[6])) if len(r) > 6 and r[6] is not None else 0
        out.append((t, v, oi))
    out.sort()
    return out


def run(code):
    key = INSTR[code]["key"]
    xs = upstox.get_expiries(key)
    if xs is None:
        print("[%s] এক্সপায়ারি তালিকা আনা যায়নি" % code)
        return
    start = dt.date.fromisoformat(OPT_START)
    last_day = last_complete_day()
    monthly = [m for m in monthly_expiries(xs) if m >= start]
    c = store.conn()
    t0 = time.time()
    prev = start - dt.timedelta(days=1)
    total = 0
    try:
        for n, ex in enumerate(monthly, 1):
            d0, d1 = prev + dt.timedelta(days=1), min(ex, last_day)
            prev = ex
            if ex > last_day:
                print("[%s] %d/%d %s: মেয়াদ শেষ হয়নি — বাদ (শেষ হলে আসবে)" % (code, n, len(monthly), ex))
                continue
            if store.fut_is_done(c, code, ex.isoformat()):
                continue
            st, cons = upstox.get_futures(key, ex)
            cons = [x for x in cons if x.get("instrument_key")]
            if st != 200 or not cons:
                print("[%s] %d/%d %s: ফিউচার কন্ট্র্যাক্ট নেই (status %s)" % (code, n, len(monthly), ex, st))
                continue
            ok, raw = upstox.fetch_option_1m(cons[0]["instrument_key"], d0, d1)
            rows = keep_rows(raw)
            store.fut_save(c, code, ex.isoformat(), rows, ok and bool(rows))
            total += len(rows)
            print("[%s] %d/%d %s: %d বার%s | %.0f মিনিট" % (code, n, len(monthly), ex, len(rows),
                                                          "" if ok and rows else " (অসম্পূর্ণ/ফাঁকা, আবার চেষ্টা হবে)",
                                                          (time.time() - t0) / 60))
    finally:
        c.close()
    print("[%s] শেষ: নতুন %d বার" % (code, total))


def main():
    codes = [a.upper() for a in sys.argv[1:]] or list(INSTR)
    for code in codes:
        if code not in INSTR:
            sys.exit("অজানা ইনডেক্স: %s (বেছে নিন: %s)" % (code, ", ".join(INSTR)))
    try:
        for code in codes:
            run(code)
    except upstox.TokenError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        print("\nবন্ধ করা হলো। আবার চালালে বাকিটা চলবে।")


if __name__ == "__main__":
    main()
