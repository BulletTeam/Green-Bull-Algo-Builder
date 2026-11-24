"""অপশন ডেটার গঠন যাচাই। শুধু দেখে, কিছু সেভ করে না। টোকেন ছাপা হয় না।

চালানো (backend ফোল্ডার থেকে):
  python probe_options.py N50
  python probe_options.py BN
কী দেখায়: এক্সপায়ারি তালিকা, কন্ট্র্যাক্টের ফিল্ডের নাম, স্ট্রাইকের ধাপ,
একটি কন্ট্র্যাক্টের ১-মিনিট ক্যান্ডেল (দিনে কত বার আসে)। এই আউটপুট পাঠাতে হবে।
"""
import datetime as dt
import sys
from collections import Counter
from urllib.parse import quote

from config import INSTR
import upstox

V2 = "https://api.upstox.com/v2/expired-instruments"


def as_list(body):
    d = body.get("data") if isinstance(body, dict) else None
    return d if isinstance(d, list) else []


def get_expiries(key):
    st, body = upstox.get("%s/expiries?instrument_key=%s" % (V2, quote(key, safe="")))
    if st == 401:
        sys.exit("401: টোকেন কাজ করছে না")
    out = []
    for x in as_list(body):
        try:
            out.append(dt.date.fromisoformat(str(x)[:10]))
        except ValueError:
            pass
    return st, sorted(set(out))


def get_contracts(key, expiry):
    url = "%s/option/contract?instrument_key=%s&expiry_date=%s" % (V2, quote(key, safe=""), expiry.isoformat())
    st, body = upstox.get(url)
    return st, as_list(body)


def get_candles(ikey, d0, d1):
    url = "%s/historical-candle/%s/1minute/%s/%s" % (V2, quote(ikey, safe=""), d1.isoformat(), d0.isoformat())
    st, body = upstox.get(url)
    data = body.get("data") if isinstance(body, dict) else None
    rows = data.get("candles", []) if isinstance(data, dict) else []
    return st, rows or []


def strike_of(c):
    for k in ("strike_price", "strike"):
        if k in c:
            try:
                return float(c[k])
            except (TypeError, ValueError):
                pass
    return None


def probe_expiry(key, label, ex):
    print("\n--- %s: %s ---" % (label, ex))
    st, cons = get_contracts(key, ex)
    print("কন্ট্র্যাক্ট তালিকা: status %s, মোট %d" % (st, len(cons)))
    if not cons:
        return
    print("ফিল্ডের নাম:", sorted(cons[0].keys()))
    print("নমুনা কন্ট্র্যাক্ট:", cons[0])
    print("ধরন গণনা:", dict(Counter(c.get("instrument_type") for c in cons)))
    strikes = sorted({strike_of(c) for c in cons if strike_of(c) is not None})
    if strikes:
        diffs = Counter(round(b - a, 2) for a, b in zip(strikes, strikes[1:]))
        print("স্ট্রাইক: %d টি, %s → %s, ধাপ (সংখ্যা): %s" % (
            len(strikes), strikes[0], strikes[-1], dict(diffs.most_common(3))))
    else:
        print("⚠ স্ট্রাইকের ফিল্ড খুঁজে পাইনি")
        return
    ces = sorted((c for c in cons if c.get("instrument_type") == "CE" and strike_of(c) is not None), key=strike_of)
    if not ces:
        return
    mid = ces[len(ces) // 2]
    ikey = mid.get("instrument_key")
    d1 = ex
    d0 = ex - dt.timedelta(days=6)
    st, rows = get_candles(ikey, d0, d1)
    print("ক্যান্ডেল (১-মিনিট) %s → %s, status %s, মোট %d" % (d0, d1, st, len(rows)))
    if rows:
        per_day = Counter(r[0][:10] for r in rows)
        print("দিন অনুযায়ী বার:", dict(sorted(per_day.items())))
        print("প্রথম সারি:", rows[-1])
        print("শেষ সারি:", rows[0])
        print("কলাম সংখ্যা:", len(rows[0]))


def main():
    code = (sys.argv[1] if len(sys.argv) > 1 else "N50").upper()
    if code not in INSTR:
        sys.exit("অজানা ইনডেক্স: %s (বেছে নিন: %s)" % (code, ", ".join(INSTR)))
    key = INSTR[code]["key"]
    print("[%s] %s" % (code, INSTR[code]["name"]))
    st, xs = get_expiries(key)
    print("এক্সপায়ারি: status %s, মোট %d" % (st, len(xs)))
    if not xs:
        return
    print("প্রথম %s, শেষ %s" % (xs[0], xs[-1]))
    print("শেষ ৫টি:", [x.isoformat() for x in xs[-5:]])

    today = dt.date.today()
    done = [x for x in xs if x < today - dt.timedelta(days=3)]
    if done:
        probe_expiry(key, "সাম্প্রতিক শেষ হওয়া", done[-1])
    old = [x for x in xs if x >= dt.date(2024, 10, 1)]
    if old:
        probe_expiry(key, "সবচেয়ে পুরনো (২০২৪-১০ থেকে)", old[0])


if __name__ == "__main__":
    main()
