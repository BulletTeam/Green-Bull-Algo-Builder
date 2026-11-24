"""১-মিনিট স্পট ডেটা নামিয়ে SQLite-এ রাখে।

চালানো (Termux-এ, backend ফোল্ডার থেকে):
  python downloader.py            সব ইনডেক্স, শুধু নতুন দিন যোগ হয়
  python downloader.py BN         একটি ইনডেক্স
  python downloader.py --since 2026-08-01     ওই দিন থেকে আবার নামায় (সব ইনডেক্স)
"""
import datetime as dt
import sys

from config import INSTR, START_DAY, SESSION_START, SESSION_END, CLOSE_AFTER
import store
import upstox

EPOCH = dt.datetime(1970, 1, 1)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def last_complete_day():
    now = dt.datetime.now(IST)
    if now.strftime("%H:%M") >= CLOSE_AFTER:
        return now.date()
    return now.date() - dt.timedelta(days=1)


def minute_of(ts):
    """'2024-01-02T09:15:00+05:30' -> IST মিনিট (epoch থেকে)। ওই সময়টা IST-এর ঘড়ির মান।"""
    d = dt.datetime.strptime(ts[:16], "%Y-%m-%dT%H:%M")
    return int((d - EPOCH).total_seconds() // 60)


def to_rows(candles):
    """Upstox সারি -> (t, o, h, l, c, v); শুধু সেশনের সময় রাখে। ক্রম নিয়ে চিন্তা নেই, SQLite নিজে ঠিক করে।"""
    out = []
    for r in candles:
        try:
            t = minute_of(r[0])
        except (ValueError, TypeError, IndexError):
            continue
        mod = t % 1440
        if not (SESSION_START <= mod <= SESSION_END):
            continue
        out.append((t, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
    return out


def update_spot(code, since=None):
    """একটি ইনডেক্সের স্পট ১-মিনিট আপডেট করে। ফেরত দেয় একটি বাংলা লাইন।
    since (date) দিলে ওই দিন থেকে আবার নামায় (পুরনো ডেটা শুধরাতে)।"""
    info = INSTR[code]
    c = store.conn()
    try:
        last = store.last_minute(c, code)
        if since is not None:
            start = since
        elif last is not None:
            start = (EPOCH + dt.timedelta(minutes=last)).date()   # শেষ দিনটা আবার, অসম্পূর্ণ দিন পূর্ণ হতে
        else:
            start = dt.date.fromisoformat(START_DAY)
        end = last_complete_day()
        if start > end:
            msg = "আপ-টু-ডেট (শেষ দিন %s)" % start
            store.log(c, code, "spot", True, msg)
            return msg

        raw = upstox.fetch_spot_1m(info["key"], start, end)
        rows = to_rows(raw)
        if not rows:
            msg = "নতুন কোনো ডেটা আসেনি (ছুটি বা ডেটা নেই)"
            store.log(c, code, "spot", True, msg)
            return msg

        store.upsert(c, code, rows)
        s = store.summary(c, code)
        msg = "%d বার যোগ/আপডেট | মোট %d বার, %d দিন, %s → %s | OHLC ভুল %d, অসম্পূর্ণ দিন %d" % (
            len(rows), s["bars"], s["days"], s["first_day"], s["last_day"], s["bad_ohlc"], s["short_days"])
        store.log(c, code, "spot", True, msg)
        return msg
    except upstox.TokenError as e:
        store.log(c, code, "spot", False, str(e))
        raise
    except Exception as e:
        store.log(c, code, "spot", False, str(e))
        raise
    finally:
        c.close()


def main():
    args = sys.argv[1:]
    since = None
    if "--since" in args:
        i = args.index("--since")
        if i + 1 >= len(args):
            sys.exit("--since এর পরে তারিখ দিন, যেমন --since 2026-08-01")
        since = dt.date.fromisoformat(args[i + 1])
        args = args[:i] + args[i + 2:]
    codes = list(INSTR) if not args else [a.upper() for a in args]
    for code in codes:
        if code not in INSTR:
            sys.exit("অজানা ইনডেক্স: %s (বেছে নিন: %s)" % (code, ", ".join(INSTR)))
    for code in codes:
        print("[%s] %s" % (code, INSTR[code]["name"]))
        try:
            print("  ", update_spot(code, since))
        except Exception as e:
            print("   ত্রুটি:", e)


if __name__ == "__main__":
    main()
