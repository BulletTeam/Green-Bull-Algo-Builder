# =====================================================================
#  পেপার ট্রেডিং (রিপ্লে + লাইভ)
#  নীতি: কোনো আলাদা "লাইভ লজিক" নেই। প্রতি টিকে ব্যাকটেস্ট ইঞ্জিনকেই আজকের দিনের জন্য, "এখন পর্যন্ত জানা ডেটা"
#  দিয়ে চালানো হয় (স্পট শুধু সম্পূর্ণ হওয়া চার্ট বার; চলতি মিনিটের শুধু open)। তাই পেপারের ফল ও ব্যাকটেস্ট
#  একই কোড থেকে আসে, আর অবস্থা (state) ফাইলে না রেখে প্রতিবার নতুন হিসাব — রিস্টার্টে কিছু হারায় না।
# =====================================================================
PAPER = {"sess": None, "stop": False, "thread": None}
PLOCK = threading.Lock()


def paper_dir():
    src = DATA.get("src") or {}
    d = os.path.join(src.get("db_dir") or os.getcwd(), "paper")
    os.makedirs(d, exist_ok=True)
    return d


def paper_view(D, now, tf, warm=25):
    """'now' মিনিট পর্যন্ত যা জানা, শুধু সেটুকুর স্পট ডেটা-অবজেক্ট (অপশন ডেটা শেয়ার্ড)।"""
    day = now // 1440
    i0 = int(np.searchsorted(D.day, day - warm, "left"))
    i1 = int(np.searchsorted(D.t, (day + 1) * 1440, "left"))
    t = D.t[i0:i1]
    m = t % 1440
    bs = OPEN_MIN + ((m - OPEN_MIN) // tf) * tf
    keep = (t // 1440 < day) | (bs + tf <= now % 1440)
    V = Data()
    V._bars, V._cache = {}, {}
    V.t = t[keep]
    V.o, V.h, V.l, V.c, V.v = (a[i0:i1][keep] for a in (D.o, D.h, D.l, D.c, D.v))
    _spot_finish(V, lambda *a: None, view=True)
    V.opt, V.strikes, V.exp_lot, V.exp_range, V.expiries = D.opt, D.strikes, D.exp_lot, D.exp_range, D.expiries
    return V


def paper_eval(D, settings, now):
    """আজকের (now-এর দিনের) ট্রেড-তালিকা, এখন পর্যন্ত জানা তথ্যে।"""
    st = copy.deepcopy(settings)
    G = st["general"]
    day = now // 1440
    G["from"] = G["to"] = day_str(day)
    tf = int(float(G["tf"]))
    with RUN_LOCK:
        V = paper_view(D, now, tf)
        return backtest(V, st, light="trades", now=now)["trades"]


def _tkey(t):
    return (t["date"], t["in"], t["side"], t["case"])


def paper_diff(sess, today, now):
    """আগের টিকের সাথে তুলনা করে ঘটনা-লগ (ENTRY / PARTIAL / EXIT) বানায়।"""
    prev = sess.get("_prev", {})
    cur = {}
    for t in today:
        k = _tkey(t)
        nl = len(t["legs"]) if t.get("legs") else 1
        cur[k] = (bool(t["open"]), nl)
        if k not in prev:
            paper_event(sess, now, "ENTRY", "%s %s | ইন %s @ %.2f | %d লট" % (t["case"], t["side"], t["in"], t["entry"], t["lots"]))
            if not t["open"]:
                paper_event(sess, now, "EXIT", "%s | %s @ %.2f | নেট %+.0f" % (t["side"], t["reason"], t["exit"], t["net"]))
        else:
            po, pl = prev[k]
            if po and not t["open"]:
                paper_event(sess, now, "EXIT", "%s | %s @ %.2f | নেট %+.0f" % (t["side"], t["reason"], t["exit"], t["net"]))
            elif t["open"] and nl > pl:
                paper_event(sess, now, "PARTIAL", "%s | আংশিক বুকিং (লেগ %d)" % (t["side"], nl))
    sess["_prev"] = cur


def paper_event(sess, now, kind, msg):
    with PLOCK:
        sess["events"].append({"t": hm_str(now), "day": day_str(now // 1440), "k": kind, "msg": msg})
        del sess["events"][:-400]
    if kind in ("ENTRY", "EXIT", "PARTIAL") and sess.get("mode") == "live":
        tg_send(sess, "%s %s | %s" % ({"ENTRY": "🟢", "EXIT": "🔴", "PARTIAL": "🟡"}[kind], kind, msg))


def paper_tick(sess, D, now):
    today = paper_eval(D, sess["settings"], now)
    paper_diff(sess, today, now)
    day = now // 1440
    a = int(np.searchsorted(D.t, day * 1440, "left"))
    b = int(np.searchsorted(D.t, now, "right"))
    sp = [[int(D.t[k] % 1440), float(D.c[k])] for k in range(a, b)]
    if len(sp) > 400:
        sp = sp[::len(sp) // 400 + 1] + [sp[-1]]
    dpnl = float(sum(t["net"] for t in today))
    with PLOCK:
        sess["now"] = int(now)
        sess["day"] = day_str(day)
        sess["today"] = [t for t in today]
        sess["open"] = next((t for t in today if t["open"]), None)
        sess["spot"] = sp
        m = sess.setdefault("mtm", [])
        tot = float(sum(t["net"] for t in sess["closed"])) + dpnl
        if not m or m[-1][0] != int(now):
            m.append([int(now), round(tot, 2)])
            del m[:-3000]
        sess["day_pnl"] = dpnl
    kl = float(sess.get("kill_loss") or 0)
    if kl > 0 and dpnl <= -kl and not sess.get("killed"):
        sess["_killnow"] = "আজকের লস ₹%d সীমা (₹%d) ছুঁয়েছে" % (round(-dpnl), round(kl))
    return today


def tg_send(sess_or_cfg, text):
    """Telegram অ্যালার্ট (ঐচ্ছিক)। ব্যর্থ হলে নীরবে বাদ — ট্রেডিং থামে না।"""
    cfg = sess_or_cfg.get("_tg") if "_tg" in sess_or_cfg else sess_or_cfg
    if not cfg or not cfg.get("token") or not cfg.get("chat"):
        return False

    def go():
        try:
            import requests
            requests.post("https://api.telegram.org/bot%s/sendMessage" % cfg["token"], json={"chat_id": cfg["chat"], "text": text}, timeout=8)
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()
    return True


def paper_kill_finish(sess, today, why):
    """Kill-Switch: খোলা ট্রেড বর্তমান MTM-এ বন্ধ ধরে সেশন থামানো"""
    with PLOCK:
        for t in today:
            if t["open"]:
                t["open"] = False
                t["reason"] = "Kill-Switch"
        sess["killed"] = True
        sess["kill_why"] = why
    paper_event(sess, sess.get("now", 0), "KILL", "⛔ Kill-Switch: %s — সব ট্রেড বন্ধ, সেশন থামল" % why)
    tg_send(sess, "⛔ Green Bull Kill-Switch: %s" % why)
    paper_finalize_day(sess, today)
    PAPER["stop"] = True


def paper_finalize_day(sess, today):
    with PLOCK:
        done = [t for t in today if not t["open"]]
        sess["closed"].extend(done)
        sess["days_done"] = sess.get("days_done", 0) + 1
        sess["_prev"] = {}
        sess["today"], sess["open"] = [], None
    paper_save(sess)


def paper_save(sess):
    try:
        slim = {k: v for k, v in sess.items() if not k.startswith("_")}
        with open(os.path.join(paper_dir(), "%s.json" % sess["id"]), "w", encoding="utf-8") as f:
            json.dump(clean(slim), f, ensure_ascii=False)
    except Exception:
        traceback.print_exc()


def paper_replay(sess):
    D = DATA["D"]
    st = sess["settings"]
    G = st["general"]
    tf = int(float(G["tf"]))
    sq = hm(G["sqoff"])
    d0, d1 = day_int(sess["from"]), day_int(sess["to"])
    days = [d for d in D.du.tolist() if d0 <= d <= d1]
    sess["n_days"] = len(days)
    step = max(int(sess.get("step", tf)), 1)
    delay = float(sess.get("delay", 0))
    for d in days:
        if PAPER["stop"]:
            break
        now = d * 1440 + OPEN_MIN + tf
        end = d * 1440 + sq
        today = []
        while now < end and not PAPER["stop"]:
            today = paper_tick(sess, D, now)
            if sess.get("_killnow"):
                break
            if delay:
                time.sleep(delay)
            now += step
        if sess.get("_killnow"):
            paper_kill_finish(sess, today, sess.pop("_killnow"))
            break
        if PAPER["stop"]:
            break
        today = paper_tick(sess, D, end)
        paper_finalize_day(sess, today)


# ---------------------------------------------------------------------
#  লাইভ ফিড (মাল্টি-ব্রোকার কাঠামো)। ব্রোকার-নির্ভর অংশ শুধু Feed ক্লাসে; পেপার ইঞ্জিন একই থাকে।
#  এখন শুধু Upstox ডেটা-ফিড আছে। অন্য ব্রোকার = নতুন Feed ক্লাস (spot_today / contracts / option_today) + LIVE_FEEDS-এ নাম।
# ---------------------------------------------------------------------
LIVE_KEYS = {"BN": "NSE_INDEX|Nifty Bank", "N50": "NSE_INDEX|Nifty 50", "FIN": "NSE_INDEX|Nifty Fin Service",
             "SEN": "BSE_INDEX|SENSEX"}


def ist_now():
    n = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=5, minutes=30)
    return int((n.date().toordinal() - 719163) * 1440 + n.hour * 60 + n.minute), n.second


def _cmin(ts):
    """Upstox ক্যান্ডেলের সময় ('2026-11-27T09:15:00+05:30') → IST মিনিট (epoch থেকে)"""
    p = pd.Timestamp(str(ts))
    if p.tzinfo is not None:
        p = p.tz_localize(None)
    return int((p - EPOCH) // pd.Timedelta(minutes=1))


def _exp_date(x):
    if isinstance(x, (int, float)):
        return (dt.datetime.fromtimestamp(x / 1000.0, dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=5, minutes=30)).date()
    return dt.date.fromisoformat(str(x)[:10])


class UpstoxFeed:
    name = "Upstox"
    V2 = "https://api.upstox.com/v2"
    V3 = "https://api.upstox.com/v3"

    def __init__(self, token_file):
        self.token_file = token_file
        self.sleep = 0.35
        self._tl = threading.local()

    @property
    def last_body(self):
        return getattr(self._tl, "b", {})

    @last_body.setter
    def last_body(self, v):
        self._tl.b = v

    def get(self, url):
        if not os.path.exists(self.token_file):
            raise ValueError("টোকেন ফাইল নেই: %s" % self.token_file)
        tok = open(self.token_file, encoding="utf-8").read().strip()
        hdr = {"Accept": "application/json", "Authorization": "Bearer " + tok,
               "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"}
        try:
            try:
                import requests          # backend-এর মতোই (WAF অনেক সময় urllib-এর UA আটকায়)
                r = requests.get(url, headers=dict(hdr, **{"User-Agent": "python-requests/2.32"}), timeout=30)
                st, txt = r.status_code, r.text
            except ImportError:
                import urllib.request, urllib.error
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=30) as r:
                        st, txt = r.status, r.read().decode("utf-8")
                except urllib.error.HTTPError as e:
                    st, txt = e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            return 0, {"error": str(e)}
        try:
            body = json.loads(txt or "{}")
        except Exception:
            body = {"raw": (txt or "")[:200]}
        self.last_body = body
        time.sleep(self.sleep)
        return st, body

    @staticmethod
    def why(body):
        """ব্যর্থ উত্তর থেকে ছোট কারণ-বাক্য (বিশ্লেষণের জন্য)"""
        try:
            e = (body.get("errors") or [{}])[0]
            return (e.get("message") or e.get("errorCode") or body.get("message") or body.get("error") or body.get("raw") or "")[:140]
        except Exception:
            return ""

    def _candles(self, key):
        from urllib.parse import quote
        st, body = self.get("%s/historical-candle/intraday/%s/minutes/1" % (self.V3, quote(key, safe="")))
        if st == 401:
            raise ValueError("Upstox টোকেন কাজ করছে না (401) — নতুন টোকেন দিন")
        cs = ((body or {}).get("data") or {}).get("candles") or []
        rows = []
        for r in cs:
            try:
                rows.append((_cmin(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0)))
            except Exception:
                pass
        rows.sort()
        return st, rows

    def spot_today(self, key):
        return self._candles(key)

    def option_today(self, ikey):
        return self._candles(ikey)

    def contracts(self, key):
        from urllib.parse import quote
        st, body = self.get("%s/option/contract?instrument_key=%s" % (self.V2, quote(key, safe="")))
        if st == 401:
            raise ValueError("Upstox টোকেন কাজ করছে না (401) — নতুন টোকেন দিন")
        d = (body or {}).get("data")
        return st, (d if isinstance(d, list) else [])


LIVE_FEEDS = {"upstox": UpstoxFeed}


def make_feed(name):
    cls = LIVE_FEEDS.get(name)
    if cls is None:
        raise ValueError("এই ব্রোকারের ফিড এখনো নেই: %s" % name)
    src = DATA.get("src") or {}
    return cls(os.path.join(src.get("db_dir") or os.getcwd(), "upstox_token.txt"))


class LiveState:
    def __init__(self, feed, inst, band, min_dte):
        self.feed, self.inst, self.band, self.min_dte = feed, inst, band, min_dte
        self.key = LIVE_KEYS[inst]
        self.contracts = None       # নির্বাচিত এক্সপায়ারির কন্ট্র্যাক্ট: {(typ, strike): (ikey, lot)}
        self.expiry = None
        self.spot = []
        self.opt = {}               # (typ, strike) -> rows
        self.fetched = set()
        self.last_err = ""

    def pick_expiry(self, today):
        st, cons = self.feed.contracts(self.key)
        if st != 200 or not cons:
            raise ValueError("কন্ট্র্যাক্ট তালিকা আসেনি (status %s %s)" % (st, UpstoxFeed.why(getattr(self.feed, "last_body", {}))))
        by = {}
        for c in cons:
            try:
                e = _exp_date(c.get("expiry"))
                stk = float(c.get("strike_price"))
            except Exception:
                continue
            if c.get("instrument_type") in ("CE", "PE") and c.get("instrument_key"):
                by.setdefault(e, {})[(c["instrument_type"], stk)] = (c["instrument_key"], int(c.get("lot_size") or 0))
        tgt = dt.date.fromordinal(719163 + today) + dt.timedelta(days=self.min_dte)
        ex = [e for e in sorted(by) if e >= tgt]
        if not ex:
            raise ValueError("আজ বা পরের কোনো এক্সপায়ারি পাওয়া যায়নি")
        self.expiry = ex[0]
        self.contracts = by[self.expiry]

    def refresh(self, now):
        today = now // 1440
        if self.contracts is None:
            self.pick_expiry(today)
        st, rows = self.feed.spot_today(self.key)
        if st != 200:
            raise ValueError("স্পট ক্যান্ডেল আসেনি (status %s %s)" % (st, UpstoxFeed.why(getattr(self.feed, "last_body", {}))))
        self.spot = [r for r in rows if r[0] // 1440 == today]
        if not self.spot:
            return False
        strikes = sorted({k[1] for k in self.contracts})
        last = self.spot[-1][4]
        i = int(np.argmin(np.abs(np.array(strikes) - last)))
        for j in range(max(0, i - self.band), min(len(strikes), i + self.band + 1)):
            for typ in ("CE", "PE"):
                if (typ, strikes[j]) in self.contracts:
                    self.fetched.add((typ, strikes[j]))
        from concurrent.futures import ThreadPoolExecutor
        far = {k for k in self.fetched if abs(strikes.index(k[1]) - i) > self.band + 6} if self.fetched else set()
        self.fetched -= far          # দাম অনেক সরে গেলে পুরনো দূরের স্ট্রাইক আর পোল নয়
        ks = sorted(self.fetched)

        def one(k):
            if PAPER["stop"]:
                return k, 0, []
            st, r = self.feed.option_today(self.contracts[k][0])
            return k, st, r
        with ThreadPoolExecutor(max_workers=4) as ex:
            for k, st, r in ex.map(one, ks):
                if st == 200:
                    self.opt[k] = [x for x in r if x[0] // 1440 == today]
                elif st:
                    self.last_err = "অপশন %s %s: status %s" % (k[0], k[1], st)
        return True

    def build(self, Dh, now):
        """ইতিহাস (Dh-এর শেষ ৩০ দিন) + আজকের লাইভ বার → ডেটা-অবজেক্ট"""
        today = now // 1440
        i0 = int(np.searchsorted(Dh.day, today - 30, "left"))
        i1 = int(np.searchsorted(Dh.day, today, "left"))
        a = np.array(self.spot, dtype="float64")
        t = np.r_[Dh.t[i0:i1], a[:, 0].astype("int64")]
        V = Data()
        V._bars, V._cache = {}, {}
        V.t = t
        V.o, V.h, V.l, V.c, V.v = (np.r_[getattr(Dh, n)[i0:i1], a[:, k]] for n, k in (("o", 1), ("h", 2), ("l", 3), ("c", 4), ("v", 5)))
        _spot_finish(V, lambda *x: None, view=True)
        ex = day_int(self.expiry.isoformat())
        V.opt, V.strikes, V.exp_lot, V.exp_range = {}, {}, {}, {}
        lo = hi = None
        for (typ, stk), rows in self.opt.items():
            if not rows:
                continue
            b = np.array(rows, dtype="float64")
            V.opt[(ex, typ, float(stk))] = (b[:, 0].astype("int64"), b[:, 1], b[:, 2], b[:, 3], b[:, 4])
            lo = int(b[0, 0]) if lo is None else min(lo, int(b[0, 0]))
            hi = int(b[-1, 0]) if hi is None else max(hi, int(b[-1, 0]))
            if self.contracts[(typ, stk)][1]:
                V.exp_lot[ex] = self.contracts[(typ, stk)][1]
        if lo is not None:
            V.exp_range[ex] = (lo, hi)
        _opt_finish(V, lambda *x: None)
        return V


def live_selftest(inst, provider="upstox"):
    """সাবধানী কানেকশন টেস্ট: প্রতিটি ধাপের status ও কয়টা বার এল — সমস্যা হলে এই আউটপুট পাঠান।"""
    out = []

    def ck(name, ok, detail):
        out.append({"name": name, "ok": bool(ok), "detail": detail})
    try:
        feed = make_feed(provider)
        has = os.path.exists(feed.token_file) and os.path.getsize(feed.token_file) > 10
        ck("টোকেন ফাইল", has, feed.token_file + ("" if has else " — নেই/খালি (upstox_token.txt রাখুন ডেটা-ফোল্ডারে)"))
        if not has:
            return {"checks": out, "all_ok": False}
        key = LIVE_KEYS[inst]
        st, rows = feed.spot_today(key)
        ck("স্পট আজকের ১-মিনিট ক্যান্ডেল", st == 200 and len(rows) > 0,
           "status %s, %d বার%s" % (st, len(rows), (", শেষ " + hm_str(rows[-1][0]) + " IST") if rows else ((" — " + UpstoxFeed.why(feed.last_body)) if st != 200 else " (বাজার বন্ধ/ছুটি হলে ০ স্বাভাবিক)")))
        st, cons = feed.contracts(key)
        ck("চলতি অপশন কন্ট্র্যাক্ট তালিকা", st == 200 and len(cons) > 0, "status %s, %d কন্ট্র্যাক্ট%s" % (st, len(cons), (" — " + UpstoxFeed.why(feed.last_body)) if st != 200 else ""))
        if cons:
            ex = sorted({_exp_date(c.get("expiry")) for c in cons if c.get("expiry") is not None})
            ck("এক্সপায়ারি", bool(ex), ", ".join(str(e) for e in ex[:4]))
            if rows:
                last = rows[-1][4]
                e0 = ex[0]
                cs = [c for c in cons if _exp_date(c.get("expiry")) == e0 and c.get("instrument_type") == "CE"]
                if cs:
                    c = min(cs, key=lambda c: abs(float(c["strike_price"]) - last))
                    st2, r2 = feed.option_today(c["instrument_key"])
                    ck("অপশন (ATM CE) ১-মিনিট", st2 == 200 and len(r2) > 0, "%s স্ট্রাইক %s: status %s, %d বার" % (e0, c["strike_price"], st2, len(r2)))
    except Exception as e:
        ck("ত্রুটি", False, "%s: %s" % (type(e).__name__, e))
    return {"checks": out, "all_ok": all(c["ok"] for c in out) and len(out) >= 3}


def paper_live(sess):
    """লাইভ পেপার: প্রতি মিনিটে ফিড টেনে আজকের দিন আবার হিসাব। একটি দিনের সেশন (বাজার বন্ধে শেষ)।"""
    D = DATA["D"]
    G = sess["settings"]["general"]
    inst = sess.get("inst") or (DATA.get("src") or {}).get("inst") or ""
    if inst not in LIVE_KEYS:
        raise ValueError("লাইভের জন্য ইনডেক্স অজানা (%s)" % inst)
    feed = make_feed(sess.get("feed", "upstox"))
    LS = LiveState(feed, inst, int(sess.get("band", 3)), int(G["min_dte"]))
    sq = hm(G["sqoff"])
    sess["msg"] = "লাইভ ফিডের অপেক্ষা…"
    clock = sess.get("_clock") or ist_now
    def isleep(n):
        end = time.time() + n
        while time.time() < end and not PAPER["stop"]:
            time.sleep(0.25)
    sleeper = sess.get("_sleep") or isleep
    last_now = -1
    fails = 0
    while not PAPER["stop"]:
        now, sec = clock()
        mod = now % 1440
        if mod < OPEN_MIN + 1:
            sess["msg"] = "বাজার খোলার অপেক্ষা (৯:১৫)…"
            sleeper(15)
            continue
        if now == last_now:
            sleeper(max(1, 60 - sec + 3))
            continue
        t_net = time.time()
        try:
            ok = LS.refresh(now)
            fails = 0
        except ValueError as e:
            sess["msg"] = "ফিড ত্রুটি: %s" % e
            if "টোকেন" in str(e):
                raise
            fails += 1
            lim = int(sess.get("kill_feed") or 0)
            if lim and fails >= lim and sess.get("open"):
                paper_kill_finish(sess, list(sess.get("today") or []), "ফিড টানা %d বার ব্যর্থ — খোলা ট্রেড বন্ধ" % fails)
                break
            sleeper(20)
            continue
        sess["feed_secs"] = round(time.time() - t_net, 1)
        if PAPER["stop"]:
            break
        if not ok:
            sess["msg"] = "আজকের স্পট ক্যান্ডেল নেই (ছুটি/বাজার বন্ধ?)"
            if mod > sq + 5:
                break
            sleeper(30)
            continue
        Dl = LS.build(D, now)
        today = paper_tick(sess, Dl, now)
        if sess.get("_killnow"):
            paper_kill_finish(sess, today, sess.pop("_killnow"))
            break
        sess["msg"] = "চলছে | ফিড %.1fs | স্ট্রাইক %d | %s" % (sess["feed_secs"], len(LS.fetched), LS.last_err)
        last_now = now
        if mod >= sq:
            paper_finalize_day(sess, today)
            sess["msg"] = "আজকের সেশন শেষ"
            break
        sleeper(max(1, 60 - sec + 3))


def paper_start(payload):
    if PAPER["thread"] is not None and PAPER["thread"].is_alive():
        raise ValueError("একটি পেপার সেশন চলছে — আগে থামান")
    mode = payload.get("mode", "replay")
    st = norm_settings(payload["settings"])
    if st["general"].get("chart") == "prem":
        raise ValueError("পেপার ট্রেড এখন শুধু স্পট-সিগন্যাল মোডে চলে (প্রিমিয়াম-চার্ট মোডে নয়)")
    D = DATA["D"]
    sid = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    sess = {"id": sid, "name": payload.get("name") or ("Paper " + sid), "mode": mode, "inst": (DATA.get("src") or {}).get("inst", ""),
            "started": dt.datetime.now().isoformat(timespec="seconds"), "status": "running", "msg": "",
            "settings": st, "capital": st["general"]["capital"], "closed": [], "events": [], "today": [], "open": None,
            "now": 0, "day": "", "days_done": 0, "n_days": 0,
            "from": payload.get("from") or day_str(int(D.du[-1])), "to": payload.get("to") or day_str(int(D.du[-1])),
            "step": int(payload.get("step") or st["general"]["tf"]), "delay": float(payload.get("delay") or 0),
            "feed": payload.get("feed", "upstox"), "band": int(payload.get("band") or 3),
            "kill_loss": float(payload.get("kill_loss") or 0), "kill_feed": int(payload.get("kill_feed") or 0),
            "_tg": {"token": (payload.get("tg_token") or "").strip(), "chat": (payload.get("tg_chat") or "").strip()}}
    if payload.get("_test_clock"):
        sess["_clock"], sess["_sleep"] = payload["_test_clock"], payload["_test_sleep"]
    PAPER["stop"] = False
    PAPER["sess"] = sess

    def work():
        try:
            if mode == "replay":
                paper_replay(sess)
            else:
                paper_live(sess)
            sess["status"] = "stopped" if PAPER["stop"] else "done"
        except Exception as e:
            traceback.print_exc()
            sess["status"] = "error"
            sess["msg"] = "%s: %s" % (type(e).__name__, e)
        finally:
            paper_save(sess)
    th = threading.Thread(target=work, daemon=True)
    PAPER["thread"] = th
    th.start()
    return sid


def paper_status():
    sess = PAPER["sess"]
    if not sess:
        return {"sess": None}
    with PLOCK:
        out = {k: v for k, v in sess.items() if not k.startswith("_") and k not in ("settings", "closed")}
        closed = list(sess["closed"])
        today_closed = [t for t in sess.get("today", []) if not t["open"]]
    allc = closed + today_closed
    cap = sess["capital"]
    out["stats"] = calc_stats(allc, cap)
    out["trades"] = allc
    out["equity"] = [round(x, 2) for x in np.cumsum([t["net"] for t in allc]).tolist()] if allc else []
    out["live_pnl"] = float(sum(t["net"] for t in allc) + (out["open"]["net"] if out.get("open") else 0.0))
    out["running"] = bool(PAPER["thread"] is not None and PAPER["thread"].is_alive())
    return {"sess": out}


def paper_list():
    res = []
    for fn in sorted(glob.glob(os.path.join(paper_dir(), "*.json")))[-30:]:
        try:
            with open(fn, encoding="utf-8") as f:
                s = json.load(f)
            res.append({"id": s["id"], "name": s.get("name"), "mode": s.get("mode"), "from": s.get("from"), "to": s.get("to"),
                        "n": len(s.get("closed", [])), "net": float(sum(t["net"] for t in s.get("closed", []))), "status": s.get("status")})
        except Exception:
            pass
    return {"list": res[::-1]}


UPD = {"running": False, "log": [], "started": ""}


def run_update(backend_dir, db_dir):
    """backend-এর ডাউনলোডারগুলো পালা করে চালায়: স্পট → ফিউচার ভলিউম → অপশন"""
    env = dict(os.environ, GB_HOME=db_dir, PYTHONUNBUFFERED="1")
    try:
        for name, script in (("স্পট", "downloader.py"), ("ফিউচার ভলিউম", "futures.py"), ("অপশন", "options.py")):
            UPD["log"].append("== %s ==" % name)
            p = subprocess.Popen([sys.executable, "-u", script], cwd=backend_dir, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in p.stdout:
                UPD["log"].append(line.rstrip())
                del UPD["log"][:-300]
            p.wait()
            if p.returncode:
                UPD["log"].append("⚠ %s ধাপ ত্রুটিতে থেমেছে (কোড %s)" % (name, p.returncode))
    except Exception as e:
        UPD["log"].append("ত্রুটি: %s" % e)
    finally:
        UPD["running"] = False


def start_job(kind, payload):
    JOB_N[0] += 1
    jid = str(JOB_N[0])
    JOBS[jid] = {"status": "queued", "progress": 0.0}
    for old in list(JOBS.keys())[:-20]:
        JOBS.pop(old, None)

    def work():
        with RUN_LOCK:
            JOBS[jid]["status"] = "running"
            t0 = dt.datetime.now()
            try:
                prog = lambda p: JOBS[jid].__setitem__("progress", float(p))
                D = DATA["D"]
                if kind == "run":
                    res = backtest(D, payload["settings"], progress=prog)
                elif kind == "sell":
                    res = sell_backtest(D, payload["settings"], progress=prog)
                elif kind == "sellprev":
                    res = sell_preview(D, payload["settings"], (payload.get("opts") or {}).get("date"))
                elif kind == "portfolio":
                    res = portfolio_run(D, payload["settings"], progress=prog)
                elif kind == "stress":
                    res = stress_test(D, payload["settings"])
                elif kind == "advise2":
                    res = advise2(D, payload["settings"], payload.get("opts", {}), progress=prog)
                elif kind == "advise":
                    res = advise(D, payload["settings"], payload.get("opts", {}), progress=prog)
                elif kind == "multi":
                    res = run_multi(payload, prog)
                else:
                    res = run_sweep(D, payload["settings"], payload["params"], progress=prog)
                JOBS[jid].update(status="done", progress=1.0, result=clean(res),
                                 secs=(dt.datetime.now() - t0).total_seconds())
            except Exception as e:
                traceback.print_exc()
                JOBS[jid].update(status="error", error="%s: %s" % (type(e).__name__, e))
    threading.Thread(target=work, daemon=True).start()
    return jid


def data_info():
    D = DATA["D"]
    ex = []
    for e in D.expiries:
        lo, hi = D.exp_range[e]
        ex.append({"expiry": day_str(e), "lot": D.exp_lot.get(e), "from": day_str(lo // 1440), "to": day_str(hi // 1440),
                   "ce": int(len(D.strikes.get((e, "CE"), []))), "pe": int(len(D.strikes.get((e, "PE"), [])))})
    src = DATA.get("src") or {}
    return {"spot_from": day_str(D.du[0]), "spot_to": day_str(D.du[-1]), "spot_days": int(len(D.du)),
            "expiries": ex, "contracts": len(D.opt), "base": BASE_TF,
            "name": src.get("name", "BankNifty"), "inst": src.get("inst", ""), "mode": src.get("mode", "csv"),
            "from": src.get("from", ""), "to": src.get("to", ""),
            "vol_days": int(len(np.unique(D.day[D.v > 0])))}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(clean(obj), ensure_ascii=False))

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, HTML, "text/html")
            if u.path == "/api/meta":
                return self._json({"sources": SOURCES, "ops": OPS, "defaults": default_settings(),
                                   "case": default_case(1), "slot": slot(), "info": data_info(),
                                   "sell": {"templates": sell_templates(), "defaults": sell_defaults()},
                                   "datasets": (datasets_info(DATA["src"]["db_dir"]) if (DATA.get("src") or {}).get("mode") == "db" else None),
                                   "presets": {"builtin": builtin_presets(), "user": load_user_presets()}})
            if u.path == "/api/update/status":
                return self._json({"running": UPD["running"], "log": UPD["log"][-60:], "started": UPD["started"]})
            if u.path == "/api/paper/status":
                return self._json(clean(paper_status()))
            if u.path == "/api/paper/list":
                return self._json(paper_list())
            if u.path == "/api/job":
                jid = parse_qs(u.query).get("id", [""])[0]
                j = JOBS.get(jid)
                return self._json(j if j else {"status": "error", "error": "job নেই"})
            return self._send(404, "not found", "text/plain")
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            if u.path == "/api/paper/start":
                return self._json({"id": paper_start(body)})
            if u.path == "/api/copilot":
                key = llm_key(body.get("key"))
                if not key:
                    return self._json({"error": "API Key দেওয়া নেই (এটি ঐচ্ছিক ফিচার; অফলাইন অ্যাডভাইজার Key ছাড়াই চলে)"})
                try:
                    if body.get("mode") == "idea":
                        return self._json({"case": copilot_idea(str(body.get("text", ""))[:1500], key)})
                    return self._json({"text": copilot_explain(body.get("payload") or {}, key)})
                except Exception as e:
                    return self._json({"error": str(e)})
            if u.path == "/api/live/test":
                src = DATA.get("src") or {}
                return self._json(live_selftest(body.get("inst") or src.get("inst") or "N50", body.get("provider", "upstox")))
            if u.path == "/api/alert/test":
                ok = tg_send({"token": str(body.get("token", "")).strip(), "chat": str(body.get("chat", "")).strip()}, "✅ Green Bull Algo Builder: Telegram অ্যালার্ট সংযোগ ঠিক আছে")
                return self._json({"ok": bool(ok), "error": None if ok else "Token ও Chat ID দুটোই দিন"})
            if u.path == "/api/paper/stop":
                PAPER["stop"] = True
                return self._json({"ok": True})
            if u.path == "/api/paper/reset":
                if PAPER["thread"] is not None and PAPER["thread"].is_alive():
                    return self._json({"error": "আগে থামান"}, 400)
                PAPER["sess"] = None
                return self._json({"ok": True})
            if u.path == "/api/paper/load":
                fn = os.path.join(paper_dir(), os.path.basename(str(body.get("id"))) + ".json")
                if not os.path.exists(fn):
                    return self._json({"error": "সেশন ফাইল নেই"}, 404)
                if PAPER["thread"] is not None and PAPER["thread"].is_alive():
                    return self._json({"error": "আগে চলমান সেশন থামান"}, 400)
                with open(fn, encoding="utf-8") as f:
                    PAPER["sess"] = json.load(f)
                PAPER["sess"].setdefault("today", [])
                PAPER["sess"]["open"] = None
                return self._json({"ok": True})
            if u.path == "/api/start":
                return self._json({"id": start_job(body["kind"], body)})
            if u.path == "/api/update":
                src = DATA.get("src")
                bd = DATA.get("backend") or ""
                if not src or src.get("mode") != "db":
                    return self._json({"error": "আপডেট শুধু --db মোডে"}, 400)
                if not os.path.exists(os.path.join(bd, "downloader.py")):
                    return self._json({"error": "backend ফোল্ডার পাওয়া যায়নি: %s (--backend দিয়ে দিন)" % bd}, 400)
                if UPD["running"]:
                    return self._json({"started": False})
                UPD.update(running=True, log=[], started=dt.datetime.now().isoformat(timespec="seconds"))
                threading.Thread(target=run_update, args=(bd, src["db_dir"]), daemon=True).start()
                return self._json({"started": True})
            if u.path == "/api/load":
                src = DATA.get("src")
                if not src or src.get("mode") != "db":
                    return self._json({"error": "এই মোড CSV-র। SQLite ডেটার জন্য --db দিয়ে চালান"}, 400)
                inst = str(body.get("inst", "")).upper()
                with RUN_LOCK:
                    DATA["D"] = load_db(src["db_dir"], inst, body.get("from") or None, body.get("to") or None)
                    d = DATA["D"]
                    src.update(inst=inst, name=INSTR_NAMES.get(inst, inst), **{"from": day_str(d.du[0]), "to": day_str(d.du[-1])})
                info = data_info()
                if not info["expiries"]:
                    info["warn"] = "এই ইনডেক্সে অপশন ডেটা এখনো নেই (ডাউনলোড বাকি)। ব্যাকটেস্ট চলবে না।"
                return self._json({"info": info})
            if u.path == "/api/sell/chain":
                with RUN_LOCK:
                    return self._json(clean(sell_chain(DATA["D"], body.get("settings"), body)))
            if u.path == "/api/chart":
                with RUN_LOCK:
                    return self._json(chart_data(DATA["D"], body["settings"], body))
            if u.path == "/api/preset/save":
                p = load_user_presets()
                p[body["name"]] = body["settings"]
                save_user_presets(p)
                return self._json({"ok": True, "user": p})
            if u.path == "/api/preset/delete":
                p = load_user_presets()
                p.pop(body["name"], None)
                save_user_presets(p)
                return self._json({"ok": True, "user": p})
            return self._send(404, "not found", "text/plain")
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spot", default="banknifty_5min.csv")
    ap.add_argument("--opt", default="data_opt")
    ap.add_argument("--db", default="", help="gb.db ও gb_opt.db যে ফোল্ডারে আছে (SQLite মোড)")
    ap.add_argument("--inst", default="N50", help="SQLite মোডে: BN, N50, FIN, SEN")
    ap.add_argument("--from", dest="d_from", default="", help="শুরুর তারিখ YYYY-MM-DD (ফাঁকা = সব ডেটা)")
    ap.add_argument("--backend", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"),
                    help="downloader.py ইত্যাদির ফোল্ডার (ডেটা আপডেট বোতামের জন্য)")
    ap.add_argument("--to", dest="d_to", default="", help="শেষ তারিখ YYYY-MM-DD")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    print("ডেটা লোড হচ্ছে...")
    if a.db:
        db_dir = os.path.abspath(os.path.expanduser(a.db))
        inst = a.inst.upper()
        DATA["D"] = load_db(db_dir, inst, a.d_from or None, a.d_to or None)
        D = DATA["D"]
        DATA["backend"] = os.path.abspath(os.path.expanduser(a.backend))
        DATA["src"] = {"mode": "db", "db_dir": db_dir, "inst": inst, "name": INSTR_NAMES.get(inst, inst),
                       "from": day_str(D.du[0]), "to": day_str(D.du[-1])}
        if not D.expiries:
            print("সতর্কতা: %s-এর অপশন ডেটা এখনো নেই। অন্য ইনডেক্স বা সময় বেছে নিন (Data ট্যাব)।" % inst)
    else:
        if not os.path.exists(a.spot):
            raise SystemExit("স্পট ফাইল নেই: %s (ফোল্ডারটা ঠিক আছে তো?)" % a.spot)
        DATA["D"] = load_data(a.spot, a.opt)
        if not DATA["D"].expiries:
            raise SystemExit("অপশন ডেটা পাওয়া যায়নি: %s ফোল্ডারে opt_*.csv.gz নেই" % a.opt)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("\nতৈরি! ব্রাউজারে খুলুন:  http://127.0.0.1:%d\n(বন্ধ করতে Ctrl+C)" % a.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nবন্ধ।")


HTML = r'''@@HTML@@'''

if __name__ == "__main__":
    main()
