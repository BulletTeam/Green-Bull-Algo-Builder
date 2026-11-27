#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ALGO BUILDER - অপশন প্রিমিয়াম ব্যাকটেস্ট বিল্ডার (সব টিউনিং সেটিংস থেকে, কোডিং লাগে না)

চালানো (Termux বা যেকোনো Python-এ):
    python algo_builder.py
তারপর ফোনের ব্রাউজারে খুলুন:   http://127.0.0.1:8000

একই ফোল্ডারে থাকতে হবে:
    banknifty_5min.csv       (স্পট ইনডেক্সের ৫ মিনিট ডেটা)
    data_opt/opt_*.csv.gz    (download_options.py যা নামিয়েছে)
অন্য নামে থাকলে:  python algo_builder.py --spot FILE.csv --opt FOLDER --port 8000

লাগে শুধু: numpy, pandas (আপনার Termux-এ আগে থেকেই আছে)
"""
import os
import sys
import json
import time
import math
import copy
import glob
import bisect
import argparse
import itertools
import threading
import subprocess
import traceback
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np
import pandas as pd

NAN = float("nan")
OPEN_MIN = 555          # 09:15
BASE_TF = 5             # ডেটার ক্যান্ডেল মিনিট (লোডের সময় নিজে বের হয়)
EPOCH = pd.Timestamp("1970-01-01")

# =====================================================================
#  ডেটা লোড
# =====================================================================


class Data:
    pass


def _to_min(series):
    d = pd.to_datetime(series)
    if getattr(d.dt, "tz", None) is not None:
        d = d.dt.tz_localize(None)
    return ((d - EPOCH) // pd.Timedelta(minutes=1)).to_numpy().astype("int64")


def _to_days(series):
    d = pd.to_datetime(series)
    if getattr(d.dt, "tz", None) is not None:
        d = d.dt.tz_localize(None)
    return ((d - EPOCH) // pd.Timedelta(days=1)).to_numpy().astype("int64")


def day_str(d):
    return dt.date.fromordinal(719163 + int(d)).isoformat()


def day_int(s):
    return dt.date.fromisoformat(s).toordinal() - 719163


def hm_str(m):
    m = int(m) % 1440
    return "%02d:%02d" % (m // 60, m % 60)


def hm(s):
    a, b = str(s).strip().split(":")
    return int(a) * 60 + int(b)


def _spot_finish(D, log, view=False):
    """D.t/o/h/l/c/v ভরা থাকলে বাকি স্পট-হিসাব (দিন, BASE_TF, দৈনিক OHLC) বানায়"""
    D.day = D.t // 1440
    D.mod = D.t % 1440
    global BASE_TF
    if not view:
        dd = np.diff(D.t)[np.diff(D.day) == 0]
        BASE_TF = int(np.median(dd)) if len(dd) else 5
    D.base = BASE_TF
    D.du = np.unique(D.day)
    st = np.flatnonzero(np.r_[True, D.day[1:] != D.day[:-1]])
    en = np.r_[st[1:], len(D.day)]
    D.dO = D.o[st]
    D.dH = np.maximum.reduceat(D.h, st)
    D.dL = np.minimum.reduceat(D.l, st)
    D.dC = D.c[en - 1]
    log("স্পট ডেটা: %d ক্যান্ডেল, %d দিন (%s → %s)" % (len(D.t), len(D.du), day_str(D.du[0]), day_str(D.du[-1])))


def _opt_finish(D, log):
    for (ex, typ, stk) in D.opt:
        D.strikes.setdefault((ex, typ), set()).add(stk)
    D.strikes = {k: np.array(sorted(v), dtype="float64") for k, v in D.strikes.items()}
    D.expiries = sorted(D.exp_range.keys())
    log("অপশন ডেটা: %d এক্সপায়ারি, %d কন্ট্র্যাক্ট" % (len(D.expiries), len(D.opt)))


def load_data(spot_csv, opt_dir, log=print):
    D = Data()
    D._bars = {}
    D._cache = {}
    sp = pd.read_csv(spot_csv)
    sp.columns = [c.strip().lower() for c in sp.columns]
    for c in ["datetime", "open", "high", "low", "close"]:
        if c not in sp.columns:
            raise ValueError("স্পট CSV-তে '%s' কলাম নেই" % c)
    if "volume" not in sp.columns:
        sp["volume"] = 0
    sp["t"] = _to_min(sp["datetime"])
    sp = sp.sort_values("t").drop_duplicates("t")
    mod = sp["t"] % 1440
    sp = sp[(mod >= OPEN_MIN) & (mod <= OPEN_MIN + 374)]
    D.t = sp["t"].to_numpy("int64")
    D.o = sp["open"].to_numpy("float64")
    D.h = sp["high"].to_numpy("float64")
    D.l = sp["low"].to_numpy("float64")
    D.c = sp["close"].to_numpy("float64")
    D.v = sp["volume"].fillna(0).to_numpy("float64")
    _spot_finish(D, log)

    D.opt = {}
    D.strikes = {}
    D.exp_lot = {}
    D.exp_range = {}
    D.expiries = []
    files = sorted(glob.glob(os.path.join(opt_dir, "*.csv.gz")) + glob.glob(os.path.join(opt_dir, "*.csv")))
    want = {"datetime", "expiry", "type", "strike", "lot", "open", "high", "low", "close"}
    for f in files:
        try:
            df = pd.read_csv(f, usecols=lambda c: c.strip().lower() in want)
        except Exception as e:
            log("  বাদ (পড়া যায়নি): %s %s" % (f, e))
            continue
        df.columns = [c.strip().lower() for c in df.columns]
        if df.empty:
            continue
        df["t"] = _to_min(df["datetime"])
        df["ex"] = _to_days(df["expiry"])
        df["strike"] = df["strike"].astype(float)
        for (ex, typ, stk), g in df.groupby(["ex", "type", "strike"], sort=False):
            g = g.sort_values("t").drop_duplicates("t")
            D.opt[(int(ex), str(typ), float(stk))] = (
                g["t"].to_numpy("int64"), g["open"].to_numpy("float64"),
                g["high"].to_numpy("float64"), g["low"].to_numpy("float64"),
                g["close"].to_numpy("float64"))
        for ex, g in df.groupby("ex"):
            ex = int(ex)
            D.exp_lot[ex] = int(g["lot"].mode().iloc[0])
            lo, hi = int(g["t"].min()), int(g["t"].max())
            if ex in D.exp_range:
                lo, hi = min(lo, D.exp_range[ex][0]), max(hi, D.exp_range[ex][1])
            D.exp_range[ex] = (lo, hi)
        log("  লোড: %s (%d সারি)" % (os.path.basename(f), len(df)))
    _opt_finish(D, log)
    return D


INSTR_NAMES = {"BN": "BankNifty", "N50": "Nifty 50", "FIN": "FinNifty", "SEN": "Sensex"}


def _load_db_raw(db_dir, inst, d0=None, d1=None, log=print):
    """নতুন SQLite ডেটা (gb.db স্পট + gb_opt.db অপশন) থেকে D বানায়। d0/d1 = 'YYYY-MM-DD' (ঐচ্ছিক)।
    ফাঁকা থাকলে সব ডেটা (সময় ও মেমোরি বেশি লাগে; দরকারে তারিখ সীমা দিন)।"""
    import sqlite3
    sp_path = os.path.join(db_dir, "gb.db")
    op_path = os.path.join(db_dir, "gb_opt.db")
    if not os.path.exists(sp_path):
        raise ValueError("gb.db পাওয়া যায়নি: %s" % sp_path)
    D = Data()
    D._bars = {}
    D._cache = {}
    con = sqlite3.connect(sp_path, timeout=30)
    try:
        first, last = con.execute("SELECT MIN(t), MAX(t) FROM bars WHERE instr=?", (inst,)).fetchone()
        if last is None:
            raise ValueError("%s-এর স্পট ডেটা gb.db-তে নেই" % inst)
        day1 = day_int(d1) if d1 else int(last // 1440)
        day0 = day_int(d0) if d0 else int(first // 1440)
        rows = con.execute("SELECT t,o,h,l,c,v FROM bars WHERE instr=? AND t>=? AND t<? ORDER BY t",
                           (inst, day0 * 1440, (day1 + 1) * 1440)).fetchall()
        try:
            fv = con.execute("SELECT t,v FROM fvol WHERE instr=? AND t>=? AND t<? ORDER BY t",
                             (inst, day0 * 1440, (day1 + 1) * 1440)).fetchall()
        except sqlite3.OperationalError:
            fv = []
    finally:
        con.close()
    if not rows:
        raise ValueError("%s: এই সময়ে স্পট ডেটা নেই (%s → %s)" % (inst, day_str(day0), day_str(day1)))
    a = np.array(rows, dtype="float64")
    t = a[:, 0].astype("int64")
    mod = t % 1440
    keep = (mod >= OPEN_MIN) & (mod <= OPEN_MIN + 374)
    D.t = t[keep]
    D.o, D.h, D.l, D.c, D.v = (a[keep, k].copy() for k in (1, 2, 3, 4, 5))
    if fv:      # স্পট ইনডেক্সে ভলিউম নেই, তাই নিকটতম মাসিক ফিউচারের ভলিউম বসাই
        fa = np.array(fv, dtype="float64")
        ft = fa[:, 0].astype("int64")
        idx = np.minimum(np.searchsorted(ft, D.t), len(ft) - 1)
        D.v = np.where(ft[idx] == D.t, fa[idx, 1], 0.0)
    _spot_finish(D, log)

    D.opt = {}
    D.strikes = {}
    D.exp_lot = {}
    D.exp_range = {}
    D.expiries = []
    if os.path.exists(op_path):
        oc = sqlite3.connect(op_path, timeout=30)
        try:
            ks = oc.execute("SELECT cid, expiry, otype, strike, lot FROM contracts WHERE instr=? AND expiry>=? AND expiry<=?",
                            (inst, day_str(day0), day_str(day1 + 45))).fetchall()
            lo_t, hi_t = day0 * 1440, (day1 + 1) * 1440
            for n, (cid, ex_s, typ, stk, lot) in enumerate(ks, 1):
                r = oc.execute("SELECT t,o,h,l,c FROM obars WHERE cid=? AND t>=? AND t<? ORDER BY t",
                               (cid, lo_t, hi_t)).fetchall()
                if not r:
                    continue
                b = np.array(r, dtype="int64")
                ex = day_int(ex_s)
                D.opt[(ex, str(typ), float(stk))] = (b[:, 0].copy(), b[:, 1] / 100.0, b[:, 2] / 100.0,
                                                    b[:, 3] / 100.0, b[:, 4] / 100.0)
                if lot:
                    D.exp_lot[ex] = int(lot)
                lo, hi = int(b[0, 0]), int(b[-1, 0])
                if ex in D.exp_range:
                    lo, hi = min(lo, D.exp_range[ex][0]), max(hi, D.exp_range[ex][1])
                D.exp_range[ex] = (lo, hi)
                if n % 300 == 0:
                    log("  অপশন লোড: %d/%d" % (n, len(ks)))
        finally:
            oc.close()
    _opt_finish(D, log)
    return D


def load_db(db_dir, inst, d0=None, d1=None, log=print):
    """SQLite থেকে লোড; প্রথমবারের পর ডিস্ক-ক্যাশ (pickle) থেকে কয়েক সেকেন্ডে। ডেটাবেস বদলালে (আপডেট) ক্যাশ নিজে বাতিল।"""
    import pickle, hashlib
    global BASE_TF
    try:
        sig = []
        for fn in ("gb.db", "gb_opt.db"):
            pth = os.path.join(db_dir, fn)
            stt = os.stat(pth) if os.path.exists(pth) else None
            sig.append("%s:%s:%s" % (fn, stt.st_size if stt else 0, int(stt.st_mtime) if stt else 0))
        key = hashlib.md5(("|".join(sig) + "|%s|%s|%s|v1" % (inst, d0, d1)).encode()).hexdigest()[:12]
        cdir = os.path.join(db_dir, "cache")
        cf = os.path.join(cdir, "D_%s_%s.pkl" % (inst, key))
    except Exception:
        return _load_db_raw(db_dir, inst, d0, d1, log)
    if os.path.exists(cf):
        try:
            t0 = time.time()
            with open(cf, "rb") as f:
                D = pickle.load(f)
            BASE_TF = int(D.base)
            D._bars, D._cache = {}, {}
            log("ক্যাশ থেকে লোড (%.1f সেকেন্ড): %s, %d দিন, %d কন্ট্র্যাক্ট" % (time.time() - t0, inst, len(D.du), len(D.opt)))
            return D
        except Exception as e:
            log("ক্যাশ পড়া গেল না (%s), নতুন করে লোড হচ্ছে" % e)
    D = _load_db_raw(db_dir, inst, d0, d1, log)
    try:
        os.makedirs(cdir, exist_ok=True)
        for old in glob.glob(os.path.join(cdir, "D_%s_*.pkl" % inst)):
            os.remove(old)
        D._bars, D._cache = {}, {}
        tmp = cf + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(D, f, protocol=5)
        os.replace(tmp, cf)
        log("ক্যাশ সেভ হয়েছে — পরেরবার দ্রুত খুলবে (%.0f MB)" % (os.path.getsize(cf) / 1e6))
    except Exception as e:
        log("ক্যাশ সেভ হয়নি: %s" % e)
    return D


def datasets_info(db_dir):
    """gb.db / gb_opt.db-তে কোন ইনডেক্সের কতটা ডেটা আছে (UI-র তালিকার জন্য)"""
    import sqlite3
    out = {}
    con = sqlite3.connect(os.path.join(db_dir, "gb.db"), timeout=30)
    try:
        for inst, t0, t1 in con.execute("SELECT instr, MIN(t), MAX(t) FROM bars GROUP BY instr"):
            out[inst] = {"inst": inst, "name": INSTR_NAMES.get(inst, inst), "spot_from": day_str(t0 // 1440),
                         "spot_to": day_str(t1 // 1440), "contracts": 0, "done": 0, "opt_from": "", "opt_to": ""}
    finally:
        con.close()
    op = os.path.join(db_dir, "gb_opt.db")
    if os.path.exists(op):
        oc = sqlite3.connect(op, timeout=30)
        try:
            q = ("SELECT k.instr, COUNT(*), COALESCE(SUM(s.done),0), MIN(k.expiry), MAX(k.expiry) "
                 "FROM contracts k LEFT JOIN ostate s ON s.cid=k.cid GROUP BY k.instr")
            for inst, n, done, e0, e1 in oc.execute(q):
                if inst in out:
                    out[inst].update(contracts=n, done=int(done), opt_from=e0, opt_to=e1)
        finally:
            oc.close()
    order = ["BN", "N50", "FIN", "SEN"]
    return sorted(out.values(), key=lambda d: order.index(d["inst"]) if d["inst"] in order else 9)


# =====================================================================
#  চার্ট বার ও ইন্ডিকেটর
# =====================================================================

class Bars:
    pass


def get_bars(D, tf):
    tf = int(tf)
    if tf in D._bars:
        return D._bars[tf]
    key = D.day * 1000 + (D.mod - OPEN_MIN) // tf
    ch = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    en = np.r_[ch[1:], len(key)]
    B = Bars()
    B.tf = tf
    B.o = D.o[ch]
    B.h = np.maximum.reduceat(D.h, ch)
    B.l = np.minimum.reduceat(D.l, ch)
    B.c = D.c[en - 1]
    B.v = np.add.reduceat(D.v, ch)
    B.t = D.t[ch]
    B.e = B.t + tf
    B.day = D.day[ch]
    B.mod = D.mod[ch]
    B.n = len(ch)
    D._bars[tf] = B
    return B


def shift_arr(a, k):
    k = int(k)
    if k <= 0:
        return a
    out = np.full(len(a), np.nan)
    if k < len(a):
        out[k:] = a[:-k]
    return out


def price_src(X, name):
    if name == "open":
        return X.o
    if name == "high":
        return X.h
    if name == "low":
        return X.l
    if name == "hl2":
        return (X.h + X.l) / 2
    if name == "hlc3":
        return (X.h + X.l + X.c) / 3
    if name == "ohlc4":
        return (X.o + X.h + X.l + X.c) / 4
    return X.c


def wma_calc(x, n):
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    n = int(n)
    if n < 1 or len(x) < n:
        return out
    w = np.arange(1, n + 1, dtype=float)
    win = np.lib.stride_tricks.sliding_window_view(x, n)
    out[n - 1:] = win @ w / w.sum()
    return out


def ma_calc(x, typ, n):
    n = max(int(n), 1)
    s = pd.Series(x)
    if typ == "ema":
        return s.ewm(span=n, adjust=False, min_periods=n).mean().to_numpy()
    if typ == "sma":
        return s.rolling(n).mean().to_numpy()
    if typ == "rma":
        return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()
    if typ == "wma":
        return wma_calc(x, n)
    if typ == "hma":
        raw = 2 * wma_calc(x, max(n // 2, 1)) - wma_calc(x, n)
        return wma_calc(raw, max(int(round(math.sqrt(n))), 1))
    raise ValueError("অজানা MA টাইপ: %s" % typ)


def rma_arr(x, n):
    return np.array(pd.Series(x).ewm(alpha=1.0 / max(int(n), 1), adjust=False, min_periods=int(n)).mean().to_numpy(), dtype=float)


def rsi_calc(x, n):
    s = pd.Series(x)
    d = s.diff()
    up = d.clip(lower=0)
    dn = (-d).clip(lower=0)
    ru = up.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()
    rd = dn.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        r = 100 - 100 / (1 + ru / rd)
    r = np.where((rd == 0) & (ru > 0), 100.0, r)
    r = np.where((rd == 0) & (ru == 0), 50.0, r)
    r[np.isnan(ru) | np.isnan(rd)] = np.nan
    return r


def tr_calc(h, l, c):
    pc = shift_arr(c, 1)
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    tr[0] = h[0] - l[0]
    return tr


def dmi_calc(h, l, c, n, sm):
    up = h - shift_arr(h, 1)
    dn = shift_arr(l, 1) - l
    up = np.nan_to_num(up)
    dn = np.nan_to_num(dn)
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = rma_arr(tr_calc(h, l, c), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        dip = 100 * rma_arr(pdm, n) / atr
        dim = 100 * rma_arr(mdm, n) / atr
        dx = 100 * np.abs(dip - dim) / (dip + dim)
    adx = rma_arr(np.nan_to_num(dx, nan=0.0), sm)
    adx[np.isnan(dip)] = np.nan
    return adx, dip, dim


def supertrend_calc(h, l, c, n, mult):
    atr = rma_arr(tr_calc(h, l, c), n)
    hl2 = (h + l) / 2
    ub = hl2 + mult * atr
    lb = hl2 - mult * atr
    N = len(c)
    st = np.full(N, np.nan)
    di = np.full(N, np.nan)
    fu = np.full(N, np.nan)
    fl = np.full(N, np.nan)
    d = 1
    started = False
    for i in range(N):
        if np.isnan(atr[i]):
            continue
        if not started:
            fu[i], fl[i] = ub[i], lb[i]
            d = 1
            started = True
        else:
            fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
            fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
            if d == 1 and c[i] < fl[i]:
                d = -1
            elif d == -1 and c[i] > fu[i]:
                d = 1
        st[i] = fl[i] if d == 1 else fu[i]
        di[i] = d
    return st, di


def tf_apply(D, B, tf_ind, fn):
    """ইন্ডিকেটর অন্য টাইমফ্রেমে গণনা করে চার্ট বারে বসায় (শুধু সম্পূর্ণ হওয়া HTF বার, রিপেইন্ট নেই)"""
    tf_ind = int(tf_ind or 0)
    if tf_ind <= B.tf:
        return fn(B)
    H = get_bars(D, tf_ind)
    res = fn(H)
    pos = np.searchsorted(H.e, B.e, side="right") - 1
    ok = pos >= 0
    out = {}
    for k, a in res.items():
        m = np.full(B.n, np.nan)
        m[ok] = a[pos[ok]]
        out[k] = m
    return out


SOURCES = [
    ("close", "Close", "দাম"), ("open", "Open", "দাম"), ("high", "High", "দাম"), ("low", "Low", "দাম"),
    ("body", "Candle Body (পয়েন্ট)", "ক্যান্ডেল"), ("bodyp", "Body % (রেঞ্জের)", "ক্যান্ডেল"),
    ("rng", "Candle Range (High-Low)", "ক্যান্ডেল"), ("uw", "Upper Wick (পয়েন্ট)", "ক্যান্ডেল"),
    ("lw", "Lower Wick (পয়েন্ট)", "ক্যান্ডেল"), ("uwp", "Upper Wick % (রেঞ্জের)", "ক্যান্ডেল"),
    ("lwp", "Lower Wick % (রেঞ্জের)", "ক্যান্ডেল"), ("cdir", "Candle রং (+1 সবুজ / -1 লাল)", "ক্যান্ডেল"),
    ("bodyatr", "Body ÷ ATR", "ক্যান্ডেল"),
    ("hl2", "HL2", "দাম"), ("hlc3", "HLC3 (Typical)", "দাম"), ("ohlc4", "OHLC4", "দাম"),
    ("number", "— সংখ্যা (Number) —", "মান"),
    ("volume", "Volume", "ভলিউম"), ("volsma", "Volume SMA", "ভলিউম"),
    ("minsopen", "Minutes Since Open (9:15 থেকে)", "সময়"),
    ("ma1", "MA 1", "ইন্ডিকেটর"), ("ma2", "MA 2", "ইন্ডিকেটর"), ("ma3", "MA 3", "ইন্ডিকেটর"),
    ("rsi", "RSI", "ইন্ডিকেটর"), ("adx", "ADX", "ইন্ডিকেটর"), ("dip", "DI+", "ইন্ডিকেটর"), ("dim", "DI-", "ইন্ডিকেটর"),
    ("atr", "ATR", "ইন্ডিকেটর"),
    ("st1", "Supertrend 1", "ইন্ডিকেটর"), ("st1dir", "Supertrend 1 Dir (+1 উপরে / -1 নিচে)", "ইন্ডিকেটর"),
    ("st2", "Supertrend 2", "ইন্ডিকেটর"), ("st2dir", "Supertrend 2 Dir (+1 / -1)", "ইন্ডিকেটর"),
    ("macd_l", "MACD Line", "ইন্ডিকেটর"), ("macd_s", "MACD Signal", "ইন্ডিকেটর"), ("macd_h", "MACD Histogram", "ইন্ডিকেটর"),
    ("vwap", "VWAP (দিনের শুরু থেকে)", "ইন্ডিকেটর"),
    ("bbup", "BB Upper", "ইন্ডিকেটর"), ("bbmid", "BB Middle", "ইন্ডিকেটর"), ("bblow", "BB Lower", "ইন্ডিকেটর"),
    ("bbw", "BB Width %", "ইন্ডিকেটর"),
    ("donhi", "Highest High (আগের N বার)", "ইন্ডিকেটর"), ("donlo", "Lowest Low (আগের N বার)", "ইন্ডিকেটর"),
    ("orbh", "ORB High", "ORB / দিন"), ("orbl", "ORB Low", "ORB / দিন"), ("orbm", "ORB Mid", "ORB / দিন"),
    ("orbr", "ORB Range (High-Low)", "ORB / দিন"),
    ("dayo", "Day Open", "ORB / দিন"), ("dayh", "Day High (এ পর্যন্ত)", "ORB / দিন"), ("dayl", "Day Low (এ পর্যন্ত)", "ORB / দিন"),
    ("pdh", "Prev Day High", "আগের দিন"), ("pdl", "Prev Day Low", "আগের দিন"), ("pdc", "Prev Day Close", "আগের দিন"),
    ("gap", "Gap % (Day Open vs Prev Close)", "আগের দিন"),
    ("pivp", "Pivot P", "Pivot / CPR"), ("r1", "Pivot R1", "Pivot / CPR"), ("r2", "Pivot R2", "Pivot / CPR"),
    ("r3", "Pivot R3", "Pivot / CPR"), ("s1", "Pivot S1", "Pivot / CPR"), ("s2", "Pivot S2", "Pivot / CPR"),
    ("s3", "Pivot S3", "Pivot / CPR"),
    ("cprt", "CPR Top", "Pivot / CPR"), ("cprb", "CPR Bottom", "Pivot / CPR"), ("cprw", "CPR Width %", "Pivot / CPR"),
]
SRC_IDS = {s[0] for s in SOURCES}
OPS = [("cross_above", "Crosses Above"), ("cross_below", "Crosses Below"), ("above", "Is Above"),
       ("below", "Is Below"), ("rising", "Is Rising"), ("falling", "Is Falling")]


def compute_sources(D, tf, ind):
    key = json.dumps([tf, ind], sort_keys=True)
    if key in D._cache:
        return D._cache[key]
    B = get_bars(D, tf)
    n = B.n
    S = {"close": B.c, "open": B.o, "high": B.h, "low": B.l, "hl2": (B.h + B.l) / 2,
         "hlc3": (B.h + B.l + B.c) / 3, "ohlc4": (B.o + B.h + B.l + B.c) / 4, "volume": B.v,
         "minsopen": (B.mod - OPEN_MIN).astype(float)}
    rng_ = B.h - B.l
    top_, bot_ = np.maximum(B.o, B.c), np.minimum(B.o, B.c)
    safe_ = np.where(rng_ > 0, rng_, np.nan)
    S["body"], S["rng"], S["uw"], S["lw"] = np.abs(B.c - B.o), rng_, B.h - top_, bot_ - B.l
    S["bodyp"] = np.nan_to_num(S["body"] / safe_ * 100)
    S["uwp"] = np.nan_to_num(S["uw"] / safe_ * 100)
    S["lwp"] = np.nan_to_num(S["lw"] / safe_ * 100)
    S["cdir"] = np.sign(B.c - B.o)
    S["volsma"] = pd.Series(B.v).rolling(max(int(ind["vol"]["len"]), 1)).mean().to_numpy()

    for k in ("ma1", "ma2", "ma3"):
        p = ind[k]
        S[k] = tf_apply(D, B, p["tf"], lambda X, p=p: {"v": ma_calc(price_src(X, p["src"]), p["type"], p["len"])})["v"]
    p = ind["rsi"]
    S["rsi"] = tf_apply(D, B, p["tf"], lambda X: {"v": rsi_calc(price_src(X, p["src"]), int(p["len"]))})["v"]
    p = ind["adx"]
    r = tf_apply(D, B, p["tf"], lambda X: dict(zip(("adx", "dip", "dim"), dmi_calc(X.h, X.l, X.c, int(p["len"]), int(p["smooth"])))))
    S["adx"], S["dip"], S["dim"] = r["adx"], r["dip"], r["dim"]
    p = ind["atr"]
    S["atr"] = tf_apply(D, B, p["tf"], lambda X: {"v": rma_arr(tr_calc(X.h, X.l, X.c), int(p["len"]))})["v"]
    with np.errstate(all="ignore"):
        S["bodyatr"] = S["body"] / S["atr"]
    for k in ("st1", "st2"):
        p = ind[k]
        r = tf_apply(D, B, p["tf"], lambda X, p=p: dict(zip(("st", "dir"), supertrend_calc(X.h, X.l, X.c, int(p["len"]), float(p["mult"])))))
        S[k], S[k + "dir"] = r["st"], r["dir"]
    p = ind["macd"]

    def _macd(X):
        c = pd.Series(X.c)
        m = c.ewm(span=int(p["fast"]), adjust=False).mean() - c.ewm(span=int(p["slow"]), adjust=False).mean()
        s = m.ewm(span=int(p["sig"]), adjust=False).mean()
        return {"l": m.to_numpy(), "s": s.to_numpy(), "h": (m - s).to_numpy()}
    r = tf_apply(D, B, p["tf"], _macd)
    S["macd_l"], S["macd_s"], S["macd_h"] = r["l"], r["s"], r["h"]
    p = ind["bb"]

    def _bb(X):
        s = pd.Series(price_src(X, p["src"]))
        mid = s.rolling(int(p["len"])).mean()
        sd = s.rolling(int(p["len"])).std(ddof=0)
        up, lo = mid + float(p["mult"]) * sd, mid - float(p["mult"]) * sd
        with np.errstate(divide="ignore", invalid="ignore"):
            w = ((up - lo) / mid * 100)
        return {"up": up.to_numpy(), "mid": mid.to_numpy(), "low": lo.to_numpy(), "w": w.to_numpy()}
    r = tf_apply(D, B, p["tf"], _bb)
    S["bbup"], S["bbmid"], S["bblow"], S["bbw"] = r["up"], r["mid"], r["low"], r["w"]
    nn = max(int(ind["don"]["n"]), 1)
    S["donhi"] = pd.Series(B.h).rolling(nn).max().shift(1).to_numpy()
    S["donlo"] = pd.Series(B.l).rolling(nn).min().shift(1).to_numpy()

    # VWAP (ইনডেক্সে ভলিউম না থাকলে টাইপিক্যাল প্রাইসের গড়)
    tp = (B.h + B.l + B.c) / 3
    df = pd.DataFrame({"d": B.day, "pv": tp * B.v, "v": B.v, "tp": tp})
    g = df.groupby("d")
    cv, cpv = g["v"].cumsum().to_numpy(), g["pv"].cumsum().to_numpy()
    cnt, ctp = (g.cumcount() + 1).to_numpy(), g["tp"].cumsum().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        S["vwap"] = np.where(cv > 0, cpv / np.where(cv > 0, cv, 1), ctp / cnt)

    # দিনের লেভেল
    S["dayo"] = pd.Series(B.o).groupby(B.day).transform("first").to_numpy()
    S["dayh"] = pd.Series(B.h).groupby(B.day).cummax().to_numpy()
    S["dayl"] = pd.Series(B.l).groupby(B.day).cummin().to_numpy()
    dpos = np.searchsorted(D.du, B.day)
    pH = np.r_[np.nan, D.dH[:-1]][dpos]
    pL = np.r_[np.nan, D.dL[:-1]][dpos]
    pC = np.r_[np.nan, D.dC[:-1]][dpos]
    S["pdh"], S["pdl"], S["pdc"] = pH, pL, pC
    with np.errstate(divide="ignore", invalid="ignore"):
        S["gap"] = (S["dayo"] - pC) / pC * 100
        P = (pH + pL + pC) / 3
        S["pivp"] = P
        S["r1"], S["s1"] = 2 * P - pL, 2 * P - pH
        S["r2"], S["s2"] = P + (pH - pL), P - (pH - pL)
        S["r3"], S["s3"] = pH + 2 * (P - pL), pL - 2 * (pH - P)
        bc = (pH + pL) / 2
        tc = 2 * P - bc
        S["cprt"], S["cprb"] = np.maximum(tc, bc), np.minimum(tc, bc)
        S["cprw"] = (S["cprt"] - S["cprb"]) / P * 100

    # ORB
    mins = int(ind["orb"]["mins"])
    m = D.mod < OPEN_MIN + mins
    if m.any():
        odf = pd.DataFrame({"d": D.day[m], "h": D.h[m], "l": D.l[m]}).groupby("d").agg(h=("h", "max"), l=("l", "min"))
        orh = pd.Series(B.day).map(odf["h"]).to_numpy(float)
        orl = pd.Series(B.day).map(odf["l"]).to_numpy(float)
    else:
        orh = np.full(n, np.nan)
        orl = np.full(n, np.nan)
    valid = B.mod >= OPEN_MIN + mins
    orh = np.where(valid, orh, np.nan)
    orl = np.where(valid, orl, np.nan)
    S["orbh"], S["orbl"], S["orbm"], S["orbr"] = orh, orl, (orh + orl) / 2, orh - orl

    if len(D._cache) >= 6:
        D._cache.pop(next(iter(D._cache)))
    D._cache[key] = (S, B)
    return S, B


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
    for k in ("capital", "day_loss", "day_profit", "brokerage", "slip", "charges_pct"):
        G[k] = float(G[k])
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


# =====================================================================
#  পরিসংখ্যান
# =====================================================================

def calc_stats(trs, capital):
    n = len(trs)
    z = dict(n=0, win=0.0, net=0.0, gross=0.0, costs=0.0, pf=0.0, exp=0.0, avg_w=0.0, avg_l=0.0,
             dd=0.0, dd_pct=0.0, mcl=0, mcw=0, best=0.0, worst=0.0, rr=0.0, rec=0.0, hold=0.0,
             avg_r=None, pm=0.0, nm=0, dd_len=0)
    if n == 0:
        return z
    pn = np.array([t["net"] for t in trs], float)
    wins, loss = pn[pn > 0], pn[pn <= 0]
    lsum = abs(loss.sum())
    pf = (wins.sum() / lsum) if lsum > 0 else (99.0 if wins.sum() > 0 else 0.0)
    eq = np.cumsum(pn)
    peaks = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    ddv = eq - peaks
    dd = float(ddv.min())
    mcl = mcw = cur = cw = 0
    for x in pn:
        cur = cur + 1 if x <= 0 else 0
        cw = cw + 1 if x > 0 else 0
        mcl, mcw = max(mcl, cur), max(mcw, cw)
    dl = best_dl = 0
    for x in ddv:
        dl = dl + 1 if x < 0 else 0
        best_dl = max(best_dl, dl)
    aw = float(wins.mean()) if len(wins) else 0.0
    al = float(loss.mean()) if len(loss) else 0.0
    rs = [t["r"] for t in trs if t.get("r") is not None]
    mon = {}
    for t in trs:
        mon[t["date"][:7]] = mon.get(t["date"][:7], 0.0) + t["net"]
    pm = 100.0 * sum(1 for v in mon.values() if v > 0) / len(mon)
    net = float(pn.sum())
    return dict(n=n, win=float(100 * (pn > 0).mean()), net=net,
                gross=float(sum(t["gross"] for t in trs)), costs=float(sum(t["costs"] for t in trs)),
                pf=float(min(pf, 99.0)), exp=float(pn.mean()), avg_w=aw, avg_l=al,
                dd=dd, dd_pct=float(100 * dd / capital) if capital else 0.0, mcl=int(mcl), mcw=int(mcw),
                best=float(pn.max()), worst=float(pn.min()),
                rr=float(aw / abs(al)) if al < 0 else 0.0, rec=float(net / abs(dd)) if dd < 0 else 0.0,
                hold=float(np.mean([t.get("hold", 0) for t in trs])),
                avg_r=float(np.mean(rs)) if rs else None, pm=float(pm), nm=len(mon), dd_len=int(best_dl))


def _pctl(a, q):
    return float(np.percentile(a, q)) if len(a) else 0.0


DOWN = ["সোম", "মঙ্গল", "বুধ", "বৃহ", "শুক্র", "শনি", "রবি"]


def analyze(trades, cap, base):
    """ট্রেড তালিকা থেকে বিস্তারিত বিশ্লেষণ: টাইমিং, DD, RR, MAE/MFE, SL/TP what-if"""
    if not trades:
        return {}
    out = {}

    def grp(keyf, order=None):
        g = {}
        for t in trades:
            g.setdefault(keyf(t), []).append(t)
        ks = order if order else sorted(g)
        return [dict(k=k, **calc_stats(g[k], cap)) for k in ks if k in g]

    def mins(t):
        return int(t["in"][:2]) * 60 + int(t["in"][3:5])

    def slotk(t):
        m = mins(t)
        b = (m - OPEN_MIN) // 30 * 30 + OPEN_MIN
        return "%s-%s" % (hm_str(b), hm_str(b + 30))

    def bucket(v, edges, labels):
        for e, l in zip(edges, labels):
            if v <= e:
                return l
        return labels[-1]

    out["by_slot"] = grp(slotk)
    out["by_dow"] = grp(lambda t: t["dow"], DOWN)
    out["by_reason"] = grp(lambda t: t["reason"])
    out["by_case"] = grp(lambda t: t["case"])
    out["by_type"] = grp(lambda t: t["side"].split()[1])
    dl = ["০ দিন (এক্সপায়ারি দিন)", "১ দিন", "২ দিন", "৩-৫ দিন", "৬-১০ দিন", "১১-২০ দিন", "২১+ দিন"]
    out["by_dte"] = grp(lambda t: bucket(t["dte"], [0, 1, 2, 5, 10, 20], dl), dl)
    hl = ["≤৯ মিনিট", "১০-১৯ মিনিট", "২০-৩৯ মিনিট", "৪০-৫৯ মিনিট", "১-২ ঘণ্টা", "২ ঘণ্টার বেশি"]
    out["by_hold"] = grp(lambda t: bucket(t["hold"], [9, 19, 39, 59, 120], hl), hl)
    pl = ["<১০০", "১০০-২৫০", "২৫০-৪০০", "৪০০-৭০০", "৭০০-১০০০", "১০০০+"]
    out["by_prem"] = grp(lambda t: bucket(t["entry"], [100, 250, 400, 700, 1000], pl), pl)

    # ড্রডাউন
    pn = np.array([t["net"] for t in trades], float)
    eq = np.cumsum(pn)
    pk = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    dd = eq - pk
    k = int(np.argmin(dd))
    pi = int(np.argmax(eq[:k + 1] == pk[k])) if dd[k] < 0 else 0
    rec = None
    for j in range(k + 1, len(eq)):
        if eq[j] >= pk[k]:
            rec = j
            break
    out["dd"] = [round(float(x), 1) for x in dd]
    out["dd_info"] = {"max": float(dd[k]), "pct": float(100 * dd[k] / cap) if cap else 0.0,
                      "peak_date": trades[pi]["date"] if dd[k] < 0 else None, "trough_date": trades[k]["date"],
                      "rec_date": trades[rec]["date"] if rec is not None else None,
                      "trades_in_dd": int(k - pi), "recovered": rec is not None}
    # দিন অনুযায়ী
    dm = {}
    for t in trades:
        dm[t["date"]] = dm.get(t["date"], 0.0) + t["net"]
    dv = [dm[d] for d in sorted(dm)]
    cl = mx = 0
    for x in dv:
        cl = cl + 1 if x <= 0 else 0
        mx = max(mx, cl)
    out["days"] = {"n": len(dv), "win": float(100 * np.mean([x > 0 for x in dv])), "best": float(max(dv)),
                   "worst": float(min(dv)), "avg": float(np.mean(dv)), "mcl": int(mx),
                   "tpd": float(len(trades) / len(dv))}
    # MAE / MFE
    mae = np.array([t["mae"] for t in trades], float)
    mfe = np.array([t["mfe"] for t in trades], float)
    ret = np.array([t["ret"] for t in trades], float)
    w = pn > 0
    rr_ = np.array([t.get("rret", t["ret"]) for t in trades], float)    # স্লিপেজ ছাড়া, MFE-র সাথে একই ভিত্তি
    gb = (mfe - rr_)[w]
    out["exc"] = {
        "mae_w": [_pctl(mae[w], q) for q in (50, 75, 90, 100)],
        "mae_l": [_pctl(mae[~w], q) for q in (50, 75, 90, 100)],
        "mfe_w": [_pctl(mfe[w], q) for q in (25, 50, 75, 90)],
        "mfe_l": [_pctl(mfe[~w], q) for q in (25, 50, 75, 90)],
        "give_med": _pctl(gb, 50), "give_avg": float(gb.mean()) if len(gb) else 0.0,
        "l_touch": {str(x): float(100 * np.mean(mfe[~w] >= x)) if (~w).any() else 0.0 for x in (5, 10, 20, 30)},
        "nw": int(w.sum()), "nl": int((~w).sum())}
    return out


# =====================================================================
#  ব্যাকটেস্ট ইঞ্জিন
# =====================================================================


def contract_view(D, ct):
    """একটা অপশন কন্ট্র্যাক্টের নিজের প্রিমিয়াম চার্টকে স্পটের মতো ডেটা-অবজেক্ট বানায়"""
    t, o, h, l, c = ct
    mod = t % 1440
    m = (mod >= OPEN_MIN) & (mod <= OPEN_MIN + 374)
    V = Data()
    V._bars, V._cache = {}, {}
    V.t, V.o, V.h, V.l, V.c = t[m], o[m], h[m], l[m], c[m]
    V.v = np.zeros(len(V.t))
    V.day, V.mod = V.t // 1440, V.t % 1440
    if len(V.t) == 0:
        return V
    V.du = np.unique(V.day)
    st = np.flatnonzero(np.r_[True, V.day[1:] != V.day[:-1]])
    en = np.r_[st[1:], len(V.day)]
    V.dO, V.dH = V.o[st], np.maximum.reduceat(V.h, st)
    V.dL, V.dC = np.minimum.reduceat(V.l, st), V.c[en - 1]
    return V


def prem_signals(D, st, exset):
    """প্রতিটা কন্ট্র্যাক্টের প্রিমিয়াম চার্টে সিগন্যাল/এক্সিট সময় বের করে (ছোট অ্যারে হিসেবে ক্যাশ হয়)"""
    G = st["general"]
    ck = json.dumps([G["tf"], G["fresh"], G["win_from"], G["win_to"], st["ind"],
                     [[c["opt"], c["on"], c["entry"], c["exit"]] for c in st["cases"]]], sort_keys=True)
    pc = D.__dict__.setdefault("_pc", {})
    store = pc.setdefault(ck, {})
    if len(pc) > 3:
        pc.pop(next(iter(pc)))
    w0, w1 = hm(G["win_from"]), hm(G["win_to"])
    for (ex, typ, stk), ct in D.opt.items():
        if ex not in exset or (ex, typ, stk) in store:
            continue
        res = {}
        V = contract_view(D, ct)
        if len(V.t) > 5:
            S, B = compute_sources(V, G["tf"], st["ind"])
            emod = B.e % 1440
            wm = (emod >= w0) & (emod <= w1)
            first = np.r_[True, B.day[1:] != B.day[:-1]]
            for ci, cs in enumerate(st["cases"]):
                if not cs["on"] or cs["opt"] != typ:
                    continue
                ent = eval_cond(S, cs["entry"], B.n)
                if ent is None:
                    continue
                if G["fresh"]:
                    prev = np.r_[False, ent[:-1]]
                    prev[first] = False
                    ent = ent & ~prev
                ent = ent & wm
                xx = eval_cond(S, cs["exit"], B.n)
                res[ci] = (B.e[ent], B.c[ent], B.e[xx] if xx is not None else np.zeros(0, "int64"))
        V._cache.clear()
        store[(ex, typ, stk)] = res
    return store


SRC_CACHE = {}
ADV_CACHE = {}


def backtest(D, raw, light=False, progress=None, now=None):
    st = norm_settings(raw)
    G = st["general"]
    PM = G.get("chart") == "prem"
    ck = (G["tf"], json.dumps(st["ind"], sort_keys=True))
    if SRC_CACHE.get("D") is D and SRC_CACHE.get("k") == ck:
        S, B = SRC_CACHE["v"]
    else:
        S, B = compute_sources(D, G["tf"], st["ind"])
        SRC_CACHE.update(D=D, k=ck, v=(S, B), cond={})
    n = B.n
    w0, w1, sq = hm(G["win_from"]), hm(G["win_to"]), hm(G["sqoff"])
    emod = B.e % 1440
    wmask = (emod >= w0) & (emod <= w1)
    first_of_day = np.r_[True, B.day[1:] != B.day[:-1]]

    cases = []
    for ci, cs in enumerate(st["cases"]):
        if not cs["on"] or PM:
            continue
        cc = json.dumps([cs["entry"], cs["exit"]], sort_keys=True)
        hit = SRC_CACHE["cond"].get(cc)
        if hit is None:
            hit = (eval_cond(S, cs["entry"], n), eval_cond(S, cs["exit"], n))
            if len(SRC_CACHE["cond"]) > 6:
                SRC_CACHE["cond"].clear()
            SRC_CACHE["cond"][cc] = hit
        ent = hit[0]
        if ent is None:
            continue
        if G["fresh"]:
            prev = np.r_[False, ent[:-1]]
            prev[first_of_day] = False
            sig = ent & ~prev
        else:
            sig = ent
        ex = hit[1]
        xs = set(B.e[ex].tolist()) if ex is not None else set()
        cases.append((ci, cs, sig & wmask, xs))

    d_from = day_int(G["from"]) if G["from"] else -10 ** 9
    d_to = day_int(G["to"]) if G["to"] else 10 ** 9
    exps = D.expiries
    eligible = []
    for d in D.du.tolist():
        if d < d_from or d > d_to:
            continue
        k = bisect.bisect_left(exps, d + G["min_dte"])
        if k >= len(exps):
            continue
        ex = exps[k]
        if G["skip_expiry_day"] and ex == d:
            continue
        lo, hi = D.exp_range[ex]
        if lo <= d * 1440 + 1439 and hi >= d * 1440:
            eligible.append((d, ex))

    PS = prem_signals(D, st, {ex for _, ex in eligible}) if PM else None
    pcases = [(ci, cs) for ci, cs in enumerate(st["cases"]) if cs["on"]] if PM else []
    skipped = {"ডেটা নেই (কন্ট্র্যাক্টে বার নেই)": 0, "স্ট্রাইক পাওয়া যায়নি": 0}
    trades = []
    slip = G["slip"]
    sl_first = bool(G["sl_first"])

    def locate(ex, typ, stk, te):
        ct = D.opt.get((ex, typ, float(stk)))
        if ct is None:
            return None
        j = int(np.searchsorted(ct[0], te))
        if j < len(ct[0]) and ct[0][j] == te:
            return ct, j
        return None

    def pick(cs, ex, te, spot_ref):
        typ = cs["opt"]
        stk = D.strikes.get((ex, typ))
        if stk is None:
            return None
        sm = cs["strike"]
        if sm["mode"] == "prem":
            X = float(sm["prem"])
            best = None
            for s_ in stk:
                r = locate(ex, typ, s_, te)
                if r is None:
                    continue
                pr = r[0][1][r[1]]
                if pr <= 0:
                    continue
                diff = abs(pr - X)
                if best is None or diff < best[0]:
                    best = (diff, r[0], r[1], float(s_))
            return None if best is None else (best[1], best[2], best[3])
        atm = int(np.argmin(np.abs(stk - spot_ref)))
        off = int(float(sm["off"]))
        k = atm + (off if typ == "CE" else -off)
        if k < 0 or k >= len(stk):
            return "nostrike"
        s_ = float(stk[k])
        r = locate(ex, typ, s_, te)
        if r is None:
            return None
        return (r[0], r[1], s_)

    def run_trade(cs, xs, ct, j, te, day_end, lot):
        t, o, h, l, c = ct
        side = 1 if cs["action"] == "BUY" else -1
        raw_e = float(o[j])
        sl = tp = None
        sm, sv = cs["sl"]["mode"], float(cs["sl"]["val"])
        if sm == "pct":
            sl = raw_e * (1 - side * sv / 100)
        elif sm == "pts":
            sl = raw_e - side * sv
        if sl is not None and side == 1 and sl < 0.05:
            sl = 0.05              # লং-এ SL ঋণাত্মক/শূন্য প্রিমিয়াম হতে পারে না
        sl0 = sl
        tm, tv = cs["tp"]["mode"], float(cs["tp"]["val"])
        if tm == "pct":
            tp = raw_e * (1 + side * tv / 100)
        elif tm == "pts":
            tp = raw_e + side * tv
        elif tm == "rr" and sl is not None:
            tp = raw_e + side * tv * abs(raw_e - sl)
        # ---- আংশিক প্রফিট বুকিং (T1/T2) — লট পূর্ণসংখ্যায়, অন্তত ১ লট রানারের জন্য থাকে ----
        L = G["lots"]
        tg = cs.get("tg", {})
        books = []
        if tg.get("on") and L >= 2:
            unit = tg.get("unit", "pct")

            def tpx(v):
                if unit == "pct":
                    return raw_e * (1 + side * v / 100)
                if unit == "pts":
                    return raw_e + side * v
                if unit == "rr" and sl0 is not None:
                    return raw_e + side * v * abs(raw_e - sl0)
                return None
            spec = [("T1", float(tg["t1"]["val"]), float(tg["t1"]["qty"]))]
            if tg["t2"].get("on"):
                spec.append(("T2", float(tg["t2"]["val"]), float(tg["t2"]["qty"])))
            left = L - 1
            for tag, v, q in spec:
                if left <= 0:
                    break
                p = tpx(v)
                if p is None or p <= 0:
                    continue
                if books and (p - books[-1][0]) * side <= 0:
                    continue
                if tp is not None and (p - tp) * side >= 0:
                    continue
                n = max(1, min(int(L * q / 100.0 + 0.4999), left))     # ৩ লটের ৫০% = ১ লট
                books.append([p, n, tag])
                left -= n
        a1, a2 = tg.get("a1", {}), tg.get("a2", {})
        tr = cs["trail"]
        tmode = tr.get("mode", "none")
        trail_v = float(tr.get("val", 15))
        trail_act = float(tr.get("act", 0) or 0)
        t_pts = tr.get("unit", "pct") == "pts"
        t_trig = float(tr.get("trig", 10) or 0)
        t_move = float(tr.get("move", 5) or 0)
        trig_pts = t_trig if t_pts else raw_e * t_trig / 100.0
        move_pts = t_move if t_pts else raw_e * t_move / 100.0
        be_on = bool(cs["be"]["on"])
        be_v = float(cs["be"]["val"])
        tstop = int(float(cs.get("time_stop", 0) or 0))
        peak = raw_e
        mfe = mae = 0.0
        nb = len(t)
        jj = j
        rem = L
        bk = 0
        legs = []
        reason = px = None
        exit_idx = None

        def tighten(cur, new):
            if cur is None:
                return new
            return max(cur, new) if side == 1 else min(cur, new)
        while jj < nb and t[jj] < day_end and (now is None or t[jj] <= now):
            s = int(t[jj])
            oo, hh, ll = float(o[jj]), float(h[jj]), float(l[jj])
            if now is not None and s == now:
                hh = ll = oo       # চলতি মিনিটের শুধু open জানা (লাইভে হাই/লো এখনো গঠনাধীন)
            full = None
            if jj > j:
                if s % 1440 >= sq:
                    full = ("Square-off", oo)
                elif s in xs:
                    full = ("Signal Exit", oo)
                elif tstop and s - te >= tstop:
                    full = ("Time Stop", oo)
            hits = []
            if full is None:
                if side == 1:
                    sl_hit = sl is not None and ll <= sl
                    tp_hit = tp is not None and hh >= tp
                    gap_sl = jj > j and sl is not None and oo <= sl
                    gap_tp = jj > j and tp is not None and oo >= tp
                else:
                    sl_hit = sl is not None and hh >= sl
                    tp_hit = tp is not None and ll <= tp
                    gap_sl = jj > j and sl is not None and oo >= sl
                    gap_tp = jj > j and tp is not None and oo <= tp
                kk = bk
                while kk < len(books):
                    p = books[kk][0]
                    g = jj > j and ((oo >= p) if side == 1 else (oo <= p))
                    hit = (hh >= p) if side == 1 else (ll <= p)
                    if hit or g:
                        hits.append((kk, oo if g else p))
                        kk += 1
                    else:
                        break
                if gap_sl:
                    full, hits = ("SL (gap)", oo), []
                elif gap_tp:
                    full, hits = ("Target (gap)", oo), []
                elif sl_hit and tp_hit:
                    full = ("SL", sl) if sl_first else ("Target", tp)
                    if sl_first:
                        hits = []
                elif sl_hit:
                    full = ("SL", sl)
                    if sl_first:
                        hits = []
                elif tp_hit:
                    full = ("Target", tp)
            # MAE/MFE: বারের ভেতরের ক্রম অজানা, তাই ইঞ্জিনের নিজের ধারণা (sl_first) মেনেই হিসাব।
            # ট্রেড বন্ধের পরের দাম ধরা হয় না; এক্সিট বারের খোলার দাম ধরা হয়।
            f_full, a_full = (hh, ll) if side == 1 else (ll, hh)
            if full is None:
                fav, adv = f_full, a_full
            elif full[0] == "SL":
                fav, adv = (oo if sl_first else f_full), sl
            elif full[0] == "Target":
                fav = tp
                adv = oo if (sl is not None and ((ll <= sl) if side == 1 else (hh >= sl))) else a_full
            else:
                fav = adv = oo
            mfe = max(mfe, (fav - raw_e) * side)
            mae = max(mae, (raw_e - adv) * side)
            for kk, fp in hits:
                legs.append((books[kk][1], fp, s, books[kk][2]))
                rem -= books[kk][1]
                bk = kk + 1
            if full is not None:
                legs.append((rem, full[1], s, full[0]))
                reason, px = full
                exit_idx = jj
                break
            # টার্গেট লাগার পর SL সরানো (পরের বার থেকে কার্যকর)
            for kk, _ in hits:
                tag = books[kk][2]
                if tag == "T1":
                    m1 = a1.get("mode", "be")
                    if m1 == "be":
                        sl = tighten(sl, raw_e)
                    elif m1 == "lock":
                        sl = tighten(sl, raw_e * (1 + side * float(a1.get("val", 5) or 0) / 100.0))
                elif tag == "T2":
                    m2 = a2.get("mode", "t1")
                    if m2 == "t1" and books:
                        sl = tighten(sl, books[0][0])
                    elif m2 == "be":
                        sl = tighten(sl, raw_e)
            # বারের শেষে ট্রেইলিং / ব্রেকইভেন আপডেট
            peak = max(peak, hh) if side == 1 else min(peak, ll)
            if be_on and (peak - raw_e) * side / raw_e * 100 >= be_v:
                sl = tighten(sl, raw_e)
            fav = (peak - raw_e) * side
            lvl = None
            if tmode == "pct" and fav / raw_e * 100 >= trail_act:
                lvl = peak * (1 - side * trail_v / 100)
            elif tmode == "step" and trig_pts > 0:
                k = int(fav // trig_pts)
                if k >= 1:
                    lvl = (sl0 if sl0 is not None else raw_e) + side * k * move_pts
            if lvl is not None:
                sl = tighten(sl, lvl)
            jj += 1
        is_open = False
        if exit_idx is None:
            exit_idx = jj - 1
            if now is not None and (now % 1440) < sq:
                is_open = True       # ট্রেড এখনো চলছে (পেপার/লাইভ) — চলতি দামে MTM
                reason = "OPEN"
                px = float(o[exit_idx]) if int(t[exit_idx]) == now else float(c[exit_idx])
            else:
                reason, px = "Day End", float(c[exit_idx])
            legs.append((rem, px, int(t[exit_idx]), reason))
        entry_eff = raw_e + side * slip
        qty = L * lot
        gross = 0.0
        costs = G["brokerage"]
        sell_turn = 0.0
        wsum = 0.0
        ltxt = []
        raw_w = 0.0
        for (q_l, p_l, t_l, why) in legs:
            raw_w += p_l * q_l
            q_ = q_l * lot
            ex_eff = p_l - side * slip
            gross += (ex_eff - entry_eff) * side * q_
            costs += G["brokerage"]
            sell_turn += (ex_eff if side == 1 else entry_eff) * q_
            wsum += ex_eff * q_
            ltxt.append(dict(q=int(q_l), px=round(ex_eff, 2), t=hm_str(t_l), why=why))
        costs += G["charges_pct"] / 100.0 * sell_turn
        exit_eff = (legs[0][1] - side * slip) if len(legs) == 1 else (wsum / qty if qty else 0.0)
        return dict(exit_t=int(t[exit_idx]), exit_px=float(px), entry_eff=entry_eff, exit_eff=exit_eff,
                    reason=reason, qty=qty, gross=float(gross), costs=float(costs), net=float(gross - costs), is_open=is_open, rem=int(rem), sl_now=(float(sl) if sl is not None else None), tp_now=(float(tp) if tp is not None else None),
                    bars=int(exit_idx - j + 1), mae=float(mae / raw_e * 100) if raw_e else 0.0,
                    mfe=float(mfe / raw_e * 100) if raw_e else 0.0,
                    rret=float((raw_w / L - raw_e) * side / raw_e * 100) if raw_e and L else 0.0, legs=ltxt if len(ltxt) > 1 else None,
                    risk=float(abs(raw_e - sl0) * qty) if sl0 is not None else None,
                    ret=float((exit_eff - entry_eff) * side / entry_eff * 100) if entry_eff else 0.0)

    total = max(len(eligible), 1)
    for di, (d, ex) in enumerate(eligible):
        if progress and di % 15 == 0:
            progress(di / total)
        i0 = int(np.searchsorted(B.day, d, "left"))
        i1 = int(np.searchsorted(B.day, d, "right"))
        if i1 <= i0 or not (cases or pcases):
            continue
        cand = [np.flatnonzero(sg[i0:i1]) + i0 for (_, _, sg, _) in cases]
        evs = []
        if PM:
            dpi = int(np.searchsorted(D.du, d))
            for ci, cs in pcases:
                typ, sm = cs["opt"], cs["strike"]
                stks = D.strikes.get((ex, typ))
                lst = []
                if stks is not None and len(stks):
                    if sm["mode"] == "prem":
                        X, bd = float(sm["prem"]), float(sm.get("band", 30)) / 100.0
                        pool = [float(x) for x in stks]
                    else:
                        atm = int(np.argmin(np.abs(stks - D.dO[dpi])))
                        kk = atm + (int(float(sm["off"])) if typ == "CE" else -int(float(sm["off"])))
                        pool = [float(stks[kk])] if 0 <= kk < len(stks) else []
                        X, bd = None, None
                    for sk in pool:
                        rr = PS.get((ex, typ, sk), {}).get(ci)
                        if rr is None:
                            continue
                        a = np.flatnonzero((rr[0] >= d * 1440) & (rr[0] < (d + 1) * 1440))
                        for q in a:
                            pr = float(rr[1][q])
                            if X is not None and (pr <= 0 or abs(pr - X) > X * bd):
                                continue
                            lst.append((int(rr[0][q]), abs(pr - X) if X is not None else 0.0, sk, ci, cs))
                lst.sort(key=lambda e: (e[0], e[1]))
                evs.append(lst)
        cursor = -1
        ntr = 0
        dpnl = 0.0
        day_end = (d + 1) * 1440
        lot = D.exp_lot.get(ex, 1)
        while ntr < G["max_trades"]:
            if PM:
                bestp = None
                for lst in evs:
                    for e in lst:
                        if e[0] > cursor:
                            if bestp is None or e[0] < bestp[0]:
                                bestp = e
                            break
                if bestp is None:
                    break
                te, _, sk, ci, cs = bestp
                r0 = locate(ex, cs["opt"], sk, te)
                if r0 is None:
                    skipped["ডেটা নেই (কন্ট্র্যাক্টে বার নেই)"] += 1
                    cursor = te
                    continue
                ct, j = r0
                if ct[1][j] <= 0:
                    cursor = te
                    continue
                xs = set(PS[(ex, cs["opt"], sk)][ci][2].tolist())
                sp_i = max(int(np.searchsorted(D.t, te)) - 1, 0)
                spot_px = float(D.c[sp_i])
                stk = sk
            else:
                best = None
                for k in range(len(cases)):
                    idxs = cand[k]
                    if len(idxs) == 0:
                        continue
                    pos = int(np.searchsorted(B.e[idxs], cursor, side="right"))
                    if pos < len(idxs):
                        ii = int(idxs[pos])
                        if best is None or B.e[ii] < B.e[best[0]]:
                            best = (ii, k)
                if best is None:
                    break
                ii, k = best
                ci, cs, sg, xs = cases[k]
                te = int(B.e[ii])
                if now is not None and te > now:
                    break
                pk = pick(cs, ex, te, float(B.c[ii]))
                if pk is None or pk == "nostrike":
                    skipped["ডেটা নেই (কন্ট্র্যাক্টে বার নেই)" if pk is None else "স্ট্রাইক পাওয়া যায়নি"] += 1
                    cursor = te
                    continue
                ct, j, stk = pk
                if ct[1][j] <= 0:
                    cursor = te
                    continue
                spot_px = float(B.c[ii])
            r = run_trade(cs, xs, ct, j, te, day_end, lot)
            trades.append({
                "date": day_str(d), "case": cs["name"], "side": "%s %s %d" % (cs["action"], cs["opt"], stk),
                "expiry": day_str(ex), "in": hm_str(te), "out": hm_str(r["exit_t"]),
                "entry": round(r["entry_eff"], 2), "exit": round(r["exit_eff"], 2),
                "reason": r["reason"], "lots": G["lots"], "qty": r["qty"],
                "gross": round(r["gross"], 2), "costs": round(r["costs"], 2), "net": round(r["net"], 2),
                "ret": round(r["ret"], 2), "spot": round(spot_px, 2), "bars": r["bars"],
                "hold": int(r["exit_t"] - te), "ib": int(ii), "mae": round(r["mae"], 2), "mfe": round(r["mfe"], 2),
                "rret": round(r["rret"], 2),
                "r": round(r["net"] / r["risk"], 2) if r["risk"] else None,
                "dte": int(ex - d), "dow": DOWN[(d + 3) % 7], "legs": r["legs"],
                "open": bool(r["is_open"]), "rem": r["rem"], "slx": r.get("sl_now"), "tpx": r.get("tp_now")})
            ntr += 1
            dpnl += r["net"]
            cursor = r["exit_t"]
            if (G["day_loss"] > 0 and dpnl <= -G["day_loss"]) or (G["day_profit"] > 0 and dpnl >= G["day_profit"]):
                break

    days_list = [day_str(d) for d, _ in eligible]
    cut = int(len(days_list) * G["split"] / 100.0)
    split_date = days_list[cut] if 0 < cut < len(days_list) else None
    tr_tr = [t for t in trades if split_date and t["date"] < split_date] if split_date else trades
    tr_te = [t for t in trades if split_date and t["date"] >= split_date] if split_date else []
    cap = G["capital"]
    out = {"stats": {"all": calc_stats(trades, cap), "train": calc_stats(tr_tr, cap), "test": calc_stats(tr_te, cap)},
           "meta": {"days": len(days_list), "from": days_list[0] if days_list else None,
                    "to": days_list[-1] if days_list else None, "split_date": split_date,
                    "skipped": skipped, "cases_active": len(cases),
                    "warn": (["আংশিক প্রফিট বুকিং চালু, কিন্তু লট ১ — ভাগ করা যায় না। লট কমপক্ষে ২ দিন।"]
                             if G["lots"] < 2 and any(c["on"] and c["tg"]["on"] for c in st["cases"]) else [])}}
    if light == "trades":
        return {"trades": trades, "meta": out["meta"]}
    if light:
        return out
    out["trades"] = trades
    out["an"] = analyze(trades, cap, BASE_TF)
    eq, cum = [], 0.0
    for t in trades:
        cum += t["net"]
        eq.append(round(cum, 2))
    out["equity"] = eq
    yr, mo = {}, {}
    for t in trades:
        yr.setdefault(t["date"][:4], []).append(t)
        mo.setdefault(t["date"][:7], []).append(t)
    out["yearly"] = [dict(k=k, **calc_stats(v, cap)) for k, v in sorted(yr.items())]
    out["monthly"] = [dict(k=k, **calc_stats(v, cap)) for k, v in sorted(mo.items())]
    return out


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
        for k, words in P.items():
            if any(w in c for w in words):
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



# =====================================================================
#  Advisor v2 — রুল অ্যাব্লেশন, ফিল্টার মাইনিং, স্মার্ট সার্চ, রোলিং Walk-Forward
# =====================================================================
def _seg_edges(dates, k):
    """তারিখ-তালিকা (সাজানো, ইউনিক) → k টি প্রায় সমান ক্যালেন্ডার-ভাগের সীমা"""
    if not dates:
        return []
    n = len(dates)
    return [dates[min(n - 1, (n * i) // k)] for i in range(k)] + ["9999-12-31"]


def _slice(trades, lo, hi):
    return [t for t in trades if lo <= t["date"] < hi]


def _sc_basic(trs, cap, min_n=1):
    if len(trs) < min_n:
        return None
    st = calc_stats(trs, cap)
    return float(st["net"] / (abs(st["dd"]) + 0.02 * cap)), st


def advise2(D, raw, opts, progress=None):
    t0 = time.time()
    base = norm_settings(raw)
    G = base["general"]
    cap = G["capital"]
    on_cases = [i for i, c in enumerate(base["cases"]) if c["on"]]
    if not on_cases:
        raise ValueError("কোনো Case চালু নেই")
    ci = int((opts or {}).get("case", on_cases[0]))
    if ci not in on_cases:
        ci = on_cases[0]
    for i, c in enumerate(base["cases"]):
        c["on"] = (i == ci)               # শুধু নির্বাচিত Case বিশ্লেষণ
    if G["split"] <= 0:
        G["split"] = 70
    cp = "cases.%d." % ci
    runs = [0]
    if ADV_CACHE.get("D") is not D:
        ADV_CACHE.clear()
        ADV_CACHE["D"] = D
    cache = ADV_CACHE.setdefault("t", {})
    if len(cache) > 1500:
        cache.clear()
    est = 260

    def run(s):
        k = _ck(s)
        if k not in cache:
            cache[k] = backtest(D, s, light="trades")
            runs[0] += 1
            if progress:
                progress(min(0.96, runs[0] / est))
        return cache[k]

    b = run(base)
    bt = b["trades"]
    if len(bt) < 25:
        raise ValueError("মোট ট্রেড মাত্র %d — v2 বিশ্লেষণের জন্য অন্তত ৫০+ ট্রেড দরকার। তারিখ-সীমা বাড়ান।" % len(bt))
    split_date = b["meta"].get("split_date")
    dates = sorted({t["date"] for t in bt})
    K = int(min(8, max(3, len(bt) // 25)))
    edges = _seg_edges(sorted({day_str(d) for d in D.du.tolist() if day_str(d) >= (b["meta"].get("from") or "")
                               and day_str(d) <= (b["meta"].get("to") or "9999")}), K)
    min_split = lambda trs: (_slice(trs, "0000", split_date), _slice(trs, split_date, "9999")) if split_date else (trs, [])

    def ssc(trs):
        r = _sc_basic(trs, cap)
        return (r[0] if r else None), (r[1] if r else calc_stats([], cap))

    def seg_pos(trs):
        pos = 0
        for i in range(K):
            sl_ = _slice(trs, edges[i], edges[i + 1])
            if sl_ and sum(t["net"] for t in sl_) > 0:
                pos += 1
        return pos / float(K)

    def robust(trs):
        """पूरा-সময়ের স্কোর × (০.৫ + ০.৫ × কতগুলো সময়-ভাগে লাভ)"""
        r = _sc_basic(trs, cap, 10)
        if r is None:
            return None
        return r[0] * (0.5 + 0.5 * seg_pos(trs))

    base_rob = robust(bt)
    out = {"case": base["cases"][ci]["name"], "case_idx": ci, "K": K, "n_base": len(bt), "split_date": split_date}

    # ---------------- ১) Rule Ablation ----------------
    abl = []
    ents = base["cases"][ci]["entry"]
    live_idx = [i for i, sl_ in enumerate(ents) if sl_.get("on", True)]
    if len(live_idx) > 1:
        for i in live_idx:
            s = copy.deepcopy(base)
            s["cases"][ci]["entry"][i]["on"] = False
            r = run(s)["trades"]
            tr_a, te_a = min_split(r)
            tr_b, te_b = min_split(bt)
            sa, sta = ssc(tr_a); sb_, stb = ssc(tr_b)
            ta, stte = ssc(te_a); tb, stteb = ssc(te_b)
            l, op, rr_ = ents[i]["l"], ents[i]["op"], ents[i]["r"]
            abl.append({"idx": i, "rule": "%s %s %s%s" % (l, op, rr_, (" " + str(ents[i].get("num"))) if rr_ == "number" else ""),
                        "n": len(r), "d_net": float(sum(t["net"] for t in r) - sum(t["net"] for t in bt)),
                        "d_train": float(sta["net"] - stb["net"]), "d_test": float(stte["net"] - stteb["net"]),
                        "pf": float(calc_stats(r, cap)["pf"]), "dd": float(calc_stats(r, cap)["dd"]),
                        "dd_base": float(calc_stats(bt, cap)["dd"]), "rob": robust(r)})
    out["ablation"] = abl

    # ---------------- ২) Filter Mining ----------------
    filt = []
    try:
        Sx = SRC_CACHE["v"][0] if SRC_CACHE.get("D") is D else None
        if Sx is None or SRC_CACHE.get("k") is None:
            backtest(D, base, light="trades")
            Sx = SRC_CACHE["v"][0]
    except Exception:
        Sx = None
    mined = []
    if Sx is not None and len(bt) >= 40:
        tr_b, te_b = min_split(bt)
        def val(name, t):
            a = Sx.get(name)
            if a is None:
                return None
            v = a[t["ib"]]
            return None if v is None or not np.isfinite(v) else float(v)
        feats = ["adx", "rsi", "bodyatr", "cprw", "gap", "bbw", "minsopen", "atr"]
        cands = []
        for f in feats:
            vals = np.array([val(f, t) if val(f, t) is not None else np.nan for t in tr_b], float)
            ok = np.isfinite(vals)
            if ok.sum() < 30:
                continue
            for q in (20, 30, 40, 50, 60, 70, 80):
                thr = float(np.round(np.percentile(vals[ok], q), 2))
                for op in ("above", "below"):
                    keep = ok & ((vals > thr) if op == "above" else (vals < thr))
                    if keep.sum() < max(20, 0.4 * len(tr_b)):
                        continue
                    kt = [t for t, k in zip(tr_b, keep) if k]
                    r_ = _sc_basic(kt, cap, 20)
                    if r_:
                        cands.append((r_[0], {"l": f, "op": op, "r": "number", "num": thr}))
        # তুলনামূলক: ভলিউম, DI, VWAP
        for desc, l_, op_, r_, mul in (("volume > volsma", "volume", "above", "volsma", 1.0), ("volume > 1.5×volsma", "volume", "above", "volsma", 1.5),
                                       ("DI+ > DI-", "dip", "above", "dim", 1.0), ("DI- > DI+", "dim", "above", "dip", 1.0),
                                       ("close > vwap", "close", "above", "vwap", 1.0), ("close < vwap", "close", "below", "vwap", 1.0)):
            keep, kt = [], []
            for t in tr_b:
                a, c_ = val(l_, t), val(r_, t)
                if a is None or c_ is None:
                    continue
                if (a > c_ * mul) if op_ == "above" else (a < c_ * mul):
                    kt.append(t)
            if len(kt) >= max(20, 0.4 * len(tr_b)):
                r2 = _sc_basic(kt, cap, 20)
                if r2:
                    cands.append((r2[0], {"l": l_, "op": op_, "r": r_, "mul": mul, "num": 0.0}))
        cands.sort(key=lambda x: -x[0])
        base_sc = _sc_basic(tr_b, cap)[0]
        seen = set()
        cur_s = copy.deepcopy(base)
        cur_trades = bt
        for rnd in range(2):          # সর্বোচ্চ ২টি ফিল্টার (লোভী পদ্ধতি), প্রতিটি আসল ইঞ্জিনে যাচাই
            best = None
            for sc0, sl_ in cands[:10]:
                key = json.dumps(sl_, sort_keys=True)
                if key in seen:
                    continue
                s = copy.deepcopy(cur_s)
                ns = slot(sl_["l"], sl_["op"], sl_["r"], sl_.get("num", 0.0), mul=sl_.get("mul", 1.0), join="AND")
                s["cases"][ci]["entry"].append(ns)
                r = run(s)["trades"]
                a_tr, a_te = min_split(r)
                p_tr, p_te = min_split(cur_trades)
                sa, sta = ssc(a_tr); sp_, stp = ssc(p_tr)
                ta_, stta = ssc(a_te); tp_, sttp = ssc(p_te)
                if sa is None or sp_ is None:
                    continue
                rec = {"rule": "%s %s %s" % (sl_["l"], sl_["op"], (sl_["r"] if sl_["r"] != "number" else sl_["num"]) if sl_.get("mul", 1.0) == 1.0 else "%s×%s" % (sl_["mul"], sl_["r"])),
                       "n": len(r), "d_train": float(sta["net"] - stp["net"]), "d_test": float(stta["net"] - sttp["net"]),
                       "dd_tr": float(sta["dd"]), "dd_tr_prev": float(stp["dd"]), "dd_te": float(stta["dd"]), "dd_te_prev": float(sttp["dd"]),
                       "n_tr": len(a_tr), "n_te": len(a_te), "pf_te": float(stta["pf"]), "slot": sl_, "round": rnd + 1,
                       "rob": robust(r), "rob_prev": robust(cur_trades)}
                rec["ok"] = bool(rec["d_test"] >= 0 and stta["net"] > 0 and (rec["rob"] or -9) > (rec["rob_prev"] or -9) * 1.03)
                seen.add(key)
                filt.append(rec)
                if rec["ok"] and (best is None or (sa > best[0])):
                    best = (sa, rec, s, r)
            if best is None:
                break
            cur_s, cur_trades = best[2], best[3]
            best[1]["chosen"] = True
        out["filter_settings_entry"] = cur_s["cases"][ci]["entry"]
    out["filters"] = filt

    # ---------------- ৩) স্মার্ট সার্চ (Stage A random + Stage B hill-climb) ----------------
    c0 = base["cases"][ci]
    L0 = G["lots"]
    grid = {
        "strike": [-2, -1, 0, 1, 2] if c0["strike"]["mode"] == "offset" else [100, 150, 200, 300, 400],
        "sl": [None, 8, 10, 12, 15, 20, 25, 30, 40],
        "tp": [None, 30, 50, 80, 120, ("rr", 1.5), ("rr", 2.0), ("rr", 3.0)],
        "trail": [None] + [("pct", v, a) for v in (8, 10, 15, 20) for a in (0, 10, 20)] + [("step", 10, 5), ("step", 15, 10), ("step", 20, 10), ("step", 30, 15)],
        "be": [None, 10, 15, 20],
        "ts": [0, 15, 30, 45, 60, 90],
    }
    order = list(grid)
    cur_i = {}
    def to_idx(k):
        c = c0
        if k == "strike":
            v = c["strike"]["off"] if c["strike"]["mode"] == "offset" else c["strike"]["prem"]
        elif k == "sl":
            v = None if c["sl"]["mode"] == "none" else (c["sl"]["val"] if c["sl"]["mode"] == "pct" else "x")
        elif k == "tp":
            v = None if c["tp"]["mode"] == "none" else (c["tp"]["val"] if c["tp"]["mode"] == "pct" else (("rr", c["tp"]["val"]) if c["tp"]["mode"] == "rr" else "x"))
        elif k == "trail":
            m = c["trail"]["mode"]
            v = None if m == "none" else (("pct", c["trail"]["val"], c["trail"]["act"]) if m == "pct" else ("step", c["trail"]["trig"], c["trail"]["move"]) if c["trail"].get("unit", "pct") == "pct" else "x")
        elif k == "be":
            v = (c["be"]["val"] if c["be"]["on"] else None)
        else:
            v = c.get("time_stop", 0) or 0
        for i, g in enumerate(grid[k]):
            if (isinstance(g, tuple) or g is None) and g == v:
                return i
            if not isinstance(g, tuple) and g is not None and v is not None and not isinstance(v, tuple) and v != "x" and abs(float(g) - float(v)) < 1e-9:
                return i
        return None
    for k in order:
        cur_i[k] = to_idx(k)

    def build(idx):
        s = copy.deepcopy(base)
        c = s["cases"][ci]
        k = idx["strike"]; g = grid["strike"][k] if k is not None else None
        if g is not None:
            if c["strike"]["mode"] == "offset":
                c["strike"]["off"] = g
            else:
                c["strike"]["prem"] = g
        k = idx["sl"]
        if k is not None:
            g = grid["sl"][k]
            c["sl"] = {"mode": "none", "val": c["sl"]["val"]} if g is None else {"mode": "pct", "val": float(g)}
        k = idx["tp"]
        if k is not None:
            g = grid["tp"][k]
            c["tp"] = {"mode": "none", "val": c["tp"]["val"]} if g is None else ({"mode": "rr", "val": g[1]} if isinstance(g, tuple) else {"mode": "pct", "val": float(g)})
        k = idx["trail"]
        if k is not None:
            g = grid["trail"][k]
            if g is None:
                c["trail"]["mode"] = "none"
            elif g[0] == "pct":
                c["trail"].update(mode="pct", val=float(g[1]), act=float(g[2]))
            else:
                c["trail"].update(mode="step", unit="pct", trig=float(g[1]), move=float(g[2]))
        k = idx["be"]
        if k is not None:
            g = grid["be"][k]
            c["be"]["on"] = g is not None
            if g is not None:
                c["be"]["val"] = float(g)
        k = idx["ts"]
        if k is not None:
            c["time_stop"] = grid["ts"][k]
        return s

    pool = {}           # json(idx) -> (idx, trades)
    def ev_idx(idx):
        kk = json.dumps(idx, sort_keys=True)
        if kk not in pool:
            pool[kk] = (dict(idx), run(build(idx))["trades"])
        return pool[kk]
    rng = np.random.default_rng(11)
    ev_idx(dict(cur_i))
    nA = 40
    for _ in range(nA):
        idx = {k: int(rng.integers(0, len(grid[k]))) for k in order}
        for k in order:                      # স্ট্রাইক মোড prem হলে বর্তমান-ভিত্তিক থাকুক
            pass
        ev_idx(idx)
    def top(n):
        sc = [(robust(tr) , kk) for kk, (ix, tr) in pool.items()]
        sc = [(a, kk) for a, kk in sc if a is not None]
        sc.sort(reverse=True)
        return [kk for a, kk in sc[:n]]
    for rnd in range(3):
        for kk in top(5):
            ix = dict(pool[kk][0])
            for k in order:
                for dlt in (-1, 1):
                    j = (ix[k] if ix[k] is not None else 0) + dlt
                    if 0 <= j < len(grid[k]):
                        ni = dict(ix)
                        ni[k] = j
                        ev_idx(ni)
        if runs[0] > 230:
            break

    # ---------------- ৪) Walk-Forward (পুল থেকে নির্বাচন, শুধু ট্রেন-অংশে) ----------------
    items = list(pool.items())
    base_kk = json.dumps(dict(cur_i), sort_keys=True)
    wfo = []
    oos_all, oos_base, is_sc = [], [], []
    tr_len = max(2, K - 2)
    for w in range(0, K - tr_len):
        lo, mid, hi = edges[w], edges[w + tr_len], edges[w + tr_len + 1]
        best_kk, best_sc = None, None
        for kk, (ix, tr) in items:
            r = _sc_basic(_slice(tr, lo, mid), cap, 8)
            if r and (best_sc is None or r[0] > best_sc):
                best_kk, best_sc = kk, r[0]
        if best_kk is None:
            continue
        o = _slice(pool[best_kk][1], mid, hi)
        ob = _slice(pool[base_kk][1], mid, hi)
        oos_all += o
        oos_base += ob
        wfo.append({"train": "%s→%s" % (lo[:7], mid[:7]), "test": "%s→%s" % (mid[:7], hi[:7] if hi != "9999-12-31" else "শেষ"),
                    "net_oos": float(sum(t["net"] for t in o)), "net_base": float(sum(t["net"] for t in ob)), "n_oos": len(o),
                    "net_is": float(sum(t["net"] for t in _slice(pool[best_kk][1], lo, mid)))})
    so, sb_ = calc_stats(oos_all, cap), calc_stats(oos_base, cap)
    is_net = sum(w["net_is"] for w in wfo)
    oos_net = sum(w["net_oos"] for w in wfo)
    wfe = (oos_net / (is_net * (1.0))) * (tr_len / 1.0) if is_net > 0 else 0.0     # প্রতি-সময়-ভাগে OOS ÷ IS
    out["wfo"] = {"windows": wfo, "oos": {"n": so["n"], "net": so["net"], "pf": so["pf"], "dd": so["dd"], "win": so["win"]},
                  "base": {"n": sb_["n"], "net": sb_["net"], "pf": sb_["pf"], "dd": sb_["dd"], "win": sb_["win"]},
                  "wfe": float(wfe), "tr_len": tr_len}

    # ---------------- ৫) চূড়ান্ত সাজেশন ----------------
    ranked = sorted([(robust(tr), kk) for kk, (ix, tr) in items if robust(tr) is not None], reverse=True)
    best_kk = ranked[0][1] if ranked else base_kk
    bix, btr = pool[best_kk]
    bs = build(bix)
    if out.get("filter_settings_entry"):
        pass
    # ফিল্টার + সেরা এক্সিট একসাথে চালিয়ে যাচাই
    comb = copy.deepcopy(bs)
    if out.get("filter_settings_entry"):
        comb["cases"][ci]["entry"] = out["filter_settings_entry"]
    comb_tr = run(comb)["trades"]
    def pack(trs):
        a, b2 = min_split(trs)
        return {"all": _tr(calc_stats(trs, cap)), "train": _tr(calc_stats(a, cap)), "test": _tr(calc_stats(b2, cap)),
                "seg_pos": seg_pos(trs), "rob": robust(trs)}
    out["base"] = pack(bt)
    out["exit_best"] = pack(btr)
    out["final"] = pack(comb_tr)
    # পরিবর্তনের তালিকা (settings diff)
    chg = []
    def addc(label, a, b2, muts):
        if a != b2:
            chg.append({"label": label, "from": a, "to": b2, "muts": muts})
    cb, cn = base["cases"][ci], comb["cases"][ci]
    addc("স্ট্রাইক", cb["strike"]["off"] if cb["strike"]["mode"] == "offset" else cb["strike"]["prem"],
         cn["strike"]["off"] if cn["strike"]["mode"] == "offset" else cn["strike"]["prem"],
         [(cp + "strike.off", cn["strike"]["off"])] if cn["strike"]["mode"] == "offset" else [(cp + "strike.prem", cn["strike"]["prem"])])
    addc("SL", (cb["sl"]["mode"], cb["sl"]["val"]), (cn["sl"]["mode"], cn["sl"]["val"]), [(cp + "sl.mode", cn["sl"]["mode"]), (cp + "sl.val", cn["sl"]["val"])])
    addc("টার্গেট", (cb["tp"]["mode"], cb["tp"]["val"]), (cn["tp"]["mode"], cn["tp"]["val"]), [(cp + "tp.mode", cn["tp"]["mode"]), (cp + "tp.val", cn["tp"]["val"])])
    tk = lambda c: (c["trail"]["mode"], c["trail"].get("val"), c["trail"].get("act"), c["trail"].get("trig"), c["trail"].get("move"))
    addc("ট্রেইলিং", tk(cb), tk(cn), [(cp + "trail.mode", cn["trail"]["mode"]), (cp + "trail.val", cn["trail"].get("val")), (cp + "trail.act", cn["trail"].get("act")),
                                    (cp + "trail.unit", cn["trail"].get("unit", "pct")), (cp + "trail.trig", cn["trail"].get("trig")), (cp + "trail.move", cn["trail"].get("move"))])
    addc("ব্রেকইভেন", (cb["be"]["on"], cb["be"]["val"]), (cn["be"]["on"], cn["be"]["val"]), [(cp + "be.on", cn["be"]["on"]), (cp + "be.val", cn["be"]["val"])])
    addc("টাইম-স্টপ", cb.get("time_stop", 0), cn.get("time_stop", 0), [(cp + "time_stop", cn.get("time_stop", 0))])
    if out.get("filter_settings_entry") and len(out["filter_settings_entry"]) != len(cb["entry"]):
        addc("এন্ট্রি ফিল্টার", len(cb["entry"]), len(out["filter_settings_entry"]), [(cp + "entry", out["filter_settings_entry"])])
    out["changes"] = chg
    out["apply"] = [{"path": p, "value": v} for c in chg for p, v in c["muts"]]
    # রায়
    msgs, level = [], "pass"
    w = out["wfo"]
    if len(wfo) == 0:
        level = "warn"
        msgs.append("Walk-Forward চালানোর মতো পর্যাপ্ত সময়-ভাগ নেই — যাচাই দুর্বল।")
    else:
        if w["oos"]["net"] <= 0:
            level = "fail"
            msgs.append("Walk-Forward: অদেখা সময়ে (OOS) প্রক্রিয়াটির মোট নেট ঋণাত্মক — এই স্ট্র্যাটেজিতে টিউনিং ভরসাযোগ্য নয়।")
        elif w["oos"]["net"] < w["base"]["net"]:
            level = "warn"
            msgs.append("Walk-Forward: টিউনিং করা সেটিং অদেখা সময়ে বর্তমান সেটিংয়ের চেয়ে ভালো করেনি (OOS %s ₹ বনাম %s ₹)।" % (round(w["oos"]["net"]), round(w["base"]["net"])))
        else:
            msgs.append("Walk-Forward: অদেখা সময়ে টিউনিং বর্তমানের চেয়ে ভালো (OOS %s ₹ বনাম %s ₹)।" % (round(w["oos"]["net"]), round(w["base"]["net"])))
        if w["wfe"] < 0.5 and w["wfe"] != 0:
            level = "warn" if level == "pass" else level
            msgs.append("WFE %.0f%% (৫০%%-এর নিচে): ইন-স্যাম্পলের লাভ অদেখা সময়ে অর্ধেকের কম টিকেছে।" % (w["wfe"] * 100))
    fa, ba = out["final"], out["base"]
    if fa["seg_pos"] < 0.6:
        level = "warn" if level == "pass" else level
        msgs.append("সাজেশনে মাত্র %.0f%% সময়-ভাগে লাভ — ধারাবাহিকতা কম।" % (fa["seg_pos"] * 100))
    if fa["test"]["net"] < ba["test"]["net"]:
        level = "warn" if level == "pass" else level
        msgs.append("চূড়ান্ত সাজেশন TEST-অংশে (%s ₹) বর্তমানের (%s ₹) চেয়ে খারাপ।" % (round(fa["test"]["net"]), round(ba["test"]["net"])))
    if not chg:
        level = "keep"
        msgs.append("বর্তমান সেটিংই সার্চে সেরা — বদলানোর স্পষ্ট কারণ পাওয়া যায়নি।")
    out["validation"] = {"level": level, "msgs": msgs}
    out["runs"] = runs[0]
    out["secs"] = time.time() - t0
    out["pool"] = len(pool)
    out["warnings"] = ["সব ফল ঐতিহাসিক ডেটার ওপর; ট্রেড-সংখ্যা কম হলে (১০০-র নিচে) যেকোনো সাজেশন অস্থির।",
                       "সার্চে ~%d সেটিং-সংযোগ চালানো হয়েছে; বেশি চেষ্টা = বেশি ভাগ্যের ঝুঁকি, তাই রায় Walk-Forward-এর অদেখা অংশ দেখে নিন।" % len(pool)]
    return out


# =====================================================================
#  একটি ট্রেডের চার্ট ডেটা (স্পট + প্রিমিয়াম)
# =====================================================================

PRICE_OVERLAY = {"ma1", "ma2", "ma3", "st1", "st2", "vwap", "bbup", "bbmid", "bblow", "donhi", "donlo", "orbh", "orbl",
                 "orbm", "dayo", "dayh", "dayl", "pdh", "pdl", "pdc", "pivp", "r1", "r2", "r3", "s1", "s2", "s3",
                 "cprt", "cprb"}
PLAIN_PRICE = {"close", "open", "high", "low", "hl2", "hlc3", "ohlc4"}


def chart_data(D, raw, body):
    st = norm_settings(raw)
    G = st["general"]
    S, B = compute_sources(D, G["tf"], st["ind"])
    d = day_int(body["date"])
    i0 = int(np.searchsorted(B.day, d, "left"))
    i1 = int(np.searchsorted(B.day, d, "right"))
    if i1 <= i0:
        raise ValueError("এই দিনের স্পট ডেটা নেই")
    labels = {x[0]: x[1] for x in SOURCES}
    cs = None
    for c in st["cases"]:
        if c["name"] == body.get("case"):
            cs = c
    cases = [cs] if cs else [c for c in st["cases"] if c["on"]]
    ids, hl = [], {}
    for c in cases:
        for part in ("entry", "exit"):
            for sl_ in c[part]:
                if not sl_["on"]:
                    continue
                for k in ("l", "r"):
                    v = sl_[k]
                    if v != "number" and v in S and v not in PLAIN_PRICE and v not in ids:
                        ids.append(v)
                if sl_["r"] == "number" and sl_["l"] not in PRICE_OVERLAY and sl_["l"] not in PLAIN_PRICE:
                    hl.setdefault(sl_["l"], [])
                    if float(sl_["num"]) not in hl[sl_["l"]]:
                        hl[sl_["l"]].append(float(sl_["num"]))
    PMC = G.get("chart") == "prem"
    pov, ppan = [], []
    if PMC:
        sd_ = body["side"].split()
        ct_ = D.opt.get((day_int(body["expiry"]), sd_[1], float(sd_[2])))
        if ct_ is not None:
            V = contract_view(D, ct_)
            S2, B2 = compute_sources(V, G["tf"], st["ind"])
            j0 = int(np.searchsorted(B2.day, d, "left"))
            j1 = int(np.searchsorted(B2.day, d, "right"))
            T2 = B2.t[j0:j1] * 60

            def series2(arr):
                a = arr[j0:j1]
                return [dict(t=int(T2[k]), v=float(a[k])) for k in range(len(a)) if np.isfinite(a[k])]
            pov = [dict(id=v, name=labels.get(v, v), data=series2(S2[v])) for v in ids if v in PRICE_OVERLAY]
            ppan = [dict(id=v, name=labels.get(v, v), data=series2(S2[v]), hl=hl.get(v, []))
                    for v in ids if v not in PRICE_OVERLAY][:4]
            V._cache.clear()
    T = B.t[i0:i1] * 60
    spot = [dict(t=int(T[k]), o=float(B.o[i0 + k]), h=float(B.h[i0 + k]), l=float(B.l[i0 + k]), c=float(B.c[i0 + k]))
            for k in range(i1 - i0)]

    def series(arr):
        a = arr[i0:i1]
        return [dict(t=int(T[k]), v=float(a[k])) for k in range(len(a)) if np.isfinite(a[k])]
    overlays = [dict(id=v, name=labels.get(v, v), data=series(S[v])) for v in ids if v in PRICE_OVERLAY]
    panes = [dict(id=v, name=labels.get(v, v), data=series(S[v]), hl=hl.get(v, []))
             for v in ids if v not in PRICE_OVERLAY][:4]
    if PMC:
        overlays, panes = [], []
    # প্রিমিয়াম
    side = body["side"].split()
    typ, stk = side[1], float(side[2])
    ex = day_int(body["expiry"])
    ct = D.opt.get((ex, typ, stk))
    prem, lines = [], []
    te = d * 1440 + hm(body["in"])
    tx = d * 1440 + hm(body["out"])
    if ct is not None:
        m = (ct[0] >= d * 1440) & (ct[0] < (d + 1) * 1440)
        idx = np.flatnonzero(m)
        prem = [dict(t=int(ct[0][k]) * 60, o=float(ct[1][k]), h=float(ct[2][k]), l=float(ct[3][k]), c=float(ct[4][k])) for k in idx]
        j = np.flatnonzero(ct[0] == te)
        if len(j) and cs:
            raw_e = float(ct[1][j[0]])
            sd = 1 if cs["action"] == "BUY" else -1
            sm, sv = cs["sl"]["mode"], float(cs["sl"]["val"])
            tm, tv = cs["tp"]["mode"], float(cs["tp"]["val"])
            slv = tpv = None
            if sm == "pct":
                slv = raw_e * (1 - sd * sv / 100)
            elif sm == "pts":
                slv = raw_e - sd * sv
            if tm == "pct":
                tpv = raw_e * (1 + sd * tv / 100)
            elif tm == "pts":
                tpv = raw_e + sd * tv
            elif tm == "rr" and slv is not None:
                tpv = raw_e + sd * tv * abs(raw_e - slv)
            lines.append(dict(v=raw_e, name="এন্ট্রি", color="#4f9cff"))
            if slv is not None:
                lines.append(dict(v=slv, name="SL", color="#ff6161"))
            tgc = cs.get("tg", {})
            if tgc.get("on") and G["lots"] >= 2:
                u_ = tgc.get("unit", "pct")

                def tgp(v_):
                    if u_ == "pct":
                        return raw_e * (1 + sd * v_ / 100)
                    if u_ == "pts":
                        return raw_e + sd * v_
                    return raw_e + sd * v_ * abs(raw_e - slv) if slv is not None else None
                p1 = tgp(float(tgc["t1"]["val"]))
                if p1 is not None:
                    lines.append(dict(v=p1, name="T1", color="#f5c542"))
                if tgc["t2"].get("on"):
                    p2 = tgp(float(tgc["t2"]["val"]))
                    if p2 is not None:
                        lines.append(dict(v=p2, name="T2", color="#f59e42"))
            if tpv is not None:
                lines.append(dict(v=tpv, name="টার্গেট", color="#2ecc71"))
    # স্পটে মার্কার: যে বারে সিগন্যাল শেষ / যে বারে এক্সিট
    def spot_idx(tm_):
        k = int(np.searchsorted(B.e[i0:i1], tm_, side="left"))
        return min(k, len(spot) - 1)
    ke = spot_idx(te)
    kx = min(int(np.searchsorted(B.t[i0:i1], tx, side="right")) - 1, len(spot) - 1)
    kx = max(kx, 0)
    marks = {"spot": [dict(t=spot[ke]["t"], p=spot[ke]["c"], k="in"), dict(t=spot[kx]["t"], p=spot[kx]["c"], k="out")],
             "prem": [dict(t=te * 60, p=float(body.get("entry", 0)), k="in"), dict(t=tx * 60, p=float(body.get("exit", 0)), k="out")]}
    for lg in (body.get("legs") or [])[:-1]:       # শেষ লেগ = মূল এক্সিট; বাকিগুলো আংশিক বুকিং
        try:
            marks["prem"].append(dict(t=(d * 1440 + hm(lg["t"])) * 60, p=float(lg["px"]), k="part", q=int(lg["q"]), why=lg["why"]))
        except (KeyError, ValueError, TypeError):
            pass
    return {"spot": spot, "overlays": overlays, "panes": panes, "prem": prem, "lines": lines, "marks": marks,
            "pov": pov, "ppan": ppan, "pmode": PMC,
            "tf": int(G["tf"]), "base": BASE_TF, "open": OPEN_MIN * 60 + d * 86400}

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


# =====================================================================
#  সার্ভার
# =====================================================================

RUN_LOCK = threading.Lock()
JOBS = {}
JOB_N = [0]
DATA = {"D": None, "src": None}


def clean(o):
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def run_multi(payload, prog):
    """একই স্ট্র্যাটেজি একাধিক ইনডেক্সে পালা করে চালায় (প্রতিটির ডেটা লোড করে, চালিয়ে, ছেড়ে দেয়)"""
    global BASE_TF
    src = DATA.get("src")
    if not src or src.get("mode") != "db":
        raise ValueError("মাল্টি-ইনডেক্সের জন্য --db (SQLite) মোডে চালাতে হবে")
    insts = [str(x).upper() for x in payload.get("insts", [])]
    if not insts:
        raise ValueError("অন্তত একটা ইনডেক্স বাছুন")
    keep_base = BASE_TF
    rows = []
    try:
        for k, inst in enumerate(insts):
            row = {"inst": inst, "name": INSTR_NAMES.get(inst, inst)}
            try:
                D = load_db(src["db_dir"], inst, payload.get("from") or None, payload.get("to") or None, log=lambda *a: None)
                if not D.expiries:
                    row["note"] = "অপশন ডেটা নেই"
                else:
                    res = backtest(D, copy.deepcopy(payload["settings"]))
                    a = res["stats"]["all"]
                    row.update(n=a["n"], net=a["net"], pf=a["pf"], win=a["win"], dd=a["dd"], dd_pct=a["dd_pct"],
                               rec=a["rec"], exp=a["exp"], pm=a["pm"],
                               span="%s → %s" % (day_str(D.du[0]), day_str(D.du[-1])))
                del D
            except Exception as e:
                traceback.print_exc()
                row["note"] = "%s: %s" % (type(e).__name__, str(e)[:100])
            rows.append(row)
            prog((k + 1) / len(insts))
    finally:
        BASE_TF = keep_base
    return {"rows": rows}


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


def paper_tick(sess, D, now):
    today = paper_eval(D, sess["settings"], now)
    paper_diff(sess, today, now)
    with PLOCK:
        sess["now"] = int(now)
        sess["day"] = day_str(now // 1440)
        sess["today"] = [t for t in today]
        sess["open"] = next((t for t in today if t["open"]), None)
    return today


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
            if delay:
                time.sleep(delay)
            now += step
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
        self.last_body = {}

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
        except ValueError as e:
            sess["msg"] = "ফিড ত্রুটি: %s" % e
            if "টোকেন" in str(e):
                raise
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
            "feed": payload.get("feed", "upstox"), "band": int(payload.get("band") or 3)}
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
            if u.path == "/api/live/test":
                src = DATA.get("src") or {}
                return self._json(live_selftest(body.get("inst") or src.get("inst") or "N50", body.get("provider", "upstox")))
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
            if u.path == "/api/chart":
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


HTML = r'''<!doctype html>
<html lang="bn">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0a1710">
<title>Green Bull Algo Builder</title>
<style>
:root{--bg:#050908;--card:#0c1511;--card2:#111d17;--line:#1b2c23;--line2:#27412f;--tx:#eaf3ee;--mut:#7f948a;--ac:#22d36b;--ac2:#13a352;--g:#2ee27d;--r:#ff5a67;--y:#f5c542;--b:#4f9cff}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;color:var(--tx);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans Bengali",sans-serif;font-variant-numeric:tabular-nums}
body{background:radial-gradient(900px 420px at 50% -140px,#12351f 0%,transparent 62%),radial-gradient(600px 300px at 100% 40%,#0c2a1a55 0%,transparent 70%),var(--bg);background-attachment:fixed;min-height:100vh}
header.top{position:sticky;top:0;z-index:5;padding:11px 14px 10px;background:linear-gradient(180deg,#0b1a12f2,#08120dd9);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-bottom:1px solid #1d4430;display:flex;flex-direction:column;gap:7px}
header.top:after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1px;background:linear-gradient(90deg,transparent,#22d36b99,transparent)}
.brand{display:flex;align-items:center;gap:11px}
.logo{width:42px;height:42px;border-radius:13px;background:conic-gradient(from 210deg,#22d36b,#0f7a3a,#22d36b);display:flex;align-items:center;justify-content:center;box-shadow:0 0 0 1px #5df5a255,0 6px 22px #22d36b44}
.logo svg{filter:drop-shadow(0 1px 2px #0006)}
h1{margin:0;font-size:18px;letter-spacing:.3px;font-weight:800;line-height:1.15}
h1 span{background:linear-gradient(90deg,#4ef08d,#22d36b);-webkit-background-clip:text;background-clip:text;color:transparent;font-weight:800}
.tag{font-size:10px;color:#6fae8e;letter-spacing:1.4px;text-transform:uppercase;margin-top:2px}
#dinfo{display:flex;flex-wrap:wrap;gap:5px;font-size:11px;color:var(--mut)}
#dinfo .pl{background:#0b1a12;border:1px solid #1d3a2a;border-radius:20px;padding:2px 10px;color:#a9d3bd}
#dinfo .pl:first-child:before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--ac);margin-right:6px;box-shadow:0 0 8px var(--ac);vertical-align:1px}
nav{display:flex;position:fixed;left:10px;right:10px;bottom:calc(8px + env(safe-area-inset-bottom));height:58px;background:#09130ff0;backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid #1f4030;border-radius:20px;z-index:8;padding:5px;box-shadow:0 12px 34px -8px #000c,0 0 0 1px #ffffff06 inset}
nav button{flex:1;padding:4px 2px;border:0;background:transparent;color:#6f857b;font-size:10px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;border-radius:14px;letter-spacing:.5px;font-weight:600;transition:all .18s}
nav button svg{width:21px;height:21px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
nav button.on{color:#06210f;background:linear-gradient(180deg,#34e57c,#1fbf5e);box-shadow:0 4px 14px #22d36b55}
main{padding:12px 11px 200px;max-width:760px;margin:0 auto}
.h2{display:flex;align-items:center;gap:8px;font-size:11px;letter-spacing:1.2px;text-transform:uppercase;color:#6fae8e;margin:16px 2px 8px}
.h2:after{content:"";flex:1;height:1px;background:linear-gradient(90deg,var(--line2),transparent)}
.pill{display:inline-block;font-size:10px;border:1px solid #2a5a40;color:#7fe0a8;border-radius:20px;padding:1px 8px;margin-left:6px}
.pill.soon{border-color:#5a4a1a;color:#f5c542}
.tilegrid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
section[hidden]{display:none}
.card{background:linear-gradient(180deg,#0e1914,#0b130f);border:1px solid var(--line);border-radius:16px;margin:0 0 11px;padding:0;box-shadow:0 1px 0 #ffffff08 inset,0 14px 28px -18px #000}
.card>summary{padding:13px 14px;font-weight:700;cursor:pointer;list-style:none;letter-spacing:.2px}
.card>summary::-webkit-details-marker{display:none}
.card>summary:after{content:"";float:right;width:8px;height:8px;margin-top:6px;border-right:2px solid var(--mut);border-bottom:2px solid var(--mut);transform:rotate(45deg);transition:transform .2s}
.card[open]>summary:after{transform:rotate(-135deg);margin-top:9px}
.card[open]>summary{border-bottom:1px solid var(--line)}
.body{padding:12px 14px 14px;display:flex;flex-wrap:wrap;gap:10px}
.f{display:flex;flex-direction:column;gap:3px;min-width:92px;flex:1 1 92px}
.f>span{font-size:11px;color:var(--mut);letter-spacing:.2px}
.f.chk{flex-direction:row;align-items:center;gap:8px;flex:1 1 100%}
select,input[type=text],input[type=number],textarea{background:#0a120e;color:var(--tx);border:1px solid var(--line);border-radius:10px;padding:9px 10px;font-size:14px;width:100%;min-width:0;transition:border-color .15s,box-shadow .15s}
select:focus,input:focus,textarea:focus{outline:0;border-color:var(--ac);box-shadow:0 0 0 3px #22d36b22}
input[type=checkbox]{width:19px;height:19px;accent-color:var(--ac)}
.ind{width:100%;background:#0a120e;border:1px solid var(--line);border-radius:12px;padding:10px;display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end}
.ind>b{flex:1 1 100%;font-size:13px}
.case{border-color:#1f5a3f;border-left:3px solid var(--ac)}
.case>summary{color:#8df0b3}
.sub{flex:1 1 100%;font-size:12px;color:var(--mut);margin:8px 0 0;padding-top:10px;border-top:1px dashed var(--line);display:flex;justify-content:space-between;align-items:center}
.cond{flex:1 1 100%;background:#0a120e;border:1px solid var(--line);border-radius:12px;padding:9px;display:flex;flex-direction:column;gap:7px}
.r1{display:flex;gap:6px;align-items:flex-end}
.r1>select,.r1>.mini{flex:1 1 0;min-width:0}
.r1 .mini{min-width:60px}
.r1 input[type=checkbox]{flex:0 0 22px;margin:0 2px 9px 2px}
.r1 .btn{flex:0 0 auto;margin-bottom:1px}
.cond.off{opacity:.45}
select.jn{flex:0 0 70px!important}
.btn{background:#101d16;color:var(--tx);border:1px solid var(--line2);border-radius:11px;padding:9px 13px;font-size:13px;font-weight:600;transition:transform .08s,filter .15s}
.btn:active{transform:scale(.97)}
.btn.p{background:linear-gradient(180deg,#34e57c,#1aa855);border-color:#4cf08e;color:#04180b;font-weight:800;box-shadow:0 6px 18px -4px #22d36b88}
.btn.g{background:#1f6f43;border-color:#2a8a57;color:#fff}
.btn.d{color:var(--r);border-color:#4a2228}
.btn.s{padding:5px 10px;font-size:12px}
.bar{position:fixed;left:10px;right:10px;bottom:calc(74px + env(safe-area-inset-bottom));background:#09130ff2;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid #1f4030;border-radius:16px;padding:8px 10px;display:flex;gap:10px;align-items:center;z-index:6;box-shadow:0 10px 30px -10px #000c}
.bar .btn.p{flex:0 0 auto;padding:11px 22px;font-size:14px;letter-spacing:.4px}
#status{flex:1;font-size:12px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#prog{height:3px;background:linear-gradient(90deg,#22d36b,#9bf5c0);box-shadow:0 0 10px #22d36b;width:0;position:fixed;bottom:calc(74px + env(safe-area-inset-bottom));left:10px;z-index:7;transition:width .3s;border-radius:3px}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{padding:7px 6px;text-align:right;border-bottom:1px solid #15241c;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:#6fae8e;font-weight:600;font-size:10.5px;letter-spacing:.7px;text-transform:uppercase;position:sticky;top:0;background:#0b130f;z-index:1}
tr:nth-child(even) td{background:#0a110e}
tr[data-i]:hover td,tr[data-i]:active td{background:#122419}
.pos{color:var(--g)}.neg{color:var(--r)}
.scroll{overflow:auto;max-height:420px;border:1px solid var(--line);border-radius:12px;background:#0a110e}
.kpis{display:grid;grid-template-columns:repeat(2,1fr);gap:9px;margin-bottom:12px}
.kpi{position:relative;overflow:hidden;background:linear-gradient(160deg,#0f1b15,#0a110e);border:1px solid var(--line);border-radius:14px;padding:11px 12px 11px 15px}
.kpi:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:#2a4a39}
.kpi.good:before{background:linear-gradient(180deg,#34e57c,#13a352)}.kpi.bad:before{background:linear-gradient(180deg,#ff7a85,#d63a49)}
.kpi small{color:var(--mut);display:block;font-size:10.5px;letter-spacing:.5px;text-transform:uppercase}
.kpi b{font-size:19px;font-weight:800;letter-spacing:.2px}
.kpi.hero{grid-column:1/-1;padding:16px 16px 14px 18px;border-radius:18px}
.kpi.hero b{font-size:34px;line-height:1.15;display:block;margin:3px 0 6px}
.kpi.hero.up{background:radial-gradient(500px 160px at 0% 0%,#14402a 0%,transparent 70%),linear-gradient(160deg,#0d2418,#08130d);border-color:#1f6f43}
.kpi.hero.up:before{background:linear-gradient(180deg,#5df5a2,#13a352);width:4px}
.kpi.hero.dn{background:radial-gradient(500px 160px at 0% 0%,#4a1a20 0%,transparent 70%),linear-gradient(160deg,#241012,#130a0b);border-color:#6b2a31}
.kpi.hero.dn:before{background:linear-gradient(180deg,#ff7a85,#d63a49);width:4px}
.hsub{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--mut)}
.hsub i{font-style:normal;color:var(--tx);font-weight:700}
canvas{width:100%;height:200px;background:linear-gradient(180deg,#0c1511,#09100c);border:1px solid var(--line);border-radius:14px;display:block}
.note{color:var(--mut);font-size:12px;margin:7px 2px}
.warn{background:linear-gradient(180deg,#3a2c12,#2c210d);border:1px solid #6b4a14;color:#ffd58a;border-radius:12px;padding:9px 12px;font-size:12px;margin:0 0 11px}
.ok{background:#12301f;border:1px solid #1f6f43;color:#9be7bb}
h3{margin:18px 2px 7px;font-size:14px;font-weight:700;letter-spacing:.2px}
.chip{display:inline-block;background:#101d16;border:1px solid var(--line);border-radius:99px;padding:2px 10px;font-size:11px;color:var(--mut);margin:2px 3px 0 0}
.tgbox{flex:1 1 100%;display:flex;flex-wrap:wrap;gap:10px;background:linear-gradient(160deg,#10200f,#0b150e);border:1px solid #2c5a22;border-radius:14px;padding:11px}
.tgbox>.tgh{flex:1 1 100%;font-weight:700;color:#d6f5a3;display:flex;align-items:center;gap:7px}
.lotplan{flex:1 1 100%;display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12px;color:var(--mut)}
.lp{display:inline-flex;align-items:center;gap:5px;border-radius:9px;padding:3px 9px;font-weight:700;font-size:12px;border:1px solid}
.lp.t1{background:#2a2410;border-color:#6b5a14;color:#f5c542}.lp.t2{background:#2a1c10;border-color:#6b4214;color:#f59e42}.lp.rn{background:#10281a;border-color:#1f6f43;color:#5df5a2}
</style>
</head>
<body>
<header class="top"><div class="brand"><div class="logo"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 17l5-6 4 3 7-9"/><path d="M15 5h5v5"/></svg></div><div><h1>Green Bull <span>Algo Builder</span></h1><div class="tag">Quant Strategy Lab · Backtest · Optimize</div></div></div><div id="dinfo"><span class="pl">লোড হচ্ছে…</span></div></header>
<nav>
  <button data-tab="build" class="on"><svg viewBox="0 0 24 24"><path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/></svg>Strategy</button>
  <button data-tab="res"><svg viewBox="0 0 24 24"><path d="M5 20V11M11 20V4M17 20v-6M3 20h18"/></svg>Results</button>
  <button data-tab="tune"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M18.7 5.3l-2.1 2.1M7.4 16.6l-2.1 2.1"/></svg>Optimize</button>
  <button data-tab="ai"><svg viewBox="0 0 24 24"><path d="M12 3l1.8 4.6L18.5 9l-4.7 1.4L12 15l-1.8-4.6L5.5 9l4.7-1.4zM18 15l.9 2.1L21 18l-2.1.9L18 21l-.9-2.1L15 18l2.1-.9z"/></svg>AI</button>
  <button data-tab="paper"><svg viewBox="0 0 24 24"><path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 13h7M9 17h5"/></svg>Paper</button>
  <button data-tab="pre"><svg viewBox="0 0 24 24"><path d="M6 3h12v18l-6-4-6 4z"/></svg>Library</button>
  <button data-tab="data"><svg viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/></svg>Data</button>
</nav>
<main>
  <section id="t-build"></section>
  <section id="t-res" hidden><div class="note">এখনও কোনো রান হয়নি। নিচে ▶ Run চাপুন।</div></section>
  <section id="t-tune" hidden></section>
  <section id="t-ai" hidden></section>
  <section id="t-paper" hidden></section>
  <section id="t-pre" hidden></section>
  <section id="t-data" hidden></section>
</main>
<div class="bar"><button class="btn p" id="run">▶ Run</button><div id="status">প্রস্তুত</div></div>
<div id="prog"></div>

<script>
"use strict";
let META = null, S = null, SW = [], LAST = null, TUNE = {a: {i: 0, from: 5, to: 30, step: 5}, b: {i: -1, from: 0, to: 0, step: 1}, min: 20, sort: 'net'};
const $ = (s, r) => (r || document).querySelector(s);
const MATYPES = [['ema','EMA'],['sma','SMA'],['wma','WMA'],['rma','RMA'],['hma','HMA']];
let TFS = [[0,'চার্ট TF'],[5,'5m'],[10,'10m'],[15,'15m'],[30,'30m'],[60,'60m']];
let CTFS = TFS.slice(1);
function setBase(b) { const L = [1,3,5,6,9,10,12,15,18,21,30,45,60].filter(x => x % b === 0 && x >= b); TFS = [[0,'চার্ট TF']].concat(L.map(x => [x, x + 'm'])); CTFS = TFS.slice(1); }
const PSRC = [['close','Close'],['open','Open'],['high','High'],['low','Low'],['hl2','HL2'],['hlc3','HLC3'],['ohlc4','OHLC4']];
const OFFS = [[-3,'ITM 3'],[-2,'ITM 2'],[-1,'ITM 1'],[0,'ATM'],[1,'OTM 1'],[2,'OTM 2'],[3,'OTM 3'],[4,'OTM 4']];

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === 'class') e.className = v;
    else if (k === 'value') e.value = v;
    else if (k === 'checked') e.checked = !!v;
    else if (k === 'on') for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
    else if (k === 'html') e.innerHTML = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(9)) { if (kid == null || kid === false) continue; e.append(kid.nodeType ? kid : document.createTextNode(String(kid))); }
  return e;
}
function getp(p) { return p.split('.').reduce((o, k) => o[k], S); }
function setp(p, v) { const ks = p.split('.'); let o = S; for (let i = 0; i < ks.length - 1; i++) o = o[ks[i]]; o[ks[ks.length - 1]] = v; persist(); }
function persist() { try { localStorage.setItem('ab_S2', JSON.stringify(S)); } catch (e) {} }
function merge(def, inc) {
  if (inc == null) return JSON.parse(JSON.stringify(def));
  if (Array.isArray(def) || typeof def !== 'object') return inc;
  const out = JSON.parse(JSON.stringify(def));
  for (const k of Object.keys(inc)) out[k] = (k in def && def[k] && typeof def[k] === 'object' && !Array.isArray(def[k])) ? merge(def[k], inc[k]) : inc[k];
  return out;
}
function fixSettings(inp) {
  const d = META.defaults, s = {general: merge(d.general, (inp || {}).general), ind: merge(d.ind, (inp || {}).ind), cases: []};
  for (const c of ((inp || {}).cases || d.cases)) {
    const m = merge(META.case, Object.assign({}, c, {entry: undefined, exit: undefined}));
    m.entry = (c.entry || META.case.entry).map(x => merge(META.slot, x));
    m.exit = (c.exit || META.case.exit).map(x => merge(META.slot, x));
    s.cases.push(m);
  }
  return s;
}

/* ---------- ফিল্ড বিল্ডার ---------- */
function selCtl(path, opts, cb, numeric) {
  const s = h('select');
  for (const [v, t] of opts) s.append(h('option', {value: v}, t));
  s.value = String(getp(path));
  s.addEventListener('change', () => { setp(path, numeric ? parseFloat(s.value) : s.value); cb && cb(); });
  return s;
}
function numCtl(path, label, step, noreg) {
  const i = h('input', {type: 'number', step: step || 'any', inputmode: 'decimal', value: getp(path)});
  i.addEventListener('change', () => { const x = parseFloat(i.value); setp(path, isNaN(x) ? 0 : x); });
  if (!noreg) SW.push({path, label});
  return i;
}
function txtCtl(path, ph) {
  const i = h('input', {type: 'text', value: getp(path), placeholder: ph || ''});
  i.addEventListener('change', () => setp(path, i.value.trim()));
  return i;
}
function chkCtl(path, cb) {
  const i = h('input', {type: 'checkbox', checked: !!getp(path)});
  i.addEventListener('change', () => { setp(path, i.checked); cb && cb(); });
  return i;
}
const F = (label, ctl) => h('label', {class: 'f'}, h('span', {}, label), ctl);
const FC = (label, ctl) => h('label', {class: 'f chk'}, ctl, h('span', {style: 'font-size:13px;color:var(--tx)'}, label));


/* ---------- আংশিক প্রফিট বুকিং (T1/T2) ---------- */
function lotPlan(L, tg) {
  if (L < 2) return null;
  let left = L - 1; const parts = [];
  const spec = [['T1', tg.t1.qty]].concat(tg.t2.on ? [['T2', tg.t2.qty]] : []);
  spec.forEach(([tag, q]) => { if (left <= 0) return; const n = Math.max(1, Math.min(Math.floor(L * q / 100 + 0.4999), left)); parts.push([tag, n]); left -= n; });
  return {parts, runner: L - parts.reduce((a, x) => a + x[1], 0)};
}
function tgBlock(c, p) {
  const tg = c.tg, L = S.general.lots, box = h('div', {class: 'tgbox'});
  box.append(h('div', {class: 'tgh'}, '🎯 আংশিক প্রফিট বুকিং (Scale-out)'));
  box.append(FC('চালু: Target ছুঁলে কিছু লট বেচে SL সরান', chkCtl(p + '.tg.on', () => renderBuild())));
  if (!tg.on) { box.append(h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, 'যেমন ২ লটে T1-এ ১ লট বেচে SL এন্ট্রিতে আনলে ঝুঁকিহীন রানার বাকি থাকে। লট কমপক্ষে ২ লাগবে।')); return [box]; }
  box.append(F('টার্গেটের একক', selCtl(p + '.tg.unit', [['pct', '% প্রিমিয়াম'], ['pts', 'পয়েন্ট ₹'], ['rr', 'RR × SL']], () => renderBuild())));
  box.append(F('Target 1 মান', numCtl(p + '.tg.t1.val', c.name + ' T1', 1)), F('T1-এ কত % লট বেচবেন', numCtl(p + '.tg.t1.qty', c.name + ' T1 লট%', 5)));
  box.append(F('T1 ছুঁলে SL কোথায় যাবে?', selCtl(p + '.tg.a1.mode', [['none', 'সরবে না'], ['be', 'এন্ট্রিতে (ব্রেকইভেন)'], ['lock', 'এন্ট্রির ওপরে লাভ লক']], () => renderBuild())));
  if (tg.a1.mode === 'lock') box.append(F('লক লাভ % (এন্ট্রির)', numCtl(p + '.tg.a1.val', c.name + ' লক%', 1)));
  box.append(FC('Target 2-ও ব্যবহার করুন', chkCtl(p + '.tg.t2.on', () => renderBuild())));
  if (tg.t2.on) {
    box.append(F('Target 2 মান (T1-এর চেয়ে বড়)', numCtl(p + '.tg.t2.val', c.name + ' T2', 1)), F('T2-তে কত % লট বেচবেন', numCtl(p + '.tg.t2.qty', c.name + ' T2 লট%', 5)));
    box.append(F('T2 ছুঁলে SL কোথায় যাবে?', selCtl(p + '.tg.a2.mode', [['none', 'সরবে না'], ['t1', 'T1-এর দামে'], ['be', 'এন্ট্রিতে']])));
  }
  const plan = lotPlan(L, tg), lp = h('div', {class: 'lotplan'});
  if (!plan) lp.append(h('div', {class: 'warn', style: 'flex:1 1 100%;margin:0'}, 'এখন লট ' + L + ' — ভাগ করা যায় না, তাই বুকিং কাজ করবে না। "লট সংখ্যা" কমপক্ষে ২ দিন।'));
  else {
    lp.append('লট ' + L + ' →');
    plan.parts.forEach(([tag, n]) => lp.append(h('span', {class: 'lp ' + tag.toLowerCase()}, tag + ': ' + n + ' লট')));
    lp.append(h('span', {class: 'lp rn'}, 'রানার: ' + plan.runner + ' লট'));
    lp.append(h('div', {class: 'note', style: 'flex:1 1 100%;margin:2px 0 0'}, 'রানার বাকি লট শেষ টার্গেট / SL / ট্রেইলিং / সিগন্যাল এক্সিট / টাইম স্টপে বেরোয়। প্রতিটি আংশিক বিক্রিতে ব্রোকারেজ আলাদা ধরা হয়। লট পূর্ণসংখ্যায় ভাগ হয়, তাই % আনুমানিক।'));
  }
  box.append(lp);
  return [box];
}

/* ---------- বিল্ড ট্যাব ---------- */
const OPEN = (() => { try { return JSON.parse(sessionStorage.getItem('ab_open') || '{}'); } catch (e) { return {}; } })();
function dtl(key, def, ...kids) {
  const e = h('details', {class: key.startsWith('case') ? 'card case' : 'card', open: key in OPEN ? OPEN[key] : def}, ...kids);
  e.addEventListener('toggle', () => { OPEN[key] = e.open; try { sessionStorage.setItem('ab_open', JSON.stringify(OPEN)); } catch (_) {} });
  return e;
}
function renderBuild() {
  SW = [];
  const root = $('#t-build');
  root.innerHTML = '';
  const G = (k, l, st) => F(l, numCtl('general.' + k, l, st));

  root.append(dtl('gen', true, h('summary', {}, '① সাধারণ সেটিংস'),
    h('div', {class: 'body'},
      META.datasets ? F('ইনডেক্স (কোন ডেটায় ব্যাকটেস্ট)', h('select', {on: {change: async ev => { ev.target.disabled = true; const ok = await loadDS(ev.target.value); if (!ok) renderBuild(); }}}, ...META.datasets.map(d => h('option', {value: d.inst, selected: d.inst === META.info.inst}, d.name + (d.contracts ? '' : ' (অপশন নেই)'))))) : null,
      F('সিগন্যাল কোন চার্টে?', selCtl('general.chart', [['spot', 'স্পট (' + INAME() + ' ইনডেক্স)'], ['prem', 'প্রিমিয়াম (অপশনের নিজের চার্ট)']], () => renderBuild())),
      F('চার্ট টাইমফ্রেম', selCtl('general.tf', CTFS, null, true)),
      F('লট সংখ্যা', numCtl('general.lots', 'লট সংখ্যা', 1)),
      F('মূলধন ₹ (DD% এর জন্য)', numCtl('general.capital', 'মূলধন', 1000, true)),
      F('ডেটা শুরু (YYYY-MM-DD)', txtCtl('general.from', 'খালি = সব')),
      F('ডেটা শেষ', txtCtl('general.to', 'খালি = সব')),
      F('Train/Test ভাগ (ঐচ্ছিক)', selCtl('general.split', [[0,'বন্ধ — পুরো ডেটায় ফাইনাল'],[50,'50 / 50'],[60,'60 / 40'],[70,'70 / 30'],[80,'80 / 20']], null, true)),
      F('এন্ট্রি শুরু (HH:MM)', txtCtl('general.win_from')),
      F('এন্ট্রি শেষ', txtCtl('general.win_to')),
      F('স্কয়ার-অফ', txtCtl('general.sqoff')),
      G('max_trades', 'দিনে সর্বোচ্চ ট্রেড', 1),
      G('day_loss', 'দৈনিক লস লিমিট ₹ (0=বন্ধ)', 100),
      G('day_profit', 'দৈনিক প্রফিট লিমিট ₹ (0=বন্ধ)', 100),
      G('min_dte', 'এক্সপায়ারি কত দিন দূরে (min DTE)', 1),
      FC('এক্সপায়ারির দিন ট্রেড নয়', chkCtl('general.skip_expiry_day')),
      FC('নতুন সিগন্যাল লাগবে (শর্ত false→true হলে তবেই এন্ট্রি)', chkCtl('general.fresh')),
      FC('একই বারে SL ও টার্গেট দুটোই ছুঁলে SL ধরা (রক্ষণশীল)', chkCtl('general.sl_first')))));

  root.append(dtl('cost', false, h('summary', {}, '② খরচ (বাস্তব করুন)'),
    h('div', {class: 'body'},
      G('brokerage', 'ব্রোকারেজ ₹/অর্ডার', 1),
      G('slip', 'স্লিপেজ (প্রিমিয়াম পয়েন্ট/সাইড)', 0.1),
      G('charges_pct', 'চার্জ % (বিক্রয় টার্নওভারে, আনুমানিক)', 0.01),
      h('div', {class: 'note', style: 'flex:1 1 100%'}, 'চার্জ % = STT + এক্সচেঞ্জ + GST ইত্যাদির আনুমানিক যোগফল। নিজের ব্রোকারের চার্জ দেখে ঠিক করুন।'))));

  const I = (k, l, parts) => h('div', {class: 'ind'}, h('b', {}, l), ...parts);
  const ip = (k, f, l, st) => F(l, numCtl('ind.' + k + '.' + f, l + ' [' + k.toUpperCase() + ']', st));
  const itf = k => F('TF', selCtl('ind.' + k + '.tf', TFS, null, true));
  const ind = h('div', {class: 'body'});
  for (const k of ['ma1', 'ma2', 'ma3'])
    ind.append(I(k, 'Moving Average ' + k.slice(2), [F('টাইপ', selCtl('ind.' + k + '.type', MATYPES)), ip(k, 'len', 'Length', 1), itf(k), F('Source', selCtl('ind.' + k + '.src', PSRC))]));
  ind.append(I('rsi', 'RSI', [ip('rsi', 'len', 'Length', 1), itf('rsi'), F('Source', selCtl('ind.rsi.src', PSRC))]));
  ind.append(I('adx', 'ADX / DI', [ip('adx', 'len', 'DI Length', 1), ip('adx', 'smooth', 'Smoothing', 1), itf('adx')]));
  ind.append(I('atr', 'ATR', [ip('atr', 'len', 'Length', 1), itf('atr')]));
  for (const k of ['st1', 'st2'])
    ind.append(I(k, 'Supertrend ' + k.slice(2), [ip(k, 'len', 'Length', 1), ip(k, 'mult', 'Multiplier', 0.5), itf(k)]));
  ind.append(I('macd', 'MACD', [ip('macd', 'fast', 'Fast', 1), ip('macd', 'slow', 'Slow', 1), ip('macd', 'sig', 'Signal', 1), itf('macd')]));
  ind.append(I('bb', 'Bollinger Band', [ip('bb', 'len', 'Length', 1), ip('bb', 'mult', 'Multiplier', 0.1), itf('bb'), F('Source', selCtl('ind.bb.src', PSRC))]));
  ind.append(I('misc', 'অন্যান্য', [ip('orb', 'mins', 'ORB সময় (মিনিট)', 5), ip('vol', 'len', 'Volume SMA Length', 1), ip('don', 'n', 'Highest/Lowest N', 1)]));
  ind.append(h('div', {class: 'note', style: 'flex:1 1 100%'}, 'উচ্চ TF (যেমন 60m EMA) শুধু সম্পূর্ণ হওয়া বার ধরে, তাই লাইভের চেয়ে একটু পিছিয়ে থাকে (রিপেইন্ট নেই)। ইনডেক্সে ভলিউম না থাকলে VWAP = টাইপিক্যাল প্রাইসের গড়।'));
  root.append(dtl('ind', false, h('summary', {}, '③ ইন্ডিকেটর প্যারামিটার'), ind));

  S.cases.forEach((c, ci) => root.append(caseCard(ci)));
  root.append(h('button', {class: 'btn', style: 'width:100%', on: {click: () => {
    S.cases.push(merge(META.case, {name: 'Case ' + (S.cases.length + 1)})); persist(); renderBuild(); }}}, '＋ নতুন Case যোগ করুন'));
  renderTune();
}

function srcSel(path, noNumber) {
  const s = h('select');
  let grp = null, gname = '';
  for (const [id, lb, g] of META.sources) {
    if (noNumber && id === 'number') continue;
    if (g !== gname) { grp = h('optgroup', {label: g}); s.append(grp); gname = g; }
    grp.append(h('option', {value: id}, lb));
  }
  s.value = getp(path);
  return s;
}

function condRow(cpath, list, i, cname, part) {
  const p = cpath + '.' + i, sl = list[i];
  const tag = cname + ' ' + part + '#' + (i + 1);
  const row = h('div', {class: 'cond' + (sl.on ? '' : ' off')});
  const on = chkCtl(p + '.on', () => renderBuild());
  const join = selCtl(p + '.join', [['AND', 'AND'], ['OR', 'OR']]); join.className = 'jn';
  if (i === 0) join.style.visibility = 'hidden';
  const L = srcSel(p + '.l', true); L.addEventListener('change', () => setp(p + '.l', L.value));
  const op = selCtl(p + '.op', META.ops, () => renderBuild());
  const noR = (sl.op === 'rising' || sl.op === 'falling');
  const isNum = sl.r === 'number';
  const R = srcSel(p + '.r', false); R.addEventListener('change', () => { setp(p + '.r', R.value); renderBuild(); });
  const mini = (label, ctl) => h('label', {class: 'f mini'}, h('span', {}, label), ctl);
  const ls = numCtl(p + '.ls', tag + ' বাম shift', 1); ls.min = 0;
  const r3 = h('div', {class: 'r1'}, mini('বাম shift (বার আগে)', ls));
  if (!noR) {
    if (!isNum) { const rs = numCtl(p + '.rs', tag + ' ডান shift', 1); rs.min = 0; r3.append(mini('ডান shift', rs)); }
    r3.append(mini(isNum ? 'সংখ্যা' : 'গুণক ×', isNum ? numCtl(p + '.num', tag + ' সংখ্যা', 'any') : numCtl(p + '.mul', tag + ' গুণক', 0.05)));
  }
  r3.append(h('button', {class: 'btn d s', on: {click: () => { list.splice(i, 1); persist(); renderBuild(); }}}, '✕'));
  row.append(h('div', {class: 'r1'}, on, join, L), h('div', {class: 'r1'}, op, noR ? null : R), r3);
  return row;
}

function condBlock(ci, part, title, hint) {
  const c = S.cases[ci], list = c[part], cpath = 'cases.' + ci + '.' + part;
  const wrap = h('div', {style: 'flex:1 1 100%;display:flex;flex-direction:column;gap:6px'});
  wrap.append(h('div', {class: 'sub'}, h('b', {style: 'color:var(--tx)'}, title), h('span', {}, hint)));
  list.forEach((_, i) => wrap.append(condRow(cpath, list, i, c.name, part === 'entry' ? 'Entry' : 'Exit')));
  wrap.append(h('button', {class: 'btn s', style: 'align-self:flex-start', on: {click: () => {
    list.push(merge(META.slot, {join: part === 'exit' ? 'OR' : 'AND'})); persist(); renderBuild(); }}}, '＋ শর্ত যোগ'));
  return wrap;
}


const MIR_SWAP = {orbh: 'orbl', orbl: 'orbh', donhi: 'donlo', donlo: 'donhi', bbup: 'bblow', bblow: 'bbup', pdh: 'pdl', pdl: 'pdh', r1: 's1', s1: 'r1', r2: 's2', s2: 'r2', r3: 's3', s3: 'r3', cprt: 'cprb', cprb: 'cprt'};
const MIR_OP = {cross_above: 'cross_below', cross_below: 'cross_above', above: 'below', below: 'above', rising: 'falling', falling: 'rising'};
const MIR_NEUTRAL = new Set(['adx', 'atr', 'volume', 'volsma', 'bbw', 'rng', 'body', 'bodyp', 'bodyatr', 'uw', 'lw', 'uwp', 'lwp', 'minsopen', 'cprw', 'orbr', 'gap', 'number']);
function mirrorCase(c) {
  const m = JSON.parse(JSON.stringify(c));
  m.opt = c.opt === 'CE' ? 'PE' : 'CE'; m.name = c.name + ' → ' + m.opt + ' মিরর';
  const fl = s => {
    if (MIR_NEUTRAL.has(s.l)) return;
    s.op = MIR_OP[s.op] || s.op;
    if (MIR_SWAP[s.l]) s.l = MIR_SWAP[s.l];
    if (MIR_SWAP[s.r]) s.r = MIR_SWAP[s.r];
    if (s.l === 'rsi' && s.r === 'number') s.num = Math.round((100 - s.num) * 100) / 100;
    if (s.l === 'cdir' && s.r === 'number') s.num = -s.num;
  };
  (m.entry || []).forEach(fl); (m.exit || []).forEach(fl);
  return m;
}

function caseCard(ci) {
  const c = S.cases[ci], p = 'cases.' + ci;
  const body = h('div', {class: 'body'});
  body.append(h('div', {style: 'flex:1 1 100%;display:flex;gap:7px;flex-wrap:wrap'},
    h('button', {class: 'btn s', on: {click: () => { S.cases.splice(ci + 1, 0, JSON.parse(JSON.stringify(c))); S.cases[ci + 1].name += ' copy'; persist(); renderBuild(); $('#status').textContent = '✓ Case কপি হয়েছে (নিচে)'; }}}, '⎘ হুবহু কপি'),
    h('button', {class: 'btn s', on: {click: () => { S.cases.splice(ci + 1, 0, mirrorCase(c)); persist(); renderBuild(); $('#status').textContent = '✓ ' + (c.opt === 'CE' ? 'PE' : 'CE') + ' মিরর তৈরি — শর্তগুলো চোখে দেখে নিন'; }}}, '🔁 ' + (c.opt === 'CE' ? 'PE' : 'CE') + ' মিরর কপি')),
    F('Case নাম', txtCtl(p + '.name')),
    FC('চালু', chkCtl(p + '.on')),
    F('Action', selCtl(p + '.action', [['BUY', 'BUY (কেনা)'], ['SELL', 'SELL (বিক্রি)']])),
    F('অপশন', selCtl(p + '.opt', [['CE', 'CE'], ['PE', 'PE']])),
    F('স্ট্রাইক বাছাই', selCtl(p + '.strike.mode', [['offset', 'ATM / ITM / OTM'], ['prem', 'প্রিমিয়াম কাছাকাছি']], () => renderBuild())));
  if (c.strike.mode === 'offset') body.append(F('স্ট্রাইক', selCtl(p + '.strike.off', OFFS, null, true)));
  else body.append(F('প্রিমিয়াম ≈ ₹', numCtl(p + '.strike.prem', c.name + ' প্রিমিয়াম লক্ষ্য', 10)));
  if (c.strike.mode === 'prem' && S.general.chart === 'prem') body.append(F('প্রিমিয়াম সীমা ± % (সিগন্যালের সময়)', numCtl(p + '.strike.band', 'সীমা %', 5)));
  body.append(condBlock(ci, 'entry', 'এন্ট্রি শর্ত', 'সিগন্যাল ক্যান্ডেল ক্লোজে, এন্ট্রি পরের বারের ওপেনে'));
  body.append(condBlock(ci, 'exit', 'এক্সিট শর্ত (ঐচ্ছিক)', 'ফাঁকা = শুধু SL/টার্গেট/স্কয়ার-অফ'));
  const pr = '(প্রিমিয়ামের উপর)';
  body.append(h('div', {class: 'sub'}, h('b', {style: 'color:var(--tx)'}, 'SL / টার্গেট ' + pr), h('span', {})));
  body.append(...[
    F('Stop Loss', selCtl(p + '.sl.mode', [['none', 'নেই'], ['pct', '% '], ['pts', 'পয়েন্ট']], () => renderBuild())),
    c.sl.mode !== 'none' ? F('SL মান', numCtl(p + '.sl.val', c.name + ' SL মান', 1)) : null,
    F('Target', selCtl(p + '.tp.mode', [['none', 'নেই'], ['pct', '%'], ['pts', 'পয়েন্ট'], ['rr', 'RR (×SL)']], () => renderBuild())),
    c.tp.mode !== 'none' ? F('টার্গেট মান', numCtl(p + '.tp.val', c.name + ' টার্গেট মান', 1)) : null,
    F('Trailing SL', selCtl(p + '.trail.mode', [['none', 'নেই'], ['step', 'স্টেপ: দাম X সরলে SL Y সরবে'], ['pct', 'পিক থেকে % পিছনে']], () => renderBuild())),
    c.trail.mode === 'pct' ? F('পিক থেকে দূরত্ব %', numCtl(p + '.trail.val', c.name + ' Trail %', 1)) : null,
    c.trail.mode === 'pct' ? F('শুরু হবে লাভ ≥ % (0=সাথে সাথে)', numCtl(p + '.trail.act', c.name + ' Trail শুরু %', 1)) : null,
    c.trail.mode === 'step' ? F('একক', selCtl(p + '.trail.unit', [['pct', '% (এন্ট্রি প্রিমিয়ামের)'], ['pts', 'পয়েন্ট (₹)']], () => renderBuild())) : null,
    c.trail.mode === 'step' ? F('দাম এগোলে (X)', numCtl(p + '.trail.trig', c.name + ' Trail X', 1)) : null,
    c.trail.mode === 'step' ? F('SL সরবে (Y)', numCtl(p + '.trail.move', c.name + ' Trail Y', 1)) : null,
    c.trail.mode === 'step' ? h('div', {class: 'note', style: 'flex:1 1 100%'}, 'উদাহরণ X=10, Y=5: প্রিমিয়াম এন্ট্রি থেকে ১০ এগোলে SL ৫ ওপরে ওঠে, ২০ এগোলে ১০ ওপরে… (শুরুর SL থেকে গোনা)। Y ≤ X রাখুন। হিসাব হয় প্রতিটি বারের শেষে, SL পরের বার থেকে কার্যকর।') : null,
    FC('Breakeven (লাভ এত % হলে SL = এন্ট্রি)', chkCtl(p + '.be.on', () => renderBuild())),
    c.be.on ? F('BE ট্রিগার %', numCtl(p + '.be.val', c.name + ' BE %', 1)) : null,
    F('টাইম স্টপ (মিনিট, 0=বন্ধ)', numCtl(p + '.time_stop', c.name + ' টাইম স্টপ', 5))].filter(Boolean));
  body.append(...tgBlock(c, p));
  body.append(h('div', {style: 'flex:1 1 100%;display:flex;gap:8px'},
    h('button', {class: 'btn s', on: {click: () => { S.cases.splice(ci + 1, 0, JSON.parse(JSON.stringify(c))); S.cases[ci + 1].name += ' copy'; persist(); renderBuild(); }}}, '⎘ কপি'),
    h('button', {class: 'btn d s', on: {click: () => { if (S.cases.length > 1 || confirm('শেষ Case মুছবেন?')) { S.cases.splice(ci, 1); persist(); renderBuild(); } }}}, '🗑 মুছুন')));
  return dtl('case' + ci, ci === 0, h('summary', {}, '④ ' + c.name + ' — ' + c.action + ' ' + c.opt + (c.on ? '' : ' (বন্ধ)')), body);
}

/* ---------- জব রানার ---------- */
async function api(path, body) {
  const r = await fetch(path, body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : undefined);
  return r.json();
}
function INAME() { return (META && META.info && META.info.name) || 'BankNifty'; }
function setHeader(i) {
  $('#dinfo').innerHTML = [(i.name || 'BankNifty'), i.spot_from + ' → ' + i.spot_to, i.spot_days + ' দিন', i.expiries.length + ' এক্সপায়ারি', i.contracts + ' কন্ট্র্যাক্ট', (i.base || 5) + ' মিনিট'].concat(i.vol_days == null ? [] : [i.vol_days > 0 ? 'ভলিউম ✓ ' + i.vol_days + ' দিন' : 'ভলিউম ✗']).map(x => '<span class="pl">' + x + '</span>').join('');
}
async function loadDS(inst, from, to, status) {
  const say = t => { if (status) status.textContent = t; };
  say('লোড হচ্ছে… (১-২ মিনিট লাগতে পারে, অপেক্ষা করুন)');
  try {
    const r = await api('/api/load', {inst, from: from || '', to: to || ''});
    if (r.error) throw new Error(r.error);
    META = await api('/api/meta');
    const ni = META.info; setBase(ni.base || 5); setHeader(ni);
    if (S.general.tf % (ni.base || 5)) S.general.tf = (5 % (ni.base || 5) === 0) ? 5 : (ni.base || 5);
    S.general.from = ''; S.general.to = '';
    persist(); renderBuild(); if (!$('#t-data').hidden) renderData();
    if (r.info && r.info.warn) alert(r.info.warn);
    return true;
  } catch (e) { say('ত্রুটি: ' + e.message); alert('ডেটা লোড হয়নি: ' + e.message); return false; }
}
let BUSY = false;
let LASTRUN = null;
async function saveStrategy(name, note) {
  name = (name || '').trim();
  if (!name) { alert('স্ট্র্যাটেজির নাম দিন'); return false; }
  if (META.presets.user[name] && !confirm('"' + name + '" নামে আগেই সেভ আছে। বদলে নতুনটা সেভ করবেন?')) return false;
  const snap = JSON.parse(JSON.stringify(S));
  const m = {saved: new Date().toISOString().slice(0, 16).replace('T', ' '), note: (note || '').trim()};
  if (LAST && LASTRUN === JSON.stringify(S)) { const a = LAST.stats.all; m.stats = {n: a.n, net: Math.round(a.net), pf: +a.pf.toFixed(2), win: +a.win.toFixed(1), dd: Math.round(a.dd), rr: +a.rr.toFixed(2), from: LAST.meta.from, to: LAST.meta.to}; }
  snap._meta = m;
  const r = await api('/api/preset/save', {name, settings: snap});
  META.presets.user = r.user; renderPresets();
  $('#status').textContent = '✓ সেভ হয়েছে: ' + name;
  return true;
}
async function runJob(payload, label) {
  if (BUSY) return null;
  BUSY = true; $('#run').disabled = true;
  const st = $('#status'), pg = $('#prog');
  st.textContent = label + ' চলছে…'; pg.style.width = '2%';
  try {
    const s = await api('/api/start', payload);
    if (s.error) throw new Error(s.error);
    for (;;) {
      await new Promise(r => setTimeout(r, 500));
      const j = await api('/api/job?id=' + s.id);
      pg.style.width = Math.max(2, (j.progress || 0) * 100) + '%';
      st.textContent = label + ' ' + Math.round((j.progress || 0) * 100) + '%';
      if (j.status === 'done') { st.textContent = 'শেষ (' + (j.secs || 0).toFixed(1) + ' সেকেন্ড)'; return j.result; }
      if (j.status === 'error') throw new Error(j.error);
    }
  } catch (e) {
    st.textContent = 'ত্রুটি: ' + e.message;
    alert('ত্রুটি: ' + e.message);
    return null;
  } finally {
    BUSY = false; $('#run').disabled = false;
    setTimeout(() => { pg.style.width = '0'; }, 600);
  }
}

/* ---------- রেজাল্ট ---------- */
const inr = x => (x < 0 ? '−' : '') + '₹' + Math.abs(Math.round(x)).toLocaleString('en-IN');
const cls = x => x > 0 ? 'pos' : (x < 0 ? 'neg' : '');
const f1 = x => (x == null ? '–' : Number(x).toFixed(1)), f2 = x => (x == null ? '–' : Number(x).toFixed(2));
const tbl = (head, rows, mh) => h('div', {class: 'scroll', style: 'max-height:' + (mh || 'none'), html: '<table><tr>' + head.map(x => '<th>' + x + '</th>').join('') + '</tr>' + rows.map(r => '<tr>' + r.map(c => '<td>' + c + '</td>').join('') + '</tr>').join('') + '</table>'});
const legTxt = t => t.legs ? t.legs.map(l => l.why.replace('Target', 'TP') + '×' + l.q).join('→') : t.reason;
const csvVal = (c, v) => c === 'legs' ? (v ? v.map(l => l.why + ' x' + l.q + ' @' + l.px + ' ' + l.t).join(' | ') : '') : String(v);
const pn = (v, d) => '<span class="' + cls(v) + '">' + (d ? v.toFixed(d) : inr(v)) + '</span>';
function grpTable(root, title, arr, note) {
  if (!arr || !arr.length) return;
  root.append(h('h3', {}, title));
  root.append(tbl(['', 'ট্রেড', 'Win%', 'নেট ₹', 'PF', 'গড় ₹', 'RR', 'DD ₹'],
    arr.map(r => [r.k, r.n, r.win.toFixed(0) + '%', pn(r.net), f2(r.pf), pn(r.exp), f2(r.rr), inr(r.dd)])));
  if (note) root.append(h('div', {class: 'note'}, note));
}
function renderResults(res) {
  LAST = res;
  const root = $('#t-res'); root.innerHTML = '';
  const A = res.stats.all, T = res.stats.train, E = res.stats.test, M = res.meta, N = res.an || {};
  if (!A.n) {
    root.append(h('div', {class: 'warn'}, 'কোনো ট্রেড হয়নি। শর্ত কড়া হতে পারে, বা সময়সীমা/ডেটা মেলেনি। ' + JSON.stringify(M.skipped)));
    return;
  }
  const kp = (l, v, c) => h('div', {class: 'kpi ' + (c === 'neg' ? 'bad' : c === 'pos' ? 'good' : '')}, h('small', {}, l), h('b', {class: c || ''}, v));
  root.append(h('div', {class: 'note', style: 'margin-top:0'}, 'ফাইনাল ফল: পুরো ডেটা ' + M.from + ' → ' + M.to + ' (' + M.days + ' দিন), একবারে চালানো।'));
  (M.warn || []).forEach(w => root.append(h('div', {class: 'warn'}, '⚠ ' + w)));
  const sn = h('input', {type: 'text', placeholder: 'নাম (যেমন: BB Breakout v3)'}), sno = h('input', {type: 'text', placeholder: 'নোট (ঐচ্ছিক)'});
  root.append(h('details', {class: 'card'}, h('summary', {}, '💾 এই স্ট্র্যাটেজি সেভ করুন'), h('div', {class: 'body'}, F('নাম', sn), F('নোট', sno),
    h('button', {class: 'btn p', style: 'align-self:flex-end', on: {click: () => saveStrategy(sn.value, sno.value)}}, 'সেভ'),
    h('div', {class: 'note', style: 'flex:1 1 100%'}, 'সব সেটিং আর এই ফলাফলের সারাংশ সেভ হবে। পরে "প্রিসেট" ট্যাব থেকে লোড করবেন।'))));
  const hero = h('div', {class: 'kpi hero ' + (A.net >= 0 ? 'up' : 'dn')}, h('small', {}, 'নেট P&L'), h('b', {class: cls(A.net)}, inr(A.net)),
    h('div', {class: 'hsub'}, [['ট্রেড', A.n], ['Win', A.win.toFixed(1) + '%'], ['PF', f2(A.pf)], ['Max DD', A.dd_pct.toFixed(1) + '%']].map(([k, v]) => h('span', {}, k + ' ', h('i', {}, String(v))))));
  root.append(h('div', {class: 'kpis'},
    hero, kp('Profit Factor', f2(A.pf)),
    kp('Win %', A.win.toFixed(1) + '%'), kp('ট্রেড', A.n),
    kp('Max Drawdown', inr(A.dd) + ' (' + A.dd_pct.toFixed(1) + '%)', 'neg'), kp('RR (গড় লাভ ÷ গড় লস)', f2(A.rr)),
    kp('গড় ট্রেড (Expectancy)', inr(A.exp), cls(A.exp)), kp('Recovery (নেট ÷ DD)', f2(A.rec)),
    kp('লাভজনক মাস', A.pm.toFixed(0) + '% (' + A.nm + ' মাসে)'), kp('মোট খরচ', inr(A.costs))));
  if (A.n < 100) root.append(h('div', {class: 'warn'}, 'ট্রেড মাত্র ' + A.n + 'টি। ১০০-র কম ট্রেডের ফলে ভরসা কম, ভাগ্যের প্রভাব বেশি থাকে।'));
  else if (A.pm < 50 && A.net > 0) root.append(h('div', {class: 'warn'}, 'নেট লাভ আছে, কিন্তু অর্ধেকের কম মাস লাভে। লাভ হয়তো কয়েকটা বড় ট্রেডের ওপর নির্ভর করছে।'));
  root.append(h('h3', {}, 'ইকুইটি কার্ভ'));
  root.append(h('canvas', {id: 'eq'}));
  root.append(h('h3', {}, 'ড্রডাউন কার্ভ'));
  root.append(h('canvas', {id: 'ddc'}));
  const row = (n, v) => [n, v];
  root.append(h('h3', {}, 'পুরো পরিসংখ্যান'));
  root.append(tbl(['মেট্রিক', 'মান'], [
    row('মোট ট্রেড / দিনে গড়', A.n + ' / ' + f2((N.days || {}).tpd)),
    row('Gross P&L (খরচের আগে)', pn(A.gross)), row('মোট খরচ', inr(A.costs)), row('নেট P&L', pn(A.net)),
    row('Profit Factor', f2(A.pf)), row('Win %', A.win.toFixed(1) + '%'),
    row('গড় লাভ', inr(A.avg_w)), row('গড় লস', inr(A.avg_l)), row('RR (লাভ ÷ লস)', f2(A.rr)),
    row('গড় R (SL ঝুঁকির গুণিতক)', A.avg_r == null ? 'SL নেই' : f2(A.avg_r) + 'R'),
    row('Expectancy প্রতি ট্রেড', pn(A.exp)), row('সেরা / খারাপ ট্রেড', inr(A.best) + ' / ' + inr(A.worst)),
    row('টানা জয় / টানা হার (সর্বোচ্চ)', A.mcw + ' / ' + A.mcl),
    row('গড় হোল্ডিং সময়', A.hold.toFixed(0) + ' মিনিট'),
    row('Max Drawdown', inr(A.dd) + ' (' + A.dd_pct.toFixed(1) + '% মূলধনের)'),
    row('সবচেয়ে লম্বা DD (ট্রেড সংখ্যা)', A.dd_len), row('Recovery Factor', f2(A.rec))]));
  const D = N.days, I = N.dd_info;
  if (I) root.append(tbl(['ড্রডাউন ও দিন', ''], [
    ['সবচেয়ে বড় DD', pn(I.max) + ' (' + I.pct.toFixed(1) + '%)'],
    ['শিখর → তলা', (I.peak_date || '–') + ' → ' + I.trough_date],
    ['ঘুরে দাঁড়ানো', I.recovered ? I.rec_date : 'এখনও পুরোপুরি হয়নি'],
    ['ওই DD-তে ট্রেড', I.trades_in_dd],
    ['ট্রেড হওয়া দিন / লাভজনক দিন %', D.n + ' / ' + D.win.toFixed(0) + '%'],
    ['সেরা / খারাপ দিন', inr(D.best) + ' / ' + inr(D.worst)],
    ['টানা লস দিন (সর্বোচ্চ)', D.mcl]]));
  if (M.split_date) {
    root.append(h('h3', {}, 'Train / Test যাচাই (ঐচ্ছিক)'));
    root.append(tbl(['', 'Train', 'Test'], [['ট্রেড', T.n, E.n], ['নেট', pn(T.net), pn(E.net)], ['PF', f2(T.pf), f2(E.pf)], ['Win%', T.win.toFixed(0) + '%', E.win.toFixed(0) + '%'], ['DD', inr(T.dd), inr(E.dd)]]));
    root.append(h('div', {class: 'note'}, 'এটা শুধু যাচাইয়ের জন্য। বিভাজন তারিখ: ' + M.split_date + '। বন্ধ করতে সেটিংসে "Train/Test ভাগ" বন্ধ করুন।'));
  }
  grpTable(root, '⏰ টাইমিং: এন্ট্রির সময় (৩০ মিনিটের স্লট)', N.by_slot, 'কোন সময়ে এন্ট্রি লাভ/লস দিচ্ছে। খারাপ স্লট বাদ দিতে "এন্ট্রি শুরু/শেষ" বদলান।');
  grpTable(root, 'সপ্তাহের দিন', N.by_dow);
  grpTable(root, 'এক্সিটের কারণ', N.by_reason, 'SL-এ যাওয়া ট্রেড বেশি খারাপ হলে SL ঢিলে বা এন্ট্রি আরও বাছাই করুন।');
  grpTable(root, 'Case অনুযায়ী', N.by_case);
  grpTable(root, 'CE / PE', N.by_type);
  grpTable(root, 'এক্সপায়ারি থেকে দূরত্ব (DTE)', N.by_dte);
  grpTable(root, 'হোল্ডিং সময়', N.by_hold);
  grpTable(root, 'এন্ট্রির প্রিমিয়াম (₹)', N.by_prem);
  const X = N.exc;
  if (X) {
    root.append(h('h3', {}, '🎯 SL / টার্গেট / ট্রেইল ঠিক করার প্যানেল'));
    root.append(h('div', {class: 'note'}, 'MAE = ট্রেডে এন্ট্রির তুলনায় সবচেয়ে বেশি উল্টো যাওয়া, MFE = সবচেয়ে বেশি অনুকূলে যাওয়া (প্রিমিয়ামের %)।'));
    const q = a => a.map(v => v.toFixed(1) + '%');
    root.append(tbl(['', 'মধ্যম', '৭৫%', '৯০%', 'সর্বোচ্চ'], [
      ['জেতা ট্রেডের MAE (' + X.nw + 'টি)'].concat(q(X.mae_w)), ['হারা ট্রেডের MAE (' + X.nl + 'টি)'].concat(q(X.mae_l))]));
    root.append(h('div', {class: 'note'}, 'SL-এর নিয়ম: জেতা ট্রেডের ৯০% MAE-র একটু বাইরে SL রাখলে বেশিরভাগ জেতা ট্রেড টিকে থাকে।'));
    root.append(tbl(['', '২৫%', 'মধ্যম', '৭৫%', '৯০%'], [
      ['জেতা ট্রেডের MFE'].concat(q(X.mfe_w)), ['হারা ট্রেডের MFE'].concat(q(X.mfe_l))]));
    root.append(h('div', {class: 'note'}, 'লাভ ফেরত যাওয়া (জেতা ট্রেডে শিখর থেকে নামা): মধ্যম ' + X.give_med.toFixed(1) + '%, গড় ' + X.give_avg.toFixed(1) + '% → ট্রেইলিং SL এর আশেপাশে রাখা যায়। হারা ট্রেডের ' + X.l_touch['10'].toFixed(0) + '% একসময় +১০% ছুঁয়েছিল, ' + X.l_touch['20'].toFixed(0) + '% +২০% ছুঁয়েছিল (ব্রেকইভেন/ট্রেইল কাজে লাগতে পারে)।'));
    root.append(h('div', {class: 'note'}, 'এই প্যানেল শুধু বর্ণনামূলক (MAE/MFE-র বণ্টন)। "SL/টার্গেট বদলালে কী হতো" এখানে আনুমানিক হিসাব দেওয়া হয় না — সেটার সঠিক উত্তরের জন্য 🧠 AI Advisor ট্যাব ব্যবহার করুন, যা প্রতিটি মান আসল ইঞ্জিনে চালিয়ে মাপে।'));
  }
  const sk = Object.entries(M.skipped).filter(([, v]) => v > 0);
  if (sk.length) root.append(h('div', {class: 'note'}, 'বাদ পড়া সিগন্যাল: ' + sk.map(([k, v]) => k + ' ' + v).join(', ')));
  const per = (arr, ttl) => {
    root.append(h('h3', {}, ttl));
    root.append(tbl(['', 'ট্রেড', 'Win%', 'নেট ₹', 'PF', 'DD ₹'], arr.map(r => [r.k, r.n, r.win.toFixed(0) + '%', pn(r.net), f2(r.pf), inr(r.dd)]), '360px'));
  };
  per(res.yearly, 'বছর অনুযায়ী'); per(res.monthly, 'মাস অনুযায়ী');
  root.append(h('h3', {}, 'ট্রেড তালিকা'));
  const jn = h('input', {type: 'number', inputmode: 'numeric', placeholder: 'ট্রেড নং (১ থেকে ' + res.trades.length + ')', style: 'flex:1'});
  root.append(h('div', {style: 'display:flex;gap:6px;align-items:center'}, jn,
    h('button', {class: 'btn p s', on: {click: () => { const n = parseInt(jn.value); if (n >= 1 && n <= res.trades.length) openChart(n - 1); else alert('১ থেকে ' + res.trades.length + ' এর মধ্যে নম্বর দিন'); }}}, '📈 চার্ট দেখুন'),
    h('button', {class: 'btn s', on: {click: dlCsv}}, '⬇ CSV')));
  root.append(h('div', {class: 'note'}, 'যেকোনো ট্রেডের সারিতে ট্যাপ করলে সেই দিনের স্পট ও প্রিমিয়াম চার্ট খুলবে।'));
  const tb = h('div', {class: 'scroll', style: 'margin-top:6px;max-height:520px', html: '<table><tr><th>#</th><th>তারিখ</th><th>ট্রেড</th><th>ইন</th><th>আউট</th><th>এন্ট্রি</th><th>এক্সিট</th><th>কারণ</th><th>MAE%</th><th>MFE%</th><th>R</th><th>নেট ₹</th></tr>' +
    res.trades.map((t, k) => '<tr data-i="' + k + '" style="cursor:pointer"><td>' + (k + 1) + '</td><td>' + t.date + '</td><td>' + t.side + '</td><td>' + t.in + '</td><td>' + t.out + '</td><td>' + t.entry + '</td><td>' + t.exit + '</td><td>' + legTxt(t) + '</td><td>' + t.mae + '</td><td>' + t.mfe + '</td><td>' + (t.r == null ? '–' : t.r) + '</td><td class="' + cls(t.net) + '">' + Math.round(t.net) + '</td></tr>').join('') + '</table>'});
  tb.addEventListener('click', e => { const r = e.target.closest('tr[data-i]'); if (r) openChart(parseInt(r.dataset.i)); });
  root.append(tb);
}
/* ---------- একটি ট্রেডের চার্ট (স্পট + প্রিমিয়াম) ---------- */
const COLS = ['#f1c40f', '#c678dd', '#56b6c2', '#e5894f', '#98c379', '#e06c9f'];
function hmOf(t) { const d = new Date(t * 1000); return String(d.getUTCHours()).padStart(2, '0') + ':' + String(d.getUTCMinutes()).padStart(2, '0'); }
/* ---------- চার্ট (নতুন): পিঞ্চ-জুম, ক্রসহেয়ার, সিঙ্ক ---------- */
let CH = {i: 0};
const CHS = {view: 'both', hide: {}};
const CU = '#26c281', CD = '#ff5d5d';
function fnum(x, rg) { if (x == null || !isFinite(x)) return '–'; return x.toFixed(rg > 80 ? 0 : rg > 6 ? 1 : 2); }
function niceStep(span) { for (const s of [300, 600, 900, 1800, 3600, 7200]) if (span / s <= 6.5) return s; return 7200; }
function setupCv(cv, W, H) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  cv._w = W; cv._h = H; cv._dpr = dpr; cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  cv.style.width = W + 'px'; cv.style.height = H + 'px';
}
function pill(g, txt, x, y, bg, fg, align) {
  g.font = 'bold 10px sans-serif'; const w = g.measureText(txt).width + 8;
  const x1 = align === 'center' ? x - w / 2 : align === 'right' ? x - w : x;
  g.fillStyle = bg; g.beginPath(); (g.roundRect ? g.roundRect(x1, y - 8, w, 16, 4) : g.rect(x1, y - 8, w, 16)); g.fill();
  g.fillStyle = fg || '#fff'; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(txt, x1 + w / 2, y + 0.5);
}
function drawPane(cv, o, R) {
  const dpr = cv._dpr, W = cv._w, H = cv._h, g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
  const padR = 52, padT = 24, padB = o.last ? 20 : 6, x0 = 2, a = R.a, b = R.b;
  const X = t => x0 + (t - a) / (b - a) * (W - padR - x0);
  const vis = t => t >= a - 900 && t <= b + 900;
  let vals = [];
  (o.bars || []).forEach(k => { if (vis(k.t)) vals.push(k.h, k.l); });
  (o.lines || []).forEach(L => { if (!L.hide) L.data.forEach(p => { if (vis(p.t)) vals.push(p.v); }); });
  (o.hl || []).forEach(v => vals.push(v)); (o.hlines || []).forEach(L => vals.push(L.v));
  g.fillStyle = '#10151d'; g.fillRect(0, 0, W, H);
  if (!vals.length) { g.fillStyle = '#8b96a8'; g.font = '11px sans-serif'; g.fillText(o.title + ' — এই অংশে ডেটা নেই', 8, 14); return; }
  let mn = Math.min(...vals), mx = Math.max(...vals); if (mx === mn) { mx += 1; mn -= 1; }
  const rg = mx - mn, pd = rg * 0.07; mn -= pd; mx += pd;
  const Y = v => padT + (H - padT - padB) * (1 - (v - mn) / (mx - mn));
  const dec = (mx - mn);
  g.font = '10px sans-serif'; g.textBaseline = 'middle';
  for (let k = 0; k <= 4; k++) {
    const v = mn + (mx - mn) * k / 4, y = Y(v);
    g.strokeStyle = '#1c2431'; g.lineWidth = 1; g.beginPath(); g.moveTo(x0, y); g.lineTo(W - padR, y); g.stroke();
    g.fillStyle = '#7d889b'; g.textAlign = 'left'; g.fillText(fnum(v, dec), W - padR + 5, y);
  }
  const ts = niceStep(b - a);
  for (let t = Math.ceil(a / ts) * ts; t <= b; t += ts) {
    const x = X(t); g.strokeStyle = '#1c2431'; g.beginPath(); g.moveTo(x, padT); g.lineTo(x, H - padB); g.stroke();
    if (o.last) { g.fillStyle = '#7d889b'; g.textAlign = 'center'; g.fillText(hmOf(t), x, H - 8); }
  }
  // ট্রেডের সময়কাল ও SL/টার্গেট জোন
  if (o.zone) {
    const xa = X(o.zone.a), xb = X(o.zone.b);
    g.fillStyle = o.zone.color; g.fillRect(xa, padT, Math.max(xb - xa, 1), H - padT - padB);
    (o.bands || []).forEach(bd => { g.fillStyle = bd.color; const y1 = Y(bd.v1), y2 = Y(bd.v2); g.fillRect(xa, Math.min(y1, y2), Math.max(xb - xa, 1), Math.abs(y2 - y1)); });
  }
  (o.hl || []).forEach(v => { g.strokeStyle = '#4d5a70'; g.setLineDash([4, 3]); g.beginPath(); g.moveTo(x0, Y(v)); g.lineTo(W - padR, Y(v)); g.stroke(); g.setLineDash([]); g.fillStyle = '#7d889b'; g.textAlign = 'left'; g.fillText(v, W - padR + 5, Y(v) - 8); });
  // BB ব্যান্ড ফিল
  const up = (o.lines || []).find(L => L.name === 'BB Upper' && !L.hide), lo = (o.lines || []).find(L => L.name === 'BB Lower' && !L.hide);
  if (up && lo && up.data.length === lo.data.length) {
    g.fillStyle = 'rgba(86,182,194,.09)'; g.beginPath();
    up.data.forEach((p, i) => { const x = X(p.t + o.lstep / 2); i ? g.lineTo(x, Y(p.v)) : g.moveTo(x, Y(p.v)); });
    for (let i = lo.data.length - 1; i >= 0; i--) g.lineTo(X(lo.data[i].t + o.lstep / 2), Y(lo.data[i].v));
    g.closePath(); g.fill();
  }
  // ক্যান্ডেল
  (o.bars || []).forEach(k => {
    if (!vis(k.t)) return;
    const upc = k.c >= k.o, col = upc ? CU : CD, xc = X(k.t + o.step / 2), w = Math.max(1, (X(k.t + o.step) - X(k.t)) * 0.7);
    g.strokeStyle = col; g.fillStyle = col; g.lineWidth = 1;
    g.beginPath(); g.moveTo(xc, Y(k.h)); g.lineTo(xc, Y(k.l)); g.stroke();
    const y1 = Y(Math.max(k.o, k.c)), y2 = Y(Math.min(k.o, k.c)); g.fillRect(xc - w / 2, y1, w, Math.max(1, y2 - y1));
  });
  // ইন্ডিকেটর লাইন
  (o.lines || []).forEach((L, i) => {
    if (L.hide) return;
    g.strokeStyle = L.color || COLS[i % COLS.length]; g.lineWidth = 1.6; g.lineJoin = 'round'; g.beginPath(); let first = true, last = null;
    L.data.forEach(p => { const x = X(p.t + o.lstep / 2), y = Y(p.v); if (first || (last !== null && p.t - last > o.lstep * 3)) g.moveTo(x, y); else g.lineTo(x, y); first = false; last = p.t; }); g.stroke();
  });
  // এন্ট্রি / SL / টার্গেট লেভেল
  (o.hlines || []).forEach(L => {
    g.strokeStyle = L.color; g.setLineDash([6, 3]); g.lineWidth = 1.1; g.beginPath(); g.moveTo(x0, Y(L.v)); g.lineTo(W - padR, Y(L.v)); g.stroke(); g.setLineDash([]);
    pill(g, fnum(L.v, 0.1), W - padR + 1, Y(L.v), L.color, '#0b0f14', 'left');
    g.fillStyle = L.color; g.font = '10px sans-serif'; g.textAlign = 'left'; g.textBaseline = 'middle'; g.fillText(L.name, x0 + 4, Y(L.v) - 7);
  });
  // এন্ট্রি → এক্সিট সংযোগ ও মার্কার
  const mi = (o.marks || []).find(m => m.k === 'in'), mo = (o.marks || []).find(m => m.k === 'out');
  if (mi && mo) { g.strokeStyle = mo.win ? CU : CD; g.setLineDash([3, 3]); g.lineWidth = 1.4; g.beginPath(); g.moveTo(X(mi.t + o.step / 2), Y(mi.p)); g.lineTo(X(mo.t + o.step / 2), Y(mo.p)); g.stroke(); g.setLineDash([]); }
  (o.marks || []).forEach(m => {
    if (m.k === 'part') { const px_ = X(m.t + o.step / 2), py_ = Y(m.p); g.fillStyle = '#f5c542'; g.beginPath(); g.moveTo(px_, py_ - 6); g.lineTo(px_ + 6, py_); g.lineTo(px_, py_ + 6); g.lineTo(px_ - 6, py_); g.closePath(); g.fill();
      pill(g, m.why + ' ×' + m.q + ' ' + fnum(m.p, 100), Math.min(Math.max(px_, 46), W - padR - 46), py_ - 20, '#f5c542', '#0b0f14', 'center'); return; }
    const x = X(m.t + o.step / 2), y = Y(m.p), inn = m.k === 'in', col = inn ? '#4f9cff' : (m.win ? CU : CD);
    g.fillStyle = col; g.beginPath();
    if (inn) { g.moveTo(x, y + 3); g.lineTo(x - 6, y + 13); g.lineTo(x + 6, y + 13); } else { g.moveTo(x, y - 3); g.lineTo(x - 6, y - 13); g.lineTo(x + 6, y - 13); }
    g.closePath(); g.fill();
    pill(g, (inn ? 'IN ' : 'OUT ') + fnum(m.p, 100), Math.min(Math.max(x, 36), W - padR - 36), inn ? y + 25 : y - 25, col, '#0b0f14', 'center');
  });
  // শিরোনাম + লেজেন্ড (ট্যাপ করলে লুকায়/দেখায়)
  g.textAlign = 'left'; g.textBaseline = 'alphabetic'; let lx = 6; o._leg = [];
  g.font = 'bold 11px sans-serif'; g.fillStyle = '#e8ebf0'; g.fillText(o.title, lx, 14); lx += g.measureText(o.title).width + 10; g.font = '10px sans-serif';
  (o.lines || []).forEach((L, i) => {
    const col = L.color || COLS[i % COLS.length], t = '● ' + L.name, w = g.measureText(t).width;
    if (L.name) { g.fillStyle = L.hide ? '#4d5a70' : col; g.fillText(t, lx, 14); o._leg.push({n: L.name, a: lx - 3, b: lx + w + 3}); lx += w + 10; }
  });
  // ক্রসহেয়ার
  if (R.cx != null) {
    const x = X(R.cx);
    if (x >= x0 && x <= W - padR) {
      g.strokeStyle = '#9aa7bb'; g.lineWidth = 1; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(x, padT); g.lineTo(x, H - padB); g.stroke();
      if (R.cy != null && R.cid === o.id) {
        g.beginPath(); g.moveTo(x0, R.cy); g.lineTo(W - padR, R.cy); g.stroke();
        const v = mn + (mx - mn) * (1 - (R.cy - padT) / (H - padT - padB));
        g.setLineDash([]); pill(g, fnum(v, dec), W - padR + 1, R.cy, '#3a4658', '#fff', 'left');
      }
      g.setLineDash([]);
      if (o.last) pill(g, hmOf(R.cx), x, H - 9, '#3a4658', '#fff', 'center');
    }
  }
  g.setLineDash([]);
}
async function openChart(i) {
  if (!LAST || !LAST.trades.length) return;
  i = Math.max(0, Math.min(LAST.trades.length - 1, i)); CH.i = i;
  let ov = $('#chOv'); if (!ov) { ov = h('div', {id: 'chOv', style: 'position:fixed;inset:0;background:#0b0f14;z-index:30;overflow:auto;-webkit-overflow-scrolling:touch'}); document.body.append(ov); }
  ov.innerHTML = ''; ov.hidden = false;
  const T = LAST.trades[i], N = LAST.trades.length, win = T.net > 0;
  const nav = h('div', {style: 'position:sticky;top:0;z-index:6;background:#0b0f14ee;border-bottom:1px solid #2a3445;padding:8px 10px;display:flex;gap:6px;align-items:center'},
    h('button', {class: 'btn s', on: {click: () => openChart(i - 1)}}, '◀'),
    h('div', {style: 'flex:1;text-align:center;line-height:1.2'}, h('b', {}, 'ট্রেড #' + (i + 1) + ' / ' + N), h('div', {style: 'font-size:11px;color:#8b96a8'}, T.date + ' (' + T.dow + ') · ' + T.case)),
    h('button', {class: 'btn s', on: {click: () => openChart(i + 1)}}, '▶'),
    h('button', {class: 'btn s', on: {click: () => { const e = ov; if (document.fullscreenElement) document.exitFullscreen(); else if (e.requestFullscreen) e.requestFullscreen().catch(() => {}); }}}, '⤢'),
    h('button', {class: 'btn s d', on: {click: () => { if (document.fullscreenElement) document.exitFullscreen().catch(() => {}); ov.hidden = true; window.removeEventListener('resize', ov._rs || (() => {})); }}}, '✕'));
  ov.append(nav);
  const pc = win ? '#26c281' : '#ff5d5d';
  ov.append(h('div', {style: 'margin:8px 10px;padding:10px 12px;border:1px solid #2a3445;border-left:4px solid ' + pc + ';border-radius:10px;background:#131922'},
    h('div', {style: 'display:flex;justify-content:space-between;align-items:baseline;gap:8px'},
      h('span', {style: 'font-weight:600'}, T.side), h('b', {style: 'font-size:20px;color:' + pc}, inr(T.net))),
    h('div', {style: 'font-size:12px;color:#8b96a8;margin-top:4px'}, 'এক্সপায়ারি ' + T.expiry + ' · DTE ' + T.dte),
    h('div', {style: 'display:flex;flex-wrap:wrap;gap:6px;margin-top:8px'},
      [['ইন', T.in + ' @ ' + T.entry], ['আউট', T.out + ' @ ' + T.exit], ['কারণ', T.reason], ['হোল্ড', T.hold + ' মি'], ['MAE', T.mae + '%'], ['MFE', T.mfe + '%'], ['R', T.r == null ? '–' : T.r]]
        .map(([k, v]) => h('span', {style: 'font-size:11px;background:#1b2330;border:1px solid #2a3445;border-radius:6px;padding:2px 7px'}, k + ' ', h('b', {}, String(v))))),
    T.legs ? h('div', {style: 'margin-top:8px;font-size:11px;color:#8b96a8'}, 'আংশিক বুকিং: ', T.legs.map(l => h('span', {style: 'display:inline-block;margin:2px 6px 0 0;background:#1b2330;border:1px solid #2a3445;border-radius:6px;padding:2px 7px;color:#e8ebf0'}, l.why + ' ×' + l.q + ' লট @ ' + l.px + ' (' + l.t + ')'))) : null));
  const loading = h('div', {class: 'note', style: 'margin:10px'}, 'চার্ট লোড হচ্ছে...'); ov.append(loading);
  let d;
  try { d = await api('/api/chart', {settings: JSON.parse(LASTRUN || JSON.stringify(S)), date: T.date, side: T.side, expiry: T.expiry, in: T.in, out: T.out, entry: T.entry, exit: T.exit, case: T.case, legs: T.legs}); } catch (e) { d = {error: String(e)}; }
  loading.remove();
  if (d.error) { ov.append(h('div', {class: 'warn', style: 'margin:10px'}, 'চার্ট আনা যায়নি: ' + d.error)); return; }
  const t0 = d.spot[0].t, t1 = t0 + 375 * 60, full = t1 - t0, base = d.base * 60, tfs = d.tf * 60;
  const mIn = d.marks.prem.find(m => m.k === 'in'), mOut = d.marks.prem.find(m => m.k === 'out');
  const R = {a: t0, b: t1, cx: null, cy: null, cid: null};
  const zone = mIn && mOut ? {a: mIn.t, b: mOut.t + base, color: win ? 'rgba(38,194,129,.07)' : 'rgba(255,93,93,.07)'} : null;
  const lv = nm => (d.lines || []).find(l => l.name === nm);
  const bands = [];
  if (lv('এন্ট্রি') && lv('SL')) bands.push({v1: lv('এন্ট্রি').v, v2: lv('SL').v, color: 'rgba(255,93,93,.10)'});
  if (lv('এন্ট্রি') && lv('টার্গেট')) bands.push({v1: lv('এন্ট্রি').v, v2: lv('টার্গেট').v, color: 'rgba(38,194,129,.10)'});
  const ctl = h('div', {style: 'display:flex;gap:6px;flex-wrap:wrap;margin:0 10px 8px'});
  ov.append(ctl);
  const info = h('div', {style: 'margin:0 10px 8px;min-height:88px;font-size:12px;line-height:1.5;background:#131922;border:1px solid #2a3445;border-radius:8px;padding:6px 9px;color:#c7d0de'}, 'চার্টে আঙুল রাখুন বা টানুন: ক্রসহেয়ার ও মান দেখাবে। দুই আঙুলে জুম/সরানো, ডবল-ট্যাপে পুরো দিন। ওপরের নামে ট্যাপ করলে ইন্ডিকেটর লুকাবে।');
  ov.append(info);
  const box = h('div', {style: 'position:relative;margin:0 0 6px;touch-action:pan-y;user-select:none;-webkit-user-select:none'}); ov.append(box);
  let panes = [];
  const mk = (id, h_, o) => { const cv = h('canvas', {style: 'display:block;border-bottom:1px solid #1c2431;touch-action:pan-y'}); cv._id = id; cv._H = h_; cv._o = o; return cv; };
  const lines = (arr, tag) => (arr || []).map(x => ({name: x.name, data: x.data, hide: !!CHS.hide[tag + x.name], tag}));
  function build() {
    box.innerHTML = ''; panes = [];
    const v = CHS.view, single = v !== 'both', Hm = single ? 330 : 250;
    const list = [];
    if (v !== 'prem') {
      list.push(['spot', Hm, {title: 'স্পট ' + d.tf + 'm', bars: d.spot, step: tfs, lstep: tfs, lines: lines(d.overlays, 's:'), zone, marks: d.marks.spot.map(m => Object.assign({win}, m))}]);
      (d.panes || []).forEach((p, k) => list.push(['sp' + k, 110, {title: p.name, step: tfs, lstep: tfs, lines: [{name: '', data: p.data, color: '#56b6c2'}], hl: p.hl, zone}]));
    }
    if (v !== 'spot' && d.prem.length) {
      list.push(['prem', Hm, {title: 'প্রিমিয়াম ' + T.side.replace(/^BUY |^SELL /, '') + (d.pmode ? ' · ' + d.tf + 'm সিগন্যাল' : ''), bars: d.prem, step: base, lstep: tfs, lines: lines(d.pov, 'p:'), hlines: d.lines, zone, bands, marks: d.marks.prem.map(m => Object.assign({win}, m))}]);
      (d.ppan || []).forEach((p, k) => list.push(['pp' + k, 110, {title: p.name + ' (প্রিমিয়াম)', step: tfs, lstep: tfs, lines: [{name: '', data: p.data, color: '#56b6c2'}], hl: p.hl, zone}]));
    }
    if (v !== 'spot' && !d.prem.length) box.append(h('div', {class: 'warn', style: 'margin:10px'}, 'এই কন্ট্র্যাক্টের প্রিমিয়াম ডেটা পাওয়া যায়নি।'));
    list.forEach(([id, H_, o], k) => { o.id = id; o.last = k === list.length - 1; const cv = mk(id, H_, o); box.append(cv); panes.push(cv); });
    resize();
  }
  function resize() { const W = Math.max(box.clientWidth, 200); panes.forEach(cv => setupCv(cv, W, cv._H)); redraw(); }
  let raf = 0;
  function redraw() { if (raf) return; raf = requestAnimationFrame(() => { raf = 0; panes.forEach(cv => drawPane(cv, cv._o, R)); }); }
  const plotW = () => (panes[0] ? panes[0]._w : 300) - 52 - 2;
  const tAt = x => R.a + (x - 2) / plotW() * (R.b - R.a);
  function setRange(a, b) {
    const minS = Math.max(base * 6, 1080); let span = Math.min(Math.max(b - a, minS), full);
    const c = (a + b) / 2; a = c - span / 2; b = c + span / 2;
    if (a < t0) { b += t0 - a; a = t0; } if (b > t1) { a -= b - t1; b = t1; }
    R.a = Math.max(a, t0); R.b = Math.min(b, t1); redraw();
  }
  const fitDay = () => setRange(t0, t1);
  const fitTrade = () => { if (mIn && mOut) setRange(mIn.t - 40 * 60, mOut.t + 40 * 60); };
  const zoom = k => { const c = (R.a + R.b) / 2, s = (R.b - R.a) * k / 2; setRange(c - s, c + s); };
  const pan = k => { const s = (R.b - R.a) * k; setRange(R.a + s, R.b + s); };
  const chip = (txt, fn, on) => h('button', {class: 'btn s', style: on ? 'background:#4f9cff;border-color:#4f9cff;color:#fff' : '', on: {click: fn}}, txt);
  function drawCtl() {
    ctl.innerHTML = '';
    [['both', 'দুটোই'], ['spot', 'স্পট'], ['prem', 'প্রিমিয়াম']].forEach(([k, t]) => ctl.append(chip(t, () => { CHS.view = k; drawCtl(); build(); }, CHS.view === k)));
    ctl.append(chip('পুরো দিন', fitDay), chip('ট্রেডে ফোকাস', fitTrade), chip('＋', () => zoom(0.6)), chip('－', () => zoom(1.6)), chip('⟵', () => pan(-0.3)), chip('⟶', () => pan(0.3)));
  }
  const find = (arr, t, step) => arr.find(k => t >= k.t && t < k.t + step);
  function showInfo(t) {
    const a = find(d.spot, t, tfs), b = find(d.prem, t, base), rows = [];
    const cl = (x, rg) => x ? 'O ' + fnum(x.o, rg) + ' H ' + fnum(x.h, rg) + ' L ' + fnum(x.l, rg) + ' C ' + fnum(x.c, rg) : '–';
    const colr = x => x ? (x.c >= x.o ? CU : CD) : '#8b96a8';
    const sp = h('div', {}, h('b', {}, hmOf(t) + '  '), 'স্পট ', h('span', {style: 'color:' + colr(a)}, cl(a, 100)));
    const pr = h('div', {}, 'প্রিমিয়াম ', h('span', {style: 'color:' + colr(b)}, cl(b, 10)));
    info.innerHTML = ''; info.append(sp, pr);
    const iv = [];
    const val = (L, step) => { const p = L.data.find(q => t >= q.t && t < q.t + step); return p ? L.name + ' ' + p.v.toFixed(2) : null; };
    [...(d.overlays || []), ...(d.panes || [])].forEach(L => { const s = val(L, tfs); if (s) iv.push(s); });
    [...(d.pov || []), ...(d.ppan || [])].forEach(L => { const s = val(L, tfs); if (s) iv.push(s + ' (প্রি)'); });
    if (iv.length) info.append(h('div', {style: 'color:#8b96a8'}, iv.join(' · ')));
  }
  // টাচ/মাউস
  const P = new Map(); let pinch = null, tapAt = 0, moved = 0, startXY = null;
  const local = e => { for (const cv of panes) { const r = cv.getBoundingClientRect(); if (e.clientY >= r.top && e.clientY <= r.bottom) return {cv, x: e.clientX - r.left, y: e.clientY - r.top}; } const r = panes[0].getBoundingClientRect(); return {cv: panes[0], x: e.clientX - r.left, y: 0}; };
  function cross(e) {
    const L = local(e), t = tAt(L.x), snapped = t0 + Math.floor((t - t0) / base) * base + base / 2;
    if (t < t0 || t > t1) return;
    R.cx = snapped; R.cy = L.y; R.cid = L.cv._id; showInfo(snapped); redraw();
  }
  box.addEventListener('pointerdown', e => {
    if (!panes.length) return;
    P.set(e.pointerId, {x: e.clientX, y: e.clientY}); moved = 0; startXY = {x: e.clientX, y: e.clientY};
    try { box.setPointerCapture(e.pointerId); } catch (_) {}
    if (P.size === 2) { const [p, q] = [...P.values()]; const r = box.getBoundingClientRect(); pinch = {d: Math.hypot(p.x - q.x, p.y - q.y) || 1, a: R.a, b: R.b, m: (p.x + q.x) / 2 - r.left}; R.cx = null; }
    else if (e.pointerType !== 'mouse' || e.button === 0) cross(e);
  });
  box.addEventListener('pointermove', e => {
    if (!panes.length) return;
    const prev = P.get(e.pointerId);
    if (prev) { moved += Math.abs(e.clientX - prev.x) + Math.abs(e.clientY - prev.y); P.set(e.pointerId, {x: e.clientX, y: e.clientY}); }
    if (P.size >= 2 && pinch) {
      const [p, q] = [...P.values()], r = box.getBoundingClientRect(), dist = Math.hypot(p.x - q.x, p.y - q.y) || 1, mid = (p.x + q.x) / 2 - r.left;
      const span = (pinch.b - pinch.a) * pinch.d / dist, tc = pinch.a + (pinch.m - 2) / plotW() * (pinch.b - pinch.a);
      const f = (mid - 2) / plotW(); setRange(tc - f * span, tc - f * span + span); return;
    }
    if (e.pointerType === 'mouse') {
      if (e.buttons & 1 && prev) { const dx = e.clientX - prev.x; const s = -dx / plotW() * (R.b - R.a); setRange(R.a + s, R.b + s); }
      else cross(e);
    } else if (prev) cross(e);
  });
  const up = e => {
    const had = P.delete(e.pointerId); if (P.size < 2) pinch = null;
    if (!had || P.size) return;
    if (moved < 8) {
      const L = local(e), o = L.cv._o;
      if (L.y < 22 && o._leg) { const hit = o._leg.find(z => L.x >= z.a && L.x <= z.b); if (hit) { const key = (o.id.startsWith('p') && o.id !== 'prem' ? 'p:' : (o.id === 'prem' ? 'p:' : 's:')) + hit.n; CHS.hide[key] = !CHS.hide[key]; build(); return; } }
      const now = Date.now(); if (now - tapAt < 320) { fitDay(); tapAt = 0; } else tapAt = now;
    }
  };
  box.addEventListener('pointerup', up); box.addEventListener('pointercancel', e => { P.delete(e.pointerId); pinch = null; });
  box.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') { R.cx = null; redraw(); } });
  box.addEventListener('wheel', e => { e.preventDefault(); const L = local(e), t = tAt(L.x), k = e.deltaY > 0 ? 1.18 : 0.85, f = (L.x - 2) / plotW(); const span = (R.b - R.a) * k; setRange(t - f * span, t - f * span + span); }, {passive: false});
  ov._rs && window.removeEventListener('resize', ov._rs); ov._rs = () => resize(); window.addEventListener('resize', ov._rs);
  drawCtl(); build();
  ov.append(h('div', {class: 'note', style: 'margin:0 10px 24px'}, 'IN = এন্ট্রি (নীল), OUT = এক্সিট (সবুজ/লাল)। প্রিমিয়ামে নীল রেখা = এন্ট্রি, লাল = প্রথম SL, সবুজ = টার্গেট; হালকা লাল/সবুজ জোন = ঝুঁকি/লাভের এলাকা। ইন্ডিকেটর শুধু এই Case-এর শর্তে ব্যবহার হওয়াগুলো দেখায়।'));
}
function rgba(hex, a) { const n = parseInt(hex.slice(1), 16); return 'rgba(' + (n >> 16) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')'; }
function drawLine(id, arr, color, fill) {
  const cv = $('#' + id); if (!cv || !arr || !arr.length) return;
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  if (!W) return;
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext('2d'); g.scale(dpr, dpr);
  const e = [0].concat(arr), mn = Math.min(...e, 0), mx = Math.max(...e, 0), padL = 8, padR = 8, padT = 16, padB = 14;
  const X = i => padL + (W - padL - padR) * i / Math.max(e.length - 1, 1), Y = v => H - padB - (H - padT - padB) * (v - mn) / Math.max(mx - mn, 1);
  g.font = '10px sans-serif'; g.textBaseline = 'middle';
  for (let k = 0; k <= 3; k++) {
    const v = mn + (mx - mn) * k / 3, y = Y(v);
    g.strokeStyle = '#16251d'; g.lineWidth = 1; g.beginPath(); g.moveTo(padL, y); g.lineTo(W - padR, y); g.stroke();
    g.fillStyle = '#5d7268'; g.textAlign = 'left'; g.fillText(Math.round(v).toLocaleString('en-IN'), padL + 2, y - 7);
  }
  g.strokeStyle = '#2f4a3b'; g.setLineDash([4, 4]); g.beginPath(); g.moveTo(padL, Y(0)); g.lineTo(W - padR, Y(0)); g.stroke(); g.setLineDash([]);
  const up = e[e.length - 1] >= 0, lc = id === 'eq' ? (up ? '#2ee27d' : '#ff5a67') : color;
  const grd = g.createLinearGradient(0, padT, 0, H - padB); grd.addColorStop(0, rgba(lc, id === 'eq' ? .30 : .26)); grd.addColorStop(1, rgba(lc, 0));
  g.beginPath(); e.forEach((v, i) => i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v)));
  g.lineTo(X(e.length - 1), Y(0)); g.lineTo(X(0), Y(0)); g.closePath(); g.fillStyle = grd; g.fill();
  g.strokeStyle = lc; g.lineWidth = 2; g.lineJoin = 'round'; g.shadowColor = lc; g.shadowBlur = 8; g.beginPath();
  e.forEach((v, i) => i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))); g.stroke(); g.shadowBlur = 0;
  const lx = X(e.length - 1), ly = Y(e[e.length - 1]), lab = Math.round(e[e.length - 1]).toLocaleString('en-IN'), w = g.measureText(lab).width + 12;
  g.fillStyle = lc; g.beginPath(); g.arc(lx, ly, 3.5, 0, 7); g.fill();
  g.fillStyle = lc; g.beginPath(); (g.roundRect ? g.roundRect(Math.min(lx - w, W - padR - w), Math.max(ly - 22, 2), w, 16, 5) : g.rect(Math.min(lx - w, W - padR - w), Math.max(ly - 22, 2), w, 16)); g.fill();
  g.fillStyle = '#04140a'; g.textAlign = 'center'; g.font = 'bold 10px sans-serif'; g.fillText(lab, Math.min(lx - w, W - padR - w) + w / 2, Math.max(ly - 22, 2) + 8.5);
}
function drawEq() {
  if (!LAST) return;
  drawLine('eq', LAST.equity, '#2ee27d');
  drawLine('ddc', (LAST.an || {}).dd, '#ff6161', 'rgba(255,97,97,.18)');
}
function dlCsv() {
  const T = LAST.trades, cols = Object.keys(T[0]);
  const csv = [cols.join(',')].concat(T.map(t => cols.map(c => '"' + csvVal(c, t[c]).replace(/"/g, '""') + '"').join(','))).join('\n');
  const a = h('a', {href: URL.createObjectURL(new Blob([csv], {type: 'text/csv'})), download: 'trades.csv'}); document.body.append(a); a.click(); a.remove();
}

/* ---------- টিউনিং ---------- */
function rangeVals(from, to, step) {
  const out = []; if (!(step > 0)) return [from];
  for (let v = from, n = 0; v <= to + 1e-9 && n < 80; v += step, n++) out.push(Math.round(v * 1e6) / 1e6);
  return out;
}
function renderTune() {
  const root = $('#t-tune'); root.innerHTML = '';
  if (META.datasets && META.info.mode === 'db') {
    const ds = META.datasets.filter(d => d.contracts > 0);
    const boxes = ds.map(d => { const c = h('input', {type: 'checkbox', value: d.inst}); c.checked = true; return [c, h('label', {style: 'display:flex;gap:6px;align-items:center;margin-right:12px'}, c, d.name)]; });
    const res = h('div', {id: 'multiRes'});
    const btn = h('button', {class: 'btn g'}, '🌐 সব ইনডেক্সে চালান');
    btn.addEventListener('click', async () => {
      const insts = boxes.filter(([c]) => c.checked).map(([c]) => c.value);
      if (!insts.length) { alert('অন্তত একটা ইনডেক্স বাছুন'); return; }
      const r = await runJob({kind: 'multi', settings: S, insts, from: S.general.from, to: S.general.to}, 'মাল্টি-ইনডেক্স');
      if (r) renderMulti(res, r);
    });
    root.append(h('div', {class: 'h2'}, 'একই স্ট্র্যাটেজি, সব ইনডেক্সে'));
    root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, h('div', {style: 'display:flex;flex-wrap:wrap;flex:1 1 100%'}, boxes.map(b => b[1])), btn,
      h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, 'প্রতিটি ইনডেক্সের ডেটা পালা করে লোড হয়ে ব্যাকটেস্ট চলে, তাই সময় লাগে। সেটিংস ট্যাবের বর্তমান সেটিংসই ব্যবহার হয়। অপশন ডেটা নেই এমন ইনডেক্স তালিকায় আসে না।'))));
    root.append(res);
  }
  if (!SW.length) return;
  const opts = SW.map((s, i) => [i, s.label]);
  const pick = (key, none) => {
    const T = TUNE[key], s = h('select');
    if (none) s.append(h('option', {value: -1}, '— নেই —'));
    opts.forEach(([v, t]) => s.append(h('option', {value: v}, t)));
    s.value = T.i; if (T.i >= SW.length) { T.i = none ? -1 : 0; s.value = T.i; }
    s.addEventListener('change', () => { T.i = parseInt(s.value); const cur = T.i >= 0 ? getp(SW[T.i].path) : 0; if (T.i >= 0) { T.from = cur; T.to = cur * 2 || 10; T.step = Math.max(cur / 2, 1) || 1; } renderTune(); });
    const num = (k, l) => { const i = h('input', {type: 'number', step: 'any', value: T[k]}); i.addEventListener('change', () => T[k] = parseFloat(i.value) || 0); return F(l, i); };
    return h('div', {class: 'body'}, F(none ? 'দ্বিতীয় প্যারামিটার' : 'প্যারামিটার', s), T.i >= 0 ? [num('from', 'শুরু'), num('to', 'শেষ'), num('step', 'ধাপ')] : null,
      T.i >= 0 ? h('div', {class: 'note', style: 'flex:1 1 100%'}, 'এখনকার মান: ' + getp(SW[T.i].path) + ' | মান: ' + rangeVals(T.from, T.to, T.step).join(', ')) : null);
  };
  root.append(h('div', {class: 'note'}, 'এখন সেটিংস ট্যাবে যা আছে সেটাই বেস। এক বা দুটি সংখ্যা বেছে রেঞ্জ দিন, সব কম্বিনেশন ব্যাকটেস্ট হবে (সর্বোচ্চ ১৫০)। ফলাফল পুরো ডেটার ওপর, ✓ চিহ্নিত আর আশপাশের মানও ভালো এমন সেটিং বাছুন।'));
  root.append(h('div', {class: 'card'}, pick('a', false)));
  root.append(h('div', {class: 'card'}, pick('b', true)));
  const mt = h('input', {type: 'number', value: TUNE.min}); mt.addEventListener('change', () => TUNE.min = parseInt(mt.value) || 0);
  root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, F('ন্যূনতম ট্রেড (সব)', mt),
    h('button', {class: 'btn g', style: 'align-self:flex-end', on: {click: doSweep}}, '🎛 টিউনিং চালান'))));
  root.append(h('div', {id: 'sweepRes'}));
}
function renderMulti(box, r) {
  const rows = r.rows.map(x => x.note ? '<tr><td>' + x.name + '</td><td colspan="7" class="note">' + x.note + '</td></tr>'
    : '<tr><td>' + x.name + '</td><td>' + x.n + '</td><td class="' + cls(x.net) + '">' + inr(x.net) + '</td><td>' + x.pf.toFixed(2) + '</td><td>' + x.win.toFixed(0) + '%</td><td>' + inr(x.dd) + '</td><td>' + x.rec.toFixed(2) + '</td><td>' + x.pm.toFixed(0) + '%</td></tr>').join('');
  box.innerHTML = '<div class="card"><div class="body"><div class="scroll"><table><tr><th>ইনডেক্স</th><th>ট্রেড</th><th>নেট</th><th>PF</th><th>Win</th><th>DD</th><th>Recov.</th><th>লাভের মাস</th></tr>' + rows + '</table></div></div></div>';
}
async function doSweep() {
  const params = [];
  for (const k of ['a', 'b']) {
    const T = TUNE[k]; if (T.i < 0) continue;
    const vals = rangeVals(T.from, T.to, T.step);
    params.push({path: SW[T.i].path, label: SW[T.i].label, values: vals});
  }
  if (!params.length) return;
  const res = await runJob({kind: 'sweep', settings: S, params}, 'টিউনিং');
  if (res) { LASTSWEEP = res; renderSweep(); }
}
let LASTSWEEP = null;
function renderSweep() {
  const box = $('#sweepRes'); if (!box || !LASTSWEEP) return;
  const R = LASTSWEEP, sorters = {net: r => r.all.net, pf: r => r.all.pf, dd: r => r.all.dd, exp: r => r.all.exp, pm: r => r.all.pm, rec: r => r.all.rec, rr: r => r.all.rr};
  if (!(TUNE.sort in sorters)) TUNE.sort = 'net';
  let rows = R.rows.filter(r => r.all.n >= TUNE.min);
  rows = rows.slice().sort((a, b) => sorters[TUNE.sort](b) - sorters[TUNE.sort](a));
  const sel = h('select', {on: {change: e => { TUNE.sort = e.target.value; renderSweep(); }}});
  [['net', 'সবচেয়ে বেশি নেট'], ['pf', 'সবচেয়ে ভালো PF'], ['rec', 'Recovery (নেট ÷ DD)'], ['dd', 'সবচেয়ে কম Drawdown'], ['pm', 'বেশি লাভজনক মাস %'], ['exp', 'গড় ট্রেড'], ['rr', 'RR']].forEach(([v, t]) => sel.append(h('option', {value: v}, t)));
  sel.value = TUNE.sort;
  box.innerHTML = '';
  box.append(h('h3', {}, 'ফল (' + rows.length + '/' + R.rows.length + ')'), F('সাজান', sel));
  if (!rows.length) { box.append(h('div', {class: 'note'}, 'ন্যূনতম ট্রেড শর্তে কিছু মেলেনি।')); return; }
  const head = R.labels.map(l => '<th>' + l.replace(/\[.*?\]/, '') + '</th>').join('');
  const body = rows.slice(0, 80).map(r => {
    const ok = r.all.net > 0 && r.all.pf >= 1.2 && r.all.pm >= 50;
    const id = R.rows.indexOf(r);
    return '<tr>' + r.vals.map(v => '<td>' + v + '</td>').join('') + '<td>' + r.all.n + '</td><td class="' + cls(r.all.net) + '">' + Math.round(r.all.net) + '</td><td>' + r.all.pf.toFixed(2) + '</td><td>' + r.all.win.toFixed(0) + '%</td><td>' + Math.round(r.all.dd) + '</td><td>' + r.all.rr.toFixed(2) + '</td><td>' + r.all.pm.toFixed(0) + '%</td><td>' + (ok ? '✓' : '') + '</td><td><button class="btn s" data-a="' + id + '">লাগান</button></td></tr>';
  }).join('');
  const t = h('div', {class: 'scroll', html: '<table><tr>' + head + '<th>ট্রেড</th><th>নেট ₹</th><th>PF</th><th>Win%</th><th>DD ₹</th><th>RR</th><th>মাস+</th><th>✓</th><th></th></tr>' + body + '</table>'});
  t.addEventListener('click', e => {
    const b = e.target.closest('button[data-a]'); if (!b) return;
    const r = R.rows[parseInt(b.dataset.a)];
    R.paths.forEach((p, i) => setp(p, r.vals[i]));
    renderBuild(); showTab('build'); $('#status').textContent = 'সেটিং লাগানো হয়েছে, ▶ Run চাপুন';
  });
  box.append(t, h('div', {class: 'note'}, '✓ = নেট লাভ, PF ≥ ১.২ এবং অন্তত অর্ধেক মাস লাভে। সবচেয়ে ভালো একটা সংখ্যার দিকে না তাকিয়ে এমন মান বাছুন যার আশেপাশের মানগুলোও ভালো, তাতে ভাগ্যের ওপর নির্ভরতা কমে।'));
}

/* ---------- প্রিসেট ---------- */
function renderPresets() {
  const root = $('#t-pre'); root.innerHTML = '';
  const nm = h('input', {type: 'text', placeholder: 'স্ট্র্যাটেজির নাম'}), nt = h('input', {type: 'text', placeholder: 'নোট (ঐচ্ছিক)'});
  root.append(h('div', {class: 'card'}, h('div', {class: 'body', style: 'padding-top:12px'}, F('বর্তমান সেটিং সেভ করুন', nm), F('নোট', nt),
    h('button', {class: 'btn p', style: 'align-self:flex-end', on: {click: () => saveStrategy(nm.value, nt.value)}}, '💾 সেভ'),
    h('div', {class: 'note', style: 'flex:1 1 100%'}, 'সেভ ফাইল: presets.json (algo_builder.py-র ফোল্ডারে)। ব্যাকআপের জন্য এই ফাইলটা কপি করে রাখুন।'))));
  const list = (title, obj, user) => {
    const box = h('div', {class: 'card'}, h('summary', {style: 'padding:11px 12px;font-weight:600;display:block'}, title));
    const b = h('div', {class: 'body'});
    const ks = Object.keys(obj);
    if (!ks.length) b.append(h('div', {class: 'note'}, 'কিছু নেই'));
    ks.forEach(k => { const mt = (obj[k] || {})._meta; b.append(h('div', {style: 'flex:1 1 100%;display:flex;gap:6px;align-items:center;flex-wrap:wrap'}, h('span', {style: 'flex:1 1 60%'}, k),
      h('button', {class: 'btn s', on: {click: () => { S = fixSettings(obj[k]); persist(); renderBuild(); showTab('build'); }}}, 'লোড'),
      user ? h('button', {class: 'btn d s', on: {click: async () => { if (!confirm('"' + k + '" মুছবেন?')) return; const r = await api('/api/preset/delete', {name: k}); META.presets.user = r.user; renderPresets(); }}}, '🗑') : null,
      mt ? h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, (mt.stats ? 'ট্রেড ' + mt.stats.n + ' | নেট ₹' + mt.stats.net + ' | PF ' + mt.stats.pf + ' | Win ' + mt.stats.win + '% | DD ₹' + mt.stats.dd + ' | ' + mt.stats.from + ' → ' + mt.stats.to : 'সেভের সময় ব্যাকটেস্ট ছিল না') + ' | ' + mt.saved + (mt.note ? ' | ' + mt.note : '')) : null)); });
    box.append(b); root.append(box);
  };
  list('আমার প্রিসেট', META.presets.user, true);
  list('নমুনা টেমপ্লেট (শুধু উদাহরণ, নিজের মতো বদলান)', META.presets.builtin, false);
  const ta = h('textarea', {rows: 6, placeholder: 'এখানে JSON পেস্ট করে Import করুন', style: 'font-family:monospace;font-size:11px'});
  root.append(h('div', {class: 'card'}, h('div', {class: 'body', style: 'padding-top:12px'}, ta,
    h('button', {class: 'btn s', on: {click: () => { ta.value = JSON.stringify(S, null, 1); }}}, 'Export (বর্তমান সেটিং)'),
    h('button', {class: 'btn s', on: {click: () => { try { S = fixSettings(JSON.parse(ta.value)); persist(); renderBuild(); showTab('build'); } catch (e) { alert('JSON ঠিক নেই: ' + e.message); } }}}, 'Import'),
    h('button', {class: 'btn d s', on: {click: () => { if (confirm('সব সেটিং ডিফল্টে ফেরাবেন?')) { S = fixSettings(META.defaults); persist(); renderBuild(); showTab('build'); } }}}, 'ডিফল্টে ফেরান'))));
}


function renderData() {
  const root = $('#t-data'); root.innerHTML = '';
  if (!META) return;
  const i = META.info;
  if (META.datasets) {
    const ds = META.datasets;
    const sel = h('select', {}, ...ds.map(d => h('option', {value: d.inst, selected: d.inst === i.inst}, d.name + ' — স্পট ' + d.spot_from + ' → ' + d.spot_to + (d.contracts ? ' | অপশন ' + d.done + '/' + d.contracts + ' কন্ট্র্যাক্ট' : ' | অপশন নেই'))));
    const f0 = h('input', {type: 'date', value: i.from || ''}), f1 = h('input', {type: 'date', value: i.to || ''});
    const msg = h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, 'ইনডেক্স ও সময় বেছে "লোড করুন" চাপুন। তারিখ ফাঁকা = সব ডেটা (লোড হতে কয়েক মিনিট লাগতে পারে, বেশি মেমোরি লাগে)। দরকারে তারিখ সীমা দিন।');
    const btn = h('button', {class: 'btn s'}, '⬇ লোড করুন');
    btn.addEventListener('click', async () => { btn.disabled = true; const ok = await loadDS(sel.value, f0.value, f1.value, msg); if (!ok) btn.disabled = false; });
    root.append(h('div', {class: 'h2'}, 'ডেটাসেট বেছে নিন'));
    root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, F('ইনডেক্স', sel), F('শুরু (ঐচ্ছিক)', f0), F('শেষ (ঐচ্ছিক)', f1), btn, msg)));
  }
  if (i.mode === 'db') {
    const log = h('pre', {class: 'note', style: 'flex:1 1 100%;max-height:220px;overflow:auto;font-size:11px;white-space:pre-wrap;margin:0'}, 'আপডেটে স্পট, ফিউচার ভলিউম ও অপশনের নতুন অংশ নামে। প্রথমবার বা বেশি দিন বাকি থাকলে সময় লাগে। চলার সময় এই পাতা খোলা রাখুন।');
    const ub = h('button', {class: 'btn g'}, '🔄 ডেটা আপডেট');
    const poll = async () => {
      if (!document.body.contains(log)) return;
      const s = await api('/api/update/status');
      if (s.log && s.log.length) { log.textContent = s.log.join('\n'); log.scrollTop = log.scrollHeight; }
      if (s.running) { ub.disabled = true; setTimeout(poll, 3000); }
      else { ub.disabled = false; if (s.log && s.log.length) log.textContent += '\n\n✔ শেষ। নতুন ডেটা দেখতে ওপরের "লোড করুন" চাপুন।'; }
    };
    ub.addEventListener('click', async () => { const r = await api('/api/update', {}); if (r.error) { alert(r.error); return; } poll(); });
    poll();
    root.append(h('div', {class: 'h2'}, 'ডেটা আপডেট'));
    root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, ub, log)));
  }
  root.append(h('div', {class: 'h2'}, 'লোড করা ডেটা'));
  root.append(h('div', {class: 'kpis'},
    [['স্পট রেঞ্জ', i.spot_from + ' → ' + i.spot_to], ['ট্রেডিং দিন', i.spot_days], ['এক্সপায়ারি', i.expiries.length], ['কন্ট্র্যাক্ট', i.contracts]]
      .map(([k, v]) => h('div', {class: 'kpi'}, h('small', {}, k), h('b', {}, String(v))))));
  const rows = i.expiries.map(e => '<tr><td>' + e.expiry + '</td><td>' + (e.lot || '–') + '</td><td>' + e.from + ' → ' + e.to + '</td><td>' + e.ce + '/' + e.pe + '</td></tr>').join('');
  root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, h('div', {class: 'scroll', style: 'flex:1 1 100%', html: '<table><thead><tr><th>এক্সপায়ারি</th><th>লট</th><th>রেঞ্জ</th><th>CE/PE স্ট্রাইক</th></tr></thead><tbody>' + rows + '</tbody></table>'}))));
  root.append(h('div', {class: 'h2'}, 'পরিকল্পিত (এখনো তৈরি হয়নি)'));
  [['অটো ডেটা ডাউনলোডার', 'স্পট, ফিউচার, প্রিমিয়াম ও স্টকের ডেটা এক জায়গা থেকে আপডেট'],
   ['স্টক স্ক্যানার', 'Nifty 50 থেকে স্টক বেছে স্ট্র্যাটেজি লাগানো; যেদিন যে স্টকে সেটআপ আগে রেডি হয়, সেখানে এন্ট্রি'],
   ['লাইভ / পেপার সিগন্যাল', 'রোজকার সিগন্যাল ও পেপার ট্রেড ট্র্যাক']]
    .forEach(([t, d]) => root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, h('div', {style: 'flex:1 1 100%'}, h('b', {}, t), h('span', {class: 'pill soon'}, 'শীঘ্রই')), h('div', {class: 'note', style: 'flex:1 1 100%'}, d)))));
}


/* ---------- 🧠 AI Advisor ---------- */
function drawAI2(root, A) {
  const sg = v => (v > 0 ? '+' : '') + inr(v).replace('₹', '₹');
  const pc = v => '<span class="' + (v >= 0 ? 'pos' : 'neg') + '">' + (v >= 0 ? '+' : '−') + '₹' + Math.abs(Math.round(v)).toLocaleString('en-IN') + '</span>';
  const V = A.validation, lv = {pass: '✅ টেকসই', keep: '👌 বদল দরকার নেই', warn: '⚠️ সতর্কতা', fail: '❌ যাচাইয়ে টেকেনি'}[V.level] || V.level;
  root.append(h('h3', {}, '🔬 v2 ফল — ' + A.case + ' (' + A.n_base + ' ট্রেড, ' + A.K + ' সময়-ভাগ, ' + A.runs + ' রান, ' + A.secs.toFixed(0) + 's)'));
  root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, h('div', {style: 'flex:1 1 100%', html: '<b>' + lv + '</b><br>' + V.msgs.map(m => '• ' + m).join('<br>')}))));
  const row = (n, x) => [n, x.all.n, pc(x.all.net), x.all.pf.toFixed(2), pc(x.train.net), pc(x.test.net), (x.seg_pos * 100).toFixed(0) + '%'];
  root.append(h('h3', {}, 'আগে বনাম পরে'));
  root.append(tbl(['', 'ট্রেড', 'নেট', 'PF', 'TRAIN', 'TEST', 'লাভজনক সময়-ভাগ'], [row('বর্তমান', A.base), row('শুধু এক্সিট টিউন', A.exit_best), row('এক্সিট + ফিল্টার (চূড়ান্ত)', A.final)]));
  if (A.ablation.length) {
    root.append(h('h3', {}, 'রুল অ্যাব্লেশন — শর্তটি বন্ধ করলে কী হয়'));
    root.append(tbl(['শর্ত', 'ট্রেড', 'নেট Δ', 'TRAIN Δ', 'TEST Δ', 'PF'], A.ablation.map(a => [a.rule, a.n, pc(a.d_net), pc(a.d_train), pc(a.d_test), a.pf.toFixed(2)])));
    root.append(h('div', {class: 'note'}, 'Δ ঋণাত্মক = শর্তটি লাভ বাড়াচ্ছে (রাখুন); Δ ধনাত্মক = শর্ত বন্ধ করলে ভালো হতো (শর্তটি ক্ষতিকর/অকেজো হতে পারে)।'));
  }
  if (A.filters.length) {
    root.append(h('h3', {}, 'ফিল্টার মাইনিং (আসল ইঞ্জিনে যাচাই)'));
    root.append(tbl(['অতিরিক্ত শর্ত', 'ট্রেড', 'TRAIN Δ', 'TEST Δ', 'রায়'], A.filters.map(f => [f.rule, f.n, pc(f.d_train), pc(f.d_test), f.chosen ? '✔ নির্বাচিত' : (f.ok ? '✓ ভালো' : '✗')])));
  }
  const W = A.wfo;
  root.append(h('h3', {}, 'রোলিং Walk-Forward (অদেখা সময়ে পরীক্ষা)'));
  root.append(tbl(['ট্রেন', 'টেস্ট', 'OOS নেট (টিউন)', 'OOS নেট (বর্তমান)', 'ট্রেড'], W.windows.map(w => [w.train, w.test, pc(w.net_oos), pc(w.net_base), w.n_oos])));
  root.append(h('div', {class: 'note', html: 'মোট OOS: <b>' + pc(W.oos.net) + '</b> (PF ' + W.oos.pf.toFixed(2) + ') বনাম বর্তমান <b>' + pc(W.base.net) + '</b> | WFE ' + (W.wfe * 100).toFixed(0) + '%'}));
  if (A.changes.length) {
    root.append(h('h3', {}, 'সাজেশন'));
    root.append(tbl(['প্যারামিটার', 'এখন', 'সাজেশন'], A.changes.map(c => [c.label, JSON.stringify(c.from), '<b>' + JSON.stringify(c.to) + '</b>'])));
    root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, h('button', {class: 'btn p', on: {click: () => {
      if (!confirm('v2 সাজেশন Strategy ট্যাবে বসানো হবে। এগিয়ে যাবেন?')) return;
      A.apply.forEach(x => setp(x.path, x.value));
      renderBuild(); showTab('build'); $('#status').textContent = '✓ v2 সাজেশন প্রয়োগ হয়েছে — Run দিয়ে দেখুন';
    }}}, '✔ v2 সাজেশন প্রয়োগ করুন'))));
  }
  (A.warnings || []).forEach(w => root.append(h('div', {class: 'note'}, '⚠ ' + w)));
}

let AIR2 = null;
let AIR = null, AIOPT = {mode: 'full', case: 0, reqs: []};
function renderAI() {
  const root = $('#t-ai'); root.innerHTML = '';
  root.append(h('div', {class: 'h2'}, '🧠 AI Advisor'));
  root.append(h('div', {class: 'note'}, 'আপনার বর্তমান স্ট্র্যাটেজি নিয়ে একে একে SL, টার্গেট, ট্রেইলিং, ব্রেকইভেন, T1/T2, স্ট্রাইক, টাইম-স্টপ, এন্ট্রি-সময় ও ট্রেড-সংখ্যার প্রতিটি মান আসল ব্যাকটেস্ট ইঞ্জিনে চালিয়ে মাপে (খরচ, স্লিপেজ, ট্রেইল সব ধরে)। বাছাই হয় শুধু TRAIN অংশে; TEST অংশ আলাদা রেখে যাচাই হয়। এটা কোনো ভবিষ্যদ্বাণী নয় — পুরনো ডেটায় কোন সেটিং বেশি স্থিতিশীল ছিল তার গাণিতিক সাজেশন।'));
  const on = S.cases.map((c, i) => [i, c.name, c.on]).filter(x => x[2]);
  if (!on.length) { root.append(h('div', {class: 'note'}, 'কোনো Case চালু নেই।')); return; }
  if (!on.some(x => x[0] === AIOPT.case)) AIOPT.case = on[0][0];
  const cs = h('select'); on.forEach(([i, n]) => cs.append(h('option', {value: i}, n))); cs.value = AIOPT.case; cs.addEventListener('change', () => AIOPT.case = parseInt(cs.value));
  const ms = h('select', {}, h('option', {value: 'quick'}, 'দ্রুত (SL, টার্গেট, ট্রেইল, স্ট্রাইক)'), h('option', {value: 'full'}, 'পূর্ণ (T1/T2, সময়, ট্রেড-সংখ্যা সহ)'));
  ms.value = AIOPT.mode; ms.addEventListener('change', () => AIOPT.mode = ms.value);
  root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, F('কোন Case', cs), F('মোড', ms),
    h('button', {class: 'btn g', style: 'align-self:flex-end', on: {click: async () => {
      const r = await runJob({kind: 'advise', settings: S, opts: {mode: AIOPT.mode, case: AIOPT.case, req: AIOPT.reqs.map(x => x.text).join('\n')}}, 'AI বিশ্লেষণ');
      if (r) { AIR = r; renderAI(); }
    }}}, '🧠 বিশ্লেষণ চালান'),
    h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, 'সময় লাগে: প্রায় ৪০–১৭০টি পূর্ণ ব্যাকটেস্ট। ফোনে কয়েক মিনিট হতে পারে; ডেটা বেশি হলে আগে Data ট্যাবে তারিখ-সীমা ছোট করুন।'))));
  root.append(h('div', {class: 'card'}, h('div', {class: 'body'},
    h('button', {class: 'btn p', on: {click: async () => {
      const r = await runJob({kind: 'advise2', settings: S, opts: {case: AIOPT.case}}, 'AI v2 গভীর বিশ্লেষণ');
      if (r) { AIR2 = r; renderAI(); }
    }}}, '🔬 গভীর বিশ্লেষণ (v2)'),
    h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, 'রুল অ্যাব্লেশন (কোন এন্ট্রি-শর্ত আসলে কাজ করছে), ফিল্টার মাইনিং, স্মার্ট সার্চ ও রোলিং Walk-Forward। কমপক্ষে ~৫০ ট্রেড দরকার।'))));
  if (AIR2) drawAI2(root, AIR2);
  /* ---- অনুরোধ বাক্স ---- */
  const ta = h('textarea', {rows: 2, placeholder: 'যেমন: ড্রডাউন ৩০% কমাও  |  লাভ বাড়াও  |  ট্রেইল চাই না  |  স্ট্রাইক বদলিও না', style: 'width:100%;flex:1 1 100%;background:#0b130f;color:inherit;border:1px solid var(--line);border-radius:10px;padding:8px;font:inherit'});
  const send = async txt => {
    txt = (txt || '').trim(); if (!txt) return;
    AIOPT.reqs.push({text: txt});
    const r = await runJob({kind: 'advise', settings: S, opts: {mode: AIOPT.mode, case: AIOPT.case, last: txt, req: AIOPT.reqs.map(x => x.text).join('\n')}}, 'AI অনুরোধ');
    if (!r) { AIOPT.reqs.pop(); return; }
    const g = r.goal_rep || {};
    AIOPT.reqs[AIOPT.reqs.length - 1].sum = {dd: g.dd_change_train, ddt: g.dd_change_test, net: g.net_change_train, nett: g.net_change_test, ok: g.target_met};
    AIR = r; renderAI();
  };
  const chips = ['ড্রডাউন একটু কমাও', 'ড্রডাউন ৩০% কমাও', 'লাভ বাড়াও', 'win rate বাড়াও', 'ট্রেইল চাই না', 'স্ট্রাইক বদলিও না', 'ITM চাই না', 'ট্রেড সংখ্যা কমিও না'].map(c => h('button', {class: 'btn s', style: 'padding:4px 9px;font-size:11.5px', on: {click: () => { ta.value = (ta.value ? ta.value + ', ' : '') + c; }}}, c));
  root.append(h('div', {class: 'h2'}, '💬 অ্যাডভাইজারকে বলুন'));
  root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, ta, h('div', {style: 'display:flex;flex-wrap:wrap;gap:5px;flex:1 1 100%'}, chips),
    h('button', {class: 'btn g', on: {click: () => { const t = ta.value; ta.value = ''; send(t); }}}, '➤ পাঠান ও সাজেশন আপডেট নিন'),
    AIOPT.reqs.length ? h('button', {class: 'btn s', on: {click: () => { AIOPT.reqs = []; AIR = null; renderAI(); }}}, '✕ সব অনুরোধ মুছুন') : null,
    h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, 'আপনার লেখা কীওয়ার্ড-নিয়ম দিয়ে বোঝা হয় (বাংলা/English), তারপর আসল ব্যাকটেস্ট আবার চালিয়ে লক্ষ্য অনুযায়ী সাজেশন বদলায় — এটা ChatGPT-ধাঁচের স্বাধীন AI নয়, তাই সহজ ছোট বাক্যে লিখুন। অনুরোধগুলো জমা হয়ে একসাথে কাজ করে; একবারে একাধিক লেখা যায় (কমা দিয়ে)। যা বুঝিনি তা নিচে দেখাবে।'))));
  if (AIOPT.reqs.length) {
    root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, h('div', {style: 'flex:1 1 100%', html: AIOPT.reqs.map((q, i) => '<div style="margin:3px 0">🗣 <b>' + (i + 1) + '.</b> ' + q.text.replace(/</g, '&lt;') + (q.sum ? ' <span class="pill">TRAIN DD ' + (q.sum.dd > 0 ? '+' : '') + (q.sum.dd || 0).toFixed(0) + '%' + (q.sum.ddt != null ? ' | TEST DD ' + (q.sum.ddt > 0 ? '+' : '') + q.sum.ddt.toFixed(0) + '%' : '') + '</span>' : '') + '</div>').join('')}))));
  }
  const A = AIR; if (!A) return;
  if (A.req && (A.req.understood.length || A.req.ignored.length)) {
    root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, h('div', {class: 'note', style: 'flex:1 1 100%;margin:0', html:
      (A.req.understood.length ? '<b>আমি যা বুঝেছি:</b><br>' + A.req.understood.map(x => '✓ ' + x).join('<br>') : '') +
      (A.req.ignored.length ? '<br><b style="color:#f5c542">যা বুঝিনি (বাদ দিয়েছি):</b><br>' + A.req.ignored.map(x => '? ' + x.replace(/</g, '&lt;')).join('<br>') : '')}))));
  }
  const GR = A.goal_rep;
  if (GR) {
    const sg = v => v == null ? '–' : (v > 0 ? '+' : '') + v.toFixed(0) + '%';
    const cdd = v => v == null ? '–' : '<span class="' + (v <= 0 ? 'pos' : 'neg') + '">' + sg(v) + '</span>';
    const cnt = v => v == null ? '–' : '<span class="' + (v >= 0 ? 'pos' : 'neg') + '">' + sg(v) + '</span>';
    root.append(h('h3', {}, 'আপনার অনুরোধের ফল (এখনকার সেটিংসের তুলনায়)'));
    root.append(tbl(['', 'TRAIN', 'TEST (যাচাই)'], [
      ['ড্রডাউন বদল (− মানে কমেছে, ভালো)', cdd(GR.dd_change_train), cdd(GR.dd_change_test)],
      ['নেট লাভ বদল', cnt(GR.net_change_train), cnt(GR.net_change_test)]]));
    root.append(h('div', {class: 'note'}, 'Win%: ' + GR.win_from.toFixed(0) + '% → ' + GR.win_to.toFixed(0) + '% | PF: ' + f2(GR.pf_from) + ' → ' + f2(GR.pf_to) + ' | ট্রেড: ' + GR.trades_from + ' → ' + GR.trades_to + (GR.target != null ? ' | লক্ষ্য ড্রডাউন ' + inr(GR.target) + (GR.target_met ? ' ✓ পূরণ' : ' ✗ পূরণ হয়নি') : '')));
  }
  const V = A.validation, col = {pass: '#26c281', keep: '#8fb7ff', warn: '#f5c542', fail: '#ff5d5d'}[V.level] || '#8b96a8';
  const ttl = {pass: '✅ সাজেশন যাচাইয়ে টিকেছে', keep: 'ℹ️ বর্তমান সেটিং-ই ভালো', warn: '⚠️ সতর্কতাসহ সাজেশন', fail: '❌ যাচাইয়ে টেকেনি'}[V.level];
  root.append(h('div', {class: 'card', style: 'border-color:' + col}, h('div', {class: 'body'},
    h('div', {style: 'flex:1 1 100%'}, h('b', {style: 'color:' + col}, ttl), h('span', {class: 'pill'}, A.case)),
    h('div', {class: 'note', style: 'flex:1 1 100%;margin:0', html: V.msgs.map(m => '• ' + m).join('<br>')}),
    h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, A.runs + 'টি রান, ' + A.secs.toFixed(0) + ' সেকেন্ড' + (A.split_date ? ' | TRAIN/TEST ভাগ: ' + A.split_date : '')))));
  const rowsC = [];
  [['TRAIN', 'train'], ['TEST (যাচাই)', 'test'], ['সব', 'all']].forEach(([t, k]) => {
    rowsC.push(['<b>' + t + '</b> এখন', A.base[k].n, pn(A.base[k].net), f2(A.base[k].pf), inr(A.base[k].dd), A.base[k].win.toFixed(0) + '%']);
    rowsC.push(['<b>' + t + '</b> সাজেশন', A.final[k].n, pn(A.final[k].net), f2(A.final[k].pf), inr(A.final[k].dd), A.final[k].win.toFixed(0) + '%']);
  });
  root.append(h('h3', {}, 'আগে বনাম পরে'));
  root.append(tbl(['', 'ট্রেড', 'নেট ₹', 'PF', 'সর্বোচ্চ DD', 'Win%'], rowsC));
  if (A.changes.length) {
    root.append(h('h3', {}, 'সাজেশন'));
    root.append(tbl(['প্যারামিটার', 'এখন', 'সাজেশন', 'TRAIN Δ', 'TEST Δ', 'স্থিতিশীলতা'],
      A.changes.map(c => [c.label, c.from, '<b>' + c.to + '</b>', pn(c.d_train), (c.test_ok ? '✓ ' : '✗ ') + pn(c.d_test), c.plateau])));
    const bt = h('button', {class: 'btn p', on: {click: () => {
      if (!confirm('সাজেশনগুলো Strategy ট্যাবের সেটিংসে বসানো হবে (পরে আবার বদলানো যাবে)। এগিয়ে যাবেন?')) return;
      A.apply.forEach(x => setp(x.path, x.value));
      renderBuild(); showTab('build'); $('#status').textContent = '✓ সাজেশন প্রয়োগ হয়েছে — এখন Run দিয়ে আসল ফল দেখুন';
    }}}, '✔ সব সাজেশন প্রয়োগ করুন');
    root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, bt, h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, 'প্রয়োগের পর Run দিন; ফল এই পাতার সাথে মিলবে। ✗ চিহ্নিত পরিবর্তনগুলো TEST-এ উল্টো ফল দিয়েছে — চাইলে Strategy ট্যাবে সেগুলো আগের মানে ফিরিয়ে দিন।'))));
  }
  if (A.tg) {
    const T = A.tg;
    root.append(h('h3', {}, 'T1 / T2 আংশিক বুকিং (' + T.lots_used + ' লটে মাপা)'));
    if (!T.changes.length) root.append(h('div', {class: 'note'}, 'আংশিক বুকিং চালু করলে উন্নতি পাওয়া যায়নি, বা এখনকার সেটিং-ই ভালো।'));
    else {
      root.append(h('div', {class: 'note', html: T.changes.map(x => '• ' + x).join('<br>') + '<br>TRAIN Δ ' + inr(T.d_train) + ' | TEST Δ ' + inr(T.d_test) + (T.d_test < 0 ? '<br><b style="color:#f5c542">⚠ TEST-এ উল্টো ফল — সতর্ক থাকুন</b>' : '')}));
      if (T.settings) root.append(h('button', {class: 'btn s', on: {click: () => {
        if (!confirm('T1/T2 সেটিং বসবে এবং লট ' + T.lots_used + ' করা হবে (T1/T2 ভাগ করতে ২+ লট লাগে)। চালাবেন?')) return;
        S.cases[A.case_idx].tg = T.settings; S.general.lots = T.lots_used; persist(); renderBuild(); showTab('build');
      }}}, 'T1/T2 প্রয়োগ করুন'));
    }
  }
  const M = A.mc.final, B = A.mc.base;
  if (M) {
    root.append(h('h3', {}, 'ঝুঁকি ও নির্ভরযোগ্যতা (সাজেশনের সব ট্রেড, ' + M.N + 'বার বুটস্ট্র্যাপ)'));
    root.append(tbl(['পরিমাপ', 'সাজেশন', 'এখনকার'], [
      ['ট্রেডপ্রতি গড় নেট ₹', inr(M.exp), B ? inr(B.exp) : '–'],
      ['৯৫% আস্থা-সীমা (ট্রেডপ্রতি)', inr(M.exp_lo) + ' … ' + inr(M.exp_hi), B ? inr(B.exp_lo) + ' … ' + inr(B.exp_hi) : '–'],
      ['গড় নেট > ০ হওয়ার সম্ভাবনা', M.p_pos.toFixed(0) + '%', B ? B.p_pos.toFixed(0) + '%' : '–'],
      ['t-স্ট্যাট (≥ ২ ভালো)', f2(M.t), B ? f2(B.t) : '–'],
      ['সম্ভাব্য সর্বোচ্চ DD (মধ্যম)', inr(M.dd_p50), B ? inr(B.dd_p50) : '–'],
      ['সম্ভাব্য সর্বোচ্চ DD (খারাপ ৫%)', inr(M.dd_p95), B ? inr(B.dd_p95) : '–'],
      ['DD ২০%-এর মধ্যে রাখতে মূলধন ≈', inr(M.cap20), B ? inr(B.cap20) : '–']]));
    root.append(h('div', {class: 'note'}, 'ট্রেডের ক্রম এলোমেলো করে বহুবার সাজিয়ে দেখা: ব্যাকটেস্টে যে ড্রডাউন এসেছে সেটা ভাগ্যে কম ছিল; বাস্তবে "খারাপ ৫%" মানের কাছাকাছি ড্রডাউনের জন্য তৈরি থাকুন।'));
  }
  const D = A.diag;
  if (D.mae_w) {
    root.append(h('h3', {}, 'প্রমাণ (SL / টার্গেট নির্বাচনের পেছনের সংখ্যা)'));
    root.append(h('div', {class: 'note', html: 'জেতা ট্রেডের ৯০% MAE = <b>' + D.mae_w[2].toFixed(1) + '%</b> → এর চেয়ে কড়া SL রাখলে ১০%-এর বেশি জেতা ট্রেড কেটে যায়।<br>জেতা ট্রেডে শিখর থেকে লাভ ফেরত যাওয়ার মধ্যম ≈ <b>' + (D.give_med || 0).toFixed(1) + '%</b> → ট্রেইলের দূরত্বের সূত্র।'}));
    const rk = Object.entries(D.reach);
    if (rk.length) root.append(tbl(['প্রিমিয়াম ছুঁয়েছে'].concat(rk.map(([k]) => '+' + k + '%')), [['ট্রেডের %'].concat(rk.map(([, v]) => v.toFixed(0) + '%'))]));
    root.append(h('div', {class: 'note'}, 'এটি পথ-ক্রম ছাড়া (SL আগে লেগেছে কি না ধরা নেই) — T1 কোথায় রাখবেন তার মোটামুটি ধারণা; চূড়ান্ত মান ওপরের আসল-রান টেবিল থেকে।'));
  }
  const bad = D.buckets.filter(b => b.t <= -1.5), good = D.buckets.filter(b => b.t >= 2);
  if (bad.length || good.length) {
    root.append(h('h3', {}, 'যেসব ভাগ উল্লেখযোগ্য ভালো/খারাপ (অন্তত ২০ ট্রেড, t-স্ট্যাট দিয়ে)'));
    root.append(tbl(['ভাগ', 'মান', 'ট্রেড', 'গড় ₹', 'নেট ₹', 't'], bad.concat(good).map(b => [b.g, b.k, b.n, pn(b.avg), pn(b.net), f2(b.t)])));
    root.append(h('div', {class: 'note'}, 'শুধু তথ্য: এগুলো নিজে থেকে প্রয়োগ হয় না। অনেক ভাগ একসাথে দেখলে কিছু "উল্লেখযোগ্য" দেখানো কাকতালীয় হতে পারে, তাই ফিল্টার বসালে আবার যাচাই করুন।'));
  }
  root.append(h('h3', {}, 'ধাপে ধাপে বিস্তারিত (প্রতিটি মান আসল ইঞ্জিনে চালানো)'));
  A.stages.forEach(st => {
    const rws = st.rows.map(r => [(r.pick ? '⭐ ' : '') + (r.cur ? '(এখন) ' : '') + r.v, r.tr.n, pn(r.tr.net), f2(r.tr.pf), inr(r.tr.dd), r.sc == null ? '–' : f2(r.sc), r.te.n, pn(r.te.net), f2(r.te.pf)]);
    root.append(h('details', {class: 'card'}, h('summary', {}, st.label + (st.picked ? ' → ' + st.rows.find(r => r.pick).v : ' (বদল নেই)')),
      h('div', {class: 'body'}, h('div', {style: 'flex:1 1 100%'}, tbl(['মান', 'TRAIN ট্রেড', 'নেট', 'PF', 'DD', 'স্কোর', 'TEST ট্রেড', 'নেট', 'PF'], rws, '320px')),
        h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, (st.note ? st.note + ' | ' : '') + (st.picked ? 'স্থিতিশীলতা: ' + st.plateau : 'বর্তমান মান থেকে স্পষ্ট উন্নতি নেই।')))));
  });
  root.append(h('div', {class: 'note'}, A.score_note));
  root.append(h('h3', {}, 'সতর্কতা'));
  root.append(h('div', {class: 'note', html: A.warnings.map(m => '• ' + m).join('<br>') + '<br>• <i>এটি শিক্ষামূলক তথ্য, আর্থিক পরামর্শ নয়।</i>'}));
}


/* ---------- 📄 Paper (রিপ্লে + লাইভ) ---------- */
let PP = {mode: 'replay', from: '', to: '', step: 30, delay: 0, band: 3, timer: null, last: null, test: null, sig: '', run: null, stopping: false};
function ppRun() { return !!(PP.last && PP.last.running); }
function paperPoll() {
  clearInterval(PP.timer);
  const tick = async () => {
    if ($('#t-paper').hidden) { clearInterval(PP.timer); return; }
    try {
      const r = await api('/api/paper/status'); PP.last = r.sess;
      const rn = ppRun();
      if (PP.run !== rn) { PP.run = rn; PP.stopping = false; drawPaperForm(); loadPaperList(); }
      drawPaperStat();
      if (!rn) clearInterval(PP.timer);
    } catch (e) {}
  };
  PP.timer = setInterval(tick, PP.stopping ? 600 : 1500); tick();
}
function renderPaper() {
  const root = $('#t-paper'); root.innerHTML = '';
  root.append(h('div', {class: 'h2'}, '📄 Paper Trading'),
    h('div', {class: 'note'}, 'আপনার বর্তমান স্ট্র্যাটেজি (Strategy ট্যাবের সেটিংস) একই ব্যাকটেস্ট-কোডে চালিয়ে "এখন পর্যন্ত জানা ডেটা" দিয়ে ট্রেড দেখায়; আসল অর্ডার যায় না। রিপ্লে = পুরনো দিন লাইভের মতো চালানো (ফিড ছাড়া); লাইভ = Upstox থেকে আজকের বাজার।'),
    h('div', {id: 'pp-form'}), h('div', {id: 'pp-stat'}), h('div', {id: 'pp-list'}));
  api('/api/paper/status').then(r => { PP.last = r.sess; PP.run = ppRun(); drawPaperForm(); drawPaperStat(); loadPaperList(); if (PP.run) paperPoll(); });
}
function drawPaperForm() {
  const f = $('#pp-form'); if (!f) return; f.innerHTML = '';
  if (ppRun()) return;
  const i = META.info, dEnd = i.spot_to, d0 = new Date(dEnd); d0.setDate(d0.getDate() - 28);
  if (!PP.from) PP.from = d0.toISOString().slice(0, 10); if (!PP.to) PP.to = dEnd;
  const ms = h('select', {}, h('option', {value: 'replay'}, '🔁 রিপ্লে (পুরনো দিন)'), h('option', {value: 'live'}, '🟢 লাইভ (আজকের বাজার)')); ms.value = PP.mode;
  ms.addEventListener('change', () => { PP.mode = ms.value; drawPaperForm(); });
  const box = h('div', {class: 'body'}, F('মোড', ms));
  if (PP.mode === 'replay') {
    const a = h('input', {type: 'date', value: PP.from}), b = h('input', {type: 'date', value: PP.to});
    a.addEventListener('change', () => PP.from = a.value); b.addEventListener('change', () => PP.to = b.value);
    const sp = h('select', {}, h('option', {value: 0}, 'তাৎক্ষণিক'), h('option', {value: 0.05}, 'দ্রুত'), h('option', {value: 0.4}, 'ধীরে')); sp.value = PP.delay;
    sp.addEventListener('change', () => PP.delay = parseFloat(sp.value));
    const stp = h('select', {}, h('option', {value: 30}, 'প্রতি ৩০ মিনিটে (দ্রুত)'), h('option', {value: 15}, 'প্রতি ১৫ মিনিটে'), h('option', {value: 0}, 'প্রতি চার্ট-বারে (ধীর)')); stp.value = PP.step;
    stp.addEventListener('change', () => PP.step = parseInt(stp.value));
    box.append(F('শুরু', a), F('শেষ', b), F('টিক-ধাপ', stp), F('গতি', sp));
  } else {
    const pv = h('select', {}, h('option', {value: 'upstox'}, 'Upstox'), h('option', {value: 'zerodha', disabled: true}, 'Zerodha (শীঘ্রই)'), h('option', {value: 'angel', disabled: true}, 'Angel One (শীঘ্রই)'), h('option', {value: 'dhan', disabled: true}, 'Dhan (শীঘ্রই)'));
    const bn = h('input', {type: 'number', value: PP.band, min: 1, max: 8}); bn.addEventListener('change', () => PP.band = parseInt(bn.value) || 3);
    const out = h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'});
    const showTest = () => { out.innerHTML = !PP.test ? '' : PP.test === 'run' ? 'টেস্ট চলছে…' : PP.test.checks.map(c => (c.ok ? '✅ ' : '❌ ') + c.name + ' — ' + String(c.detail).replace(/</g, '&lt;')).join('<br>') + (PP.test.all_ok ? '<br><b>সব ঠিক — লাইভ শুরু করা যায়।</b>' : '<br><b style="color:#f5c542">কিছু ধাপ ব্যর্থ। ছুটি/বাজার বন্ধে ক্যান্ডেল ০ স্বাভাবিক; নয়তো এই লেখা আমাকে পাঠান।</b>'); };
    box.append(F('ব্রোকার ফিড', pv), F('ATM-এর দুই পাশে স্ট্রাইক', bn),
      h('button', {class: 'btn s', on: {click: async () => { PP.test = 'run'; showTest(); try { PP.test = await api('/api/live/test', {inst: (META.info.inst || 'N50')}); } catch (e) { PP.test = {checks: [{name: 'ত্রুটি', ok: false, detail: String(e)}]}; } showTest(); }}}, '🔌 লাইভ কানেকশন টেস্ট'), out);
    showTest();
  }
  box.append(h('button', {class: 'btn g', on: {click: async () => {
    const r = await api('/api/paper/start', {mode: PP.mode, settings: S, from: PP.from, to: PP.to, delay: PP.delay, step: PP.step, band: PP.band, feed: 'upstox', inst: META.info.inst});
    if (r.error) { alert(r.error); return; }
    PP.last = {running: true, status: 'running', msg: 'শুরু হচ্ছে…', stats: {n: 0, win: 0, pf: 0, dd: 0}, trades: [], events: [], live_pnl: 0}; PP.run = true; PP.sig = '';
    drawPaperForm(); drawPaperStat(); paperPoll();
  }}}, '▶ পেপার শুরু'));
  f.append(h('div', {class: 'card'}, box));
}
function drawPaperStat() {
  const root = $('#pp-stat'); if (!root) return;
  const P = PP.last;
  const sig = JSON.stringify([P && P.status, P && P.msg, P && P.now, P && P.stats && P.stats.n, P && P.live_pnl, P && P.open && P.open.net, P && P.events && P.events.length, PP.stopping, P && P.days_done]);
  if (sig === PP.sig) return; PP.sig = sig;
  root.innerHTML = '';
  if (!P) return;
  const st = P.stats, run = P.running;
  const col = {running: '#26c281', done: '#8fb7ff', stopped: '#f5c542', error: '#ff5d5d'}[P.status] || '#8b96a8';
  const stopBtn = run ? h('button', {class: 'btn s', on: {click: async e => { PP.stopping = true; e.target.disabled = true; e.target.textContent = '… থামছে'; await api('/api/paper/stop', {}); paperPoll(); }}}, PP.stopping ? '… থামছে' : '■ থামান')
    : h('button', {class: 'btn s', on: {click: async () => { await api('/api/paper/reset', {}); PP.last = null; PP.sig = ''; PP.run = false; drawPaperStat(); loadPaperList(); }}}, '✕ সেশন বন্ধ করুন');
  root.append(h('div', {class: 'card', style: 'border-color:' + col}, h('div', {class: 'body'},
    h('div', {style: 'flex:1 1 100%'}, h('b', {style: 'color:' + col}, PP.stopping ? '… থামছে' : ({running: '● চলছে', done: '✔ শেষ', stopped: '■ থামানো', error: '✖ ত্রুটি'}[P.status] || P.status)), h('span', {class: 'pill'}, P.mode === 'live' ? 'লাইভ' : 'রিপ্লে'),
      h('span', {class: 'pill'}, (P.day || '') + (P.now ? ' ' + String(Math.floor((P.now % 1440) / 60)).padStart(2, '0') + ':' + String(P.now % 60).padStart(2, '0') : ''))),
    h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, (P.msg || '') + (P.mode === 'replay' && P.n_days ? ' | দিন ' + P.days_done + '/' + P.n_days : '')), stopBtn)));
  root.append(h('div', {class: 'kpis'},
    h('div', {class: 'kpi hero ' + (P.live_pnl >= 0 ? 'up' : 'dn')}, h('small', {}, 'মোট P&L (খোলা ট্রেড সহ)'), h('b', {class: cls(P.live_pnl)}, inr(P.live_pnl))),
    [['বন্ধ ট্রেড', st.n], ['Win%', st.win.toFixed(0) + '%'], ['PF', f2(st.pf)], ['সর্বোচ্চ DD', inr(st.dd)]].map(([k, v]) => h('div', {class: 'kpi'}, h('small', {}, k), h('b', {}, String(v))))));
  if (P.open) {
    const o = P.open;
    root.append(h('h3', {}, '🟢 চলমান ট্রেড'));
    root.append(tbl(['ট্রেড', 'ইন', 'এন্ট্রি', 'বাকি লট', 'SL', 'টার্গেট', 'MTM নেট ₹'], [[o.side, o.in, o.entry, o.rem + '/' + o.lots, o.slx == null ? '–' : o.slx.toFixed(1), o.tpx == null ? '–' : o.tpx.toFixed(1), pn(o.net)]]));
    root.append(h('div', {class: 'note'}, 'MTM = এখনই বেরোলে যা হতো (খরচ সহ)। SL/টার্গেট = এই মুহূর্তের স্তর।'));
  }
  if (P.events && P.events.length) {
    root.append(h('h3', {}, 'ঘটনা-লগ'));
    root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, h('div', {class: 'scroll', style: 'flex:1 1 100%;max-height:230px', html: P.events.slice(-40).reverse().map(e => '<div style="margin:2px 0"><span style="color:var(--mut)">' + e.day.slice(5) + ' ' + e.t + '</span> <b style="color:' + ({ENTRY: '#7fe0a8', EXIT: '#ff9aa4', PARTIAL: '#f5c542'}[e.k] || '#fff') + '">' + e.k + '</b> ' + e.msg + '</div>').join('')}))));
  }
  if (P.trades && P.trades.length) {
    root.append(h('h3', {}, 'বন্ধ হওয়া ট্রেড (' + P.trades.length + ')'));
    root.append(tbl(['তারিখ', 'ট্রেড', 'ইন', 'আউট', 'কারণ', 'নেট ₹'], P.trades.slice(-80).reverse().map(t => [t.date.slice(5), t.side, t.in, t.out, legTxt(t), pn(t.net)]), '320px'));
    if (P.mode === 'replay' && P.status === 'done') {
      const cmp = h('div', {class: 'note'});
      root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, h('button', {class: 'btn p', on: {click: async () => {
        const s2 = JSON.parse(JSON.stringify(S)); s2.general.from = P.from; s2.general.to = P.to;
        const r = await runJob({kind: 'run', settings: s2}, 'ব্যাকটেস্ট (তুলনা)'); if (!r) return;
        const a = r.stats.all, same = a.n === st.n && Math.abs(a.net - st.net) < 1;
        cmp.innerHTML = same ? '✅ <b>পেপার ও ব্যাকটেস্ট হুবহু মিলেছে</b> (' + a.n + ' ট্রেড, নেট ' + inr(a.net) + ')।' : '⚠ মেলেনি: ব্যাকটেস্ট ' + a.n + ' ট্রেড / ' + inr(a.net) + ', পেপার ' + st.n + ' ট্রেড / ' + inr(st.net) + ' (সেটিংস বদলেছে কিনা দেখুন)।';
      }}}, '⚖ ব্যাকটেস্টের সাথে মিলিয়ে দেখুন'), cmp)));
    }
  }
}
async function loadPaperList() {
  const root = $('#pp-list'); if (!root) return; root.innerHTML = '';
  if (ppRun()) return;
  try {
    const l = (await api('/api/paper/list')).list; if (!l || !l.length) return;
    root.append(h('h3', {}, 'আগের সেশন'));
    const box = h('div', {class: 'card'}, h('div', {class: 'body'}));
    l.slice(0, 8).forEach(x => box.firstChild.append(h('div', {style: 'flex:1 1 100%;display:flex;align-items:center;gap:8px;justify-content:space-between'},
      h('div', {style: 'font-size:12.5px'}, h('b', {}, (x.mode === 'live' ? '🟢 ' : '🔁 ') + (x.from || '').slice(5) + ' → ' + (x.to || '').slice(5)), h('div', {class: 'note', style: 'margin:0', html: x.n + ' ট্রেড | ' + pn(x.net)})),
      h('button', {class: 'btn s', on: {click: async () => { const r = await api('/api/paper/load', {id: x.id}); if (r.error) alert(r.error); else { PP.sig = ''; const s = await api('/api/paper/status'); PP.last = s.sess; drawPaperStat(); window.scrollTo(0, 0); } }}}, 'খুলুন'))));
    root.append(box);
  } catch (e) {}
}

/* ---------- ট্যাব ---------- */
function showTab(t) {
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  for (const k of ['build', 'res', 'tune', 'ai', 'paper', 'pre', 'data']) $('#t-' + k).hidden = k !== t;
  if (t === 'res') drawEq();
  if (t === 'data') renderData();
  if (t === 'ai') renderAI();
  if (t === 'paper') { renderPaper(); paperPoll(); }
  window.scrollTo(0, 0);
}
document.querySelectorAll('nav button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
$('#run').addEventListener('click', async () => {
  const res = await runJob({kind: 'run', settings: S}, 'ব্যাকটেস্ট');
  if (res) { LASTRUN = JSON.stringify(S); renderResults(res); showTab('res'); }
});
window.addEventListener('resize', () => { if (!$('#t-res').hidden) drawEq(); });

(async function init() {
  try {
    META = await api('/api/meta');
    const i = META.info; setBase(i.base || 5);
    setHeader(i);
    let saved = null; try { saved = JSON.parse(localStorage.getItem('ab_S2')); } catch (e) {}
    S = fixSettings(saved || META.defaults);
    if (S.general.tf % (i.base || 5)) S.general.tf = i.base || 5;
    renderBuild(); renderPresets();
  } catch (e) {
    $('#dinfo').innerHTML = '<span class="pl" style="color:#ff8a8a">ত্রুটি: ' + e.message + '</span>';
  }
})();
</script>
</body>
</html>
'''

if __name__ == "__main__":
    main()
