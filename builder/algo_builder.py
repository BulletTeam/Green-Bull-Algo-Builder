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
import uuid
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
    ("swh", "Swing High (নিশ্চিত, ৩ বার)", "প্রাইস অ্যাকশন / SMC"), ("swl", "Swing Low (নিশ্চিত, ৩ বার)", "প্রাইস অ্যাকশন / SMC"),
    ("bos", "BOS (+1 ওপরে ভাঙল / -1 নিচে)", "প্রাইস অ্যাকশন / SMC"), ("choch", "CHoCH (+1 বুলিশ / -1 বেয়ারিশ রিভার্সাল)", "প্রাইস অ্যাকশন / SMC"),
    ("fvgb", "বুলিশ FVG সাইজ % (এই বারে)", "প্রাইস অ্যাকশন / SMC"), ("fvgs", "বেয়ারিশ FVG সাইজ % (এই বারে)", "প্রাইস অ্যাকশন / SMC"),
    ("insb", "Inside Bar (1/0)", "প্রাইস অ্যাকশন / SMC"), ("nr7", "NR7 — ৭ বারের সবচেয়ে ছোট রেঞ্জ (1/0)", "প্রাইস অ্যাকশন / SMC"),
    ("rvol", "Relative Volume (Volume ÷ Vol SMA)", "প্রাইস অ্যাকশন / SMC"),
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

    # ---- প্রাইস অ্যাকশন / SMC (কোনো রিপেইন্ট নেই: সুইং ৩ বার পরে নিশ্চিত হয়, দিন-ভিত্তিক রিসেট) ----
    NSW = 3
    swh = np.full(n, np.nan)
    swl = np.full(n, np.nan)
    bos = np.zeros(n)
    choch = np.zeros(n)
    fvgb = np.zeros(n)
    fvgs = np.zeros(n)
    insb = np.zeros(n)
    nr7 = np.zeros(n)
    day_ = B.day
    hi_, lo_, cl_ = B.h, B.l, B.c
    cur_h = cur_l = np.nan
    trend = 0
    used_h = used_l = False
    for i in range(n):
        if i == 0 or day_[i] != day_[i - 1]:
            cur_h = cur_l = np.nan
            trend = 0
            used_h = used_l = False
            d0 = i
        k = i - NSW                                  # k বারের সুইং এখন নিশ্চিত
        if k - NSW >= d0:
            if hi_[k] == np.max(hi_[k - NSW:i + 1]) and hi_[k] > np.max(hi_[k - NSW:k]):
                cur_h, used_h = hi_[k], False
            if lo_[k] == np.min(lo_[k - NSW:i + 1]) and lo_[k] < np.min(lo_[k - NSW:k]):
                cur_l, used_l = lo_[k], False
        swh[i], swl[i] = cur_h, cur_l
        if not used_h and np.isfinite(cur_h) and cl_[i] > cur_h:
            bos[i] = 1
            used_h = True
            if trend == -1:
                choch[i] = 1
            trend = 1
        elif not used_l and np.isfinite(cur_l) and cl_[i] < cur_l:
            bos[i] = -1
            used_l = True
            if trend == 1:
                choch[i] = -1
            trend = -1
        if i >= d0 + 2:
            if lo_[i] > hi_[i - 2]:
                fvgb[i] = (lo_[i] - hi_[i - 2]) / cl_[i] * 100
            if hi_[i] < lo_[i - 2]:
                fvgs[i] = (lo_[i - 2] - hi_[i]) / cl_[i] * 100
        if i > d0 and hi_[i] < hi_[i - 1] and lo_[i] > lo_[i - 1]:
            insb[i] = 1
        if i >= d0 + 6 and (hi_[i] - lo_[i]) <= np.min(hi_[i - 6:i + 1] - lo_[i - 6:i + 1]) + 1e-12:
            nr7[i] = 1
    S["swh"], S["swl"], S["bos"], S["choch"], S["fvgb"], S["fvgs"], S["insb"], S["nr7"] = swh, swl, bos, choch, fvgb, fvgs, insb, nr7
    with np.errstate(all="ignore"):
        S["rvol"] = B.v / S["volsma"]

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
    size_state = {"cum": 0.0}

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
        _cur = max(G["capital"] + size_state["cum"], 0.2 * G["capital"])
        if G["size_mode"] == "compound":
            L = max(1, min(G["max_lots"], int(G["lots"] * _cur / G["capital"])))
        elif G["size_mode"] == "risk" and sl0 is not None and abs(raw_e - sl0) * lot > 0:
            L = max(1, min(G["max_lots"], int(_cur * G["risk_pct"] / 100.0 // (abs(raw_e - sl0) * lot))))
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
        slh = []
        last_sl = sl
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
            if sl is not None and sl != last_sl:
                slh.append((int(t[jj + 1]) if jj + 1 < nb else s, float(sl)))
                last_sl = sl
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
                    reason=reason, qty=qty, gross=float(gross), costs=float(costs), net=float(gross - costs), is_open=is_open, rem=int(rem), sl_now=(float(sl) if sl is not None else None), slh=slh, sl_init=(float(sl0) if sl0 is not None else None), tp_now=(float(tp) if tp is not None else None),
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
                ii = max(int(np.searchsorted(B.e, te, side="right")) - 1, 0)
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
                te = int(B.e[ii]) + G["delay"] * int(D.base)          # delay>0: দেরিতে এন্ট্রি (স্ট্রেস টেস্ট)
                if now is not None and te > now:
                    break
                if te >= day_end:
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
                "reason": r["reason"], "lots": int(r["qty"] // max(lot, 1)), "qty": r["qty"],
                "gross": round(r["gross"], 2), "costs": round(r["costs"], 2), "net": round(r["net"], 2),
                "ret": round(r["ret"], 2), "spot": round(spot_px, 2), "bars": r["bars"],
                "hold": max(int(D.base), int(r["exit_t"] - te)), "ib": int(ii), "mae": round(r["mae"], 2), "mfe": round(r["mfe"], 2),
                "rret": round(r["rret"], 2),
                "r": round(r["net"] / r["risk"], 2) if r["risk"] else None,
                "dte": int(ex - d), "dow": DOWN[(d + 3) % 7], "legs": r["legs"],
                "open": bool(r["is_open"]), "rem": r["rem"], "slx": r.get("sl_now"), "tpx": r.get("tp_now"),
                "slh": [[hm_str(a), round(b, 2)] for a, b in r.get("slh", [])], "sl0": r.get("sl_init")})
            ntr += 1
            dpnl += r["net"]
            cursor = r["exit_t"]
            size_state["cum"] += r["net"]
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
                    "skipped": skipped, "cases_active": (len(pcases) if PM else len(cases)),
                    "warn": (["আংশিক প্রফিট বুকিং চালু, কিন্তু লট ১ — ভাগ করা যায় না। লট কমপক্ষে ২ দিন।"]
                             if G["lots"] < 2 and any(c["on"] and c["tg"]["on"] for c in st["cases"]) else [])}}
    if light == "trades":
        return {"trades": trades, "meta": out["meta"]}
    if light:
        return out
    out["trades"] = trades
    out["an"] = analyze(trades, cap, getattr(D, "base", BASE_TF))
    try:
        out["an"]["adv"] = advanced_stats(trades, cap, days_list, (SRC_CACHE.get("v") or (None,))[0] if SRC_CACHE.get("D") is D else None, D)
    except Exception as e:
        out["an"]["adv"] = {"err": str(e)}
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



def advanced_stats(trades, cap, days_list, Sx=None, D=None):
    """Sharpe/Sortino/Calmar/SQN, E-ratio, আন্ডারওয়াটার, রেজিম, মার্জিন-ROI"""
    if not trades:
        return {}
    by = {}
    for t in trades:
        by[t["date"]] = by.get(t["date"], 0.0) + t["net"]
    days = list(days_list) if days_list else sorted(by)
    dp = np.array([by.get(d, 0.0) for d in days], float)
    nd = len(dp)
    mu, sd = float(dp.mean()), float(dp.std(ddof=1)) if nd > 1 else 0.0
    dn = dp[dp < 0]
    dsd = float(np.sqrt((np.minimum(dp, 0) ** 2).mean())) if nd else 0.0
    sharpe = mu / sd * np.sqrt(252) if sd > 0 else 0.0
    sortino = mu / dsd * np.sqrt(252) if dsd > 0 else 0.0
    eq = np.cumsum(dp)
    pk = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    ddv = eq - pk
    mdd = float(ddv.min())
    yrs = max(nd / 252.0, 1e-9)
    ann = float(eq[-1]) / yrs
    calmar = ann / abs(mdd) if mdd < 0 else 0.0
    # আন্ডারওয়াটার
    uw = best = 0
    for x in ddv:
        uw = uw + 1 if x < 0 else 0
        best = max(best, uw)
    # SQN (R না থাকলে নেট ব্যবহার)
    rs = np.array([t["r"] for t in trades if t.get("r") is not None], float)
    base_r = rs if len(rs) >= max(10, 0.8 * len(trades)) else np.array([t["net"] for t in trades], float)
    sqn = float(np.sqrt(len(base_r)) * base_r.mean() / base_r.std(ddof=1)) if len(base_r) > 1 and base_r.std(ddof=1) > 0 else 0.0
    # E-ratio = গড় MFE ÷ গড় MAE (শতাংশে, দুটোই ধনাত্মক ধরে)
    mfe = np.array([abs(t["mfe"]) for t in trades], float)
    mae = np.array([abs(t["mae"]) for t in trades], float)
    eratio = float(mfe.mean() / mae.mean()) if mae.mean() > 0 else 0.0
    dep = np.array([t["entry"] * t["qty"] for t in trades], float)
    out = {"sharpe": float(sharpe), "sortino": float(sortino), "calmar": float(calmar), "sqn": sqn, "eratio": eratio,
           "ann_net": ann, "ann_pct": 100.0 * ann / cap if cap else 0.0, "uw_max": int(best), "uw_now": int(uw), "days": nd,
           "avg_deployed": float(dep.mean()), "roi_deployed": float(100.0 * sum(t["net"] for t in trades) / dep.sum()) if dep.sum() > 0 else 0.0,
           "daily_win": float(100.0 * (dp > 0).sum() / max(1, (dp != 0).sum()))}
    # রেজিম
    reg = {}
    if Sx is not None:
        def bucket(name, cuts, labels):
            a = Sx.get(name)
            if a is None:
                return None
            g = {}
            for t in trades:
                try:
                    v = float(a[t["ib"]])
                except Exception:
                    continue
                if not np.isfinite(v):
                    continue
                k = labels[int(np.searchsorted(cuts, v, side="right"))]
                g.setdefault(k, []).append(t)
            rows = []
            for k in labels:
                if k in g:
                    st = calc_stats(g[k], cap)
                    rows.append({"k": k, "n": st["n"], "win": st["win"], "net": st["net"], "pf": st["pf"], "exp": st["exp"]})
            return rows
        try:
            reg["adx"] = bucket("adx", [20, 30], ["ADX<20 (রেঞ্জ)", "ADX 20-30", "ADX>30 (ট্রেন্ড)"])
            a = Sx.get("atr")
            if a is not None:
                vv = np.array([a[t["ib"]] for t in trades], float)
                ok = vv[np.isfinite(vv)]
                if len(ok) >= 9:
                    q1, q2 = np.percentile(ok, [33.3, 66.7])
                    reg["atr"] = bucket("atr", [q1, q2], ["কম অস্থিরতা (ATR)", "মাঝারি অস্থিরতা", "বেশি অস্থিরতা"])
            reg["rsi"] = bucket("rsi", [40, 60], ["RSI<40", "RSI 40-60", "RSI>60"])
        except Exception:
            pass
    # গ্যাপ রেজিম ও টাইম-ডিকে E-ratio ও টপ-৫ DD (স্পট ডেটা D লাগে)
    try:
        if D is not None:
            gg = {}
            for t in trades:
                j = int(np.searchsorted(D.du, day_int(t["date"])))
                if j <= 0 or j >= len(D.du):
                    continue
                gp = (D.dO[j] / D.dC[j - 1] - 1) * 100
                k = "Gap-Up (>0.3%)" if gp > 0.3 else ("Gap-Down (<-0.3%)" if gp < -0.3 else "Flat গ্যাপ")
                gg.setdefault(k, []).append(t)
            rows = []
            for k in ("Gap-Up (>0.3%)", "Flat গ্যাপ", "Gap-Down (<-0.3%)"):
                if k in gg:
                    st_ = calc_stats(gg[k], cap)
                    rows.append({"k": k, "n": st_["n"], "win": st_["win"], "net": st_["net"], "pf": st_["pf"], "exp": st_["exp"]})
            reg["gap"] = rows
            ed = []
            for h_ in (5, 15, 30, 60):
                fav, adv, n_ = [], [], 0
                for t in trades:
                    sp_ = t["side"].split()
                    if len(sp_) < 3:
                        continue
                    ct = D.opt.get((day_int(t["expiry"]), sp_[1], float(sp_[2])))
                    if ct is None:
                        continue
                    te = day_int(t["date"]) * 1440 + hm(t["in"])
                    j0 = int(np.searchsorted(ct[0], te))
                    if j0 >= len(ct[0]) or ct[0][j0] != te:
                        continue
                    j1 = int(np.searchsorted(ct[0], te + h_, "right"))
                    e0 = float(ct[1][j0])
                    if e0 <= 0 or j1 <= j0:
                        continue
                    hh, ll = float(ct[2][j0:j1].max()), float(ct[3][j0:j1].min())
                    sd = 1 if sp_[0] == "BUY" else -1
                    f_, a_ = ((hh - e0), (e0 - ll)) if sd == 1 else ((e0 - ll), (hh - e0))
                    fav.append(100 * max(f_, 0) / e0)
                    adv.append(100 * max(a_, 0) / e0)
                if fav:
                    ed.append({"min": h_, "n": len(fav), "mfe": float(np.mean(fav)), "mae": float(np.mean(adv)), "ratio": float(np.mean(fav) / np.mean(adv)) if np.mean(adv) > 0 else 0.0})
            out["eratio_decay"] = ed
            # টপ-৫ ড্রডাউন + স্থবিরতা + সিঙ্কড চার্ট
            dts = list(days_list) if days_list else sorted(by)
            eqd = np.cumsum(dp)
            pk = np.maximum.accumulate(np.r_[0.0, eqd])[1:]
            ddd = eqd - pk
            per, i = [], 0
            while i < len(ddd):
                if ddd[i] < 0:
                    j = i
                    while j < len(ddd) and ddd[j] < 0:
                        j += 1
                    seg = ddd[i:j]
                    k_ = int(np.argmin(seg)) + i
                    per.append({"from": dts[i - 1] if i > 0 else dts[0], "trough": dts[k_], "to": dts[j] if j < len(dts) else None, "dd": float(seg.min()), "days": int(j - i + 1)})
                    i = j
                else:
                    i += 1
            per.sort(key=lambda x: x["dd"])
            out["top_dd"] = per[:5]
            last_pk = 0
            stag = 0
            for i in range(len(eqd)):
                if eqd[i] >= pk[i] - 1e-9 and eqd[i] > (eqd[last_pk] if last_pk < len(eqd) else -1e18) - 1e-9:
                    stag = max(stag, (day_int(dts[i]) - day_int(dts[last_pk])))
                    last_pk = i
            stag = max(stag, day_int(dts[-1]) - day_int(dts[last_pk]))
            out["stagnation_days"] = int(stag)
            sp0 = None
            spot_b = []
            for dd_ in dts:
                j = int(np.searchsorted(D.du, day_int(dd_)))
                v = float(D.dC[min(j, len(D.dC) - 1)])
                sp0 = v if sp0 is None else sp0
                spot_b.append(round(cap * (v / sp0 - 1), 0))
            step = max(1, len(dts) // 400)
            out["sync"] = {"dates": dts[::step], "eq": [round(float(x), 0) for x in eqd[::step]], "dd": [round(float(x), 0) for x in ddd[::step]], "spot": spot_b[::step]}
    except Exception as e:
        out["adv_err"] = str(e)
    out["regime"] = {k: v for k, v in reg.items() if v}
    # হিটম্যাপ: সময়-স্লট × বার, মাস × বছর
    hmap = {}
    for t in trades:
        _m = int(t["in"][:2]) * 60 + int(t["in"][3:5])
        _b = (_m - OPEN_MIN) // 30 * 30 + OPEN_MIN
        k = (t["dow"], hm_str(_b))
        hmap[k] = hmap.get(k, 0.0) + t["net"]
    out["heat_slot"] = [{"dow": k[0], "slot": k[1], "net": v} for k, v in hmap.items()]
    mh = {}
    for t in trades:
        mh[(t["date"][:4], int(t["date"][5:7]))] = mh.get((t["date"][:4], int(t["date"][5:7])), 0.0) + t["net"]
    out["heat_month"] = [{"y": k[0], "m": k[1], "net": v} for k, v in sorted(mh.items())]
    out["scatter"] = [[round(abs(t["mae"]), 1), round(abs(t["mfe"]), 1), 1 if t["net"] > 0 else 0] for t in trades][:1500]
    return out


def stress_test(D, raw):
    """স্লিপেজ/খরচ/দেরিতে-এন্ট্রি স্ট্রেস: আসল ইঞ্জিনে বাড়িয়ে আবার চালানো"""
    base = norm_settings(raw)
    rows = []
    for lab, ms, mb, dl in (("এখনকার খরচ", 1, 1, 0), ("স্লিপেজ ×2", 2, 1, 0), ("স্লিপেজ ×3", 3, 1, 0), ("স্লিপেজ ×3 + ব্রোকারেজ ×1.5", 3, 1.5, 0),
                            ("+১ ক্যান্ডেল দেরিতে এন্ট্রি", 1, 1, 1), ("+২ ক্যান্ডেল দেরি + স্লিপেজ ×2", 2, 1, 2)):
        s = copy.deepcopy(base)
        s["general"]["slip"] = float(base["general"]["slip"]) * ms
        s["general"]["brokerage"] = float(base["general"]["brokerage"]) * mb
        s["general"]["delay"] = dl
        r = backtest(D, s, light=True)
        a = r["stats"]["all"]
        rows.append({"k": lab, "n": a["n"], "net": a["net"], "pf": a["pf"], "win": a["win"], "dd": a["dd"], "exp": a["exp"]})
    return {"rows": rows}



# =====================================================================
#  অপশন সেলিং ল্যাব: Black-Scholes Greeks/IV, মাল্টি-লেগ বাস্কেট ইঞ্জিন, টেমপ্লেট, মার্জিন
# =====================================================================
_RF = 0.07


def _ncdf(x):
    x = np.asarray(x, float)
    t = 1.0 / (1.0 + 0.2316419 * np.abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    pdf = np.exp(-0.5 * x * x) / np.sqrt(2 * np.pi)
    c = 1.0 - pdf * poly
    return np.where(x >= 0, c, 1.0 - c)


def _npdf(x):
    return np.exp(-0.5 * np.asarray(x, float) ** 2) / np.sqrt(2 * np.pi)


def bs_price(S, K, T, sig, typ, r=_RF):
    S, K, T, sig = (np.asarray(a, float) for a in (S, K, T, sig))
    T = np.maximum(T, 1e-8)
    sig = np.maximum(sig, 1e-6)
    sq = sig * np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    if typ == "CE":
        return S * _ncdf(d1) - K * np.exp(-r * T) * _ncdf(d2)
    return K * np.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def bs_iv(price, S, K, T, typ, r=_RF):
    """ভেক্টরাইজড বাইসেকশন IV (বার্ষিক, দশমিকে, যেমন 0.14). অসম্ভব দাম হলে nan"""
    price, S, K, T = (np.asarray(a, float) for a in (price, S, K, T))
    price, S, K, T = np.broadcast_arrays(price, S, K, T)
    lo = np.full(price.shape, 0.005)
    hi = np.full(price.shape, 4.0)
    for _ in range(46):
        mid = 0.5 * (lo + hi)
        p = bs_price(S, K, T, mid, typ, r)
        up = p < price
        lo = np.where(up, mid, lo)
        hi = np.where(up, hi, mid)
    iv = 0.5 * (lo + hi)
    intr = np.maximum((S - K) if typ == "CE" else (K - S), 0.0)
    bad = (price <= 0) | (price < intr * 0.999) | (iv > 3.99) | (iv < 0.0051)
    return np.where(bad, np.nan, iv)


def bs_greeks(S, K, T, sig, typ, r=_RF):
    S, K, T, sig = (np.asarray(a, float) for a in (S, K, T, sig))
    T = np.maximum(T, 1e-8)
    sig = np.maximum(sig, 1e-6)
    sq = sig * np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    pdf = _npdf(d1)
    delta = _ncdf(d1) if typ == "CE" else _ncdf(d1) - 1.0
    gamma = pdf / (S * sq)
    vega = S * pdf * np.sqrt(T) / 100.0                    # প্রতি ১ ভোল-পয়েন্টে
    th = -S * pdf * sig / (2 * np.sqrt(T))
    th = th - r * K * np.exp(-r * T) * _ncdf(d2) if typ == "CE" else th + r * K * np.exp(-r * T) * _ncdf(-d2)
    return dict(delta=delta, gamma=gamma, vega=vega, theta=th / 365.0)      # theta: প্রতি ক্যালেন্ডার-দিন


def _T_years(ex, t):
    return max(ex * 1440 + 930 - int(t), 1) / 525600.0


def sell_templates():
    S = lambda a, o, m, v, lots=1, sl=0, role="core", exp=0: {"act": a, "opt": o, "strike": {"mode": m, "val": v}, "lots": lots, "sl": sl, "tp": 0, "role": role, "exp": exp}
    return {
        "short_straddle": {"name": "Short Straddle", "view": "নিউট্রাল / রেঞ্জ", "desc": "ATM CE + ATM PE বিক্রি। সর্বোচ্চ থিটা, তাই লেগ-SL ও SL-to-Cost জরুরি।",
                           "legs": [S("SELL", "CE", "atm", 0, sl=30), S("SELL", "PE", "atm", 0, sl=30)]},
        "short_strangle": {"name": "Short Strangle", "view": "নিউট্রাল (চওড়া রেঞ্জ)", "desc": "OTM CE + OTM PE বিক্রি (≈0.20Δ)। উচ্চ Win-rate, চওড়া ব্রেকইভেন।",
                           "legs": [S("SELL", "CE", "delta", 0.20, sl=30), S("SELL", "PE", "delta", 0.20, sl=30)]},
        "iron_condor": {"name": "Iron Condor", "view": "সীমিত ঝুঁকি, নিউট্রাল", "desc": "Short Strangle + দূরের বাই CE/PE হেজ। সর্বোচ্চ লস নির্দিষ্ট, মার্জিন কম।",
                        "legs": [S("BUY", "CE", "prem", 10, role="hedge"), S("BUY", "PE", "prem", 10, role="hedge"), S("SELL", "CE", "delta", 0.20, sl=30), S("SELL", "PE", "delta", 0.20, sl=30)]},
        "iron_butterfly": {"name": "Iron Butterfly", "view": "টাইট রেঞ্জ / এক্সপায়ারি", "desc": "ATM Straddle বিক্রি + ±৪ স্ট্রাইকে বাই উইং।",
                           "legs": [S("BUY", "CE", "atm", 6, role="hedge"), S("BUY", "PE", "atm", 6, role="hedge"), S("SELL", "CE", "atm", 0, sl=30), S("SELL", "PE", "atm", 0, sl=30)]},
        "bull_put": {"name": "Bull Put Spread", "view": "মৃদু বুলিশ", "desc": "OTM PE বিক্রি + আরও দূরের PE কেনা (ক্রেডিট স্প্রেড)।",
                     "legs": [S("BUY", "PE", "atm", 6, role="hedge"), S("SELL", "PE", "atm", 2, sl=35)]},
        "bear_call": {"name": "Bear Call Spread", "view": "মৃদু বেয়ারিশ", "desc": "OTM CE বিক্রি + আরও দূরের CE কেনা (ক্রেডিট স্প্রেড)।",
                      "legs": [S("BUY", "CE", "atm", 6, role="hedge"), S("SELL", "CE", "atm", 2, sl=35)]},
        "ratio_spread": {"name": "Call Ratio Spread (1×2)", "view": "আংশিক বুলিশ", "desc": "১× ATM CE কেনা + ২× OTM CE বিক্রি। ওপরে অসীম ঝুঁকি — সতর্ক।",
                         "legs": [S("BUY", "CE", "atm", 0, 1, role="hedge"), S("SELL", "CE", "atm", 3, 2, sl=40)]},
        "jade_lizard": {"name": "Jade Lizard", "view": "আংশিক বুলিশ", "desc": "OTM PE বিক্রি + (OTM CE বিক্রি + আরও দূরের CE কেনা) বেয়ার কল স্প্রেড।",
                        "legs": [S("BUY", "CE", "atm", 8, role="hedge"), S("SELL", "CE", "atm", 3, sl=35), S("SELL", "PE", "atm", 3, sl=35)]},
        "calendar": {"name": "Calendar Spread (CE)", "view": "লো IV / সময়-ভিত্তিক", "desc": "কাছের এক্সপায়ারিতে ATM CE বিক্রি + পরের এক্সপায়ারিতে ATM CE কেনা (ইন্ট্রাডে)।",
                     "legs": [S("BUY", "CE", "atm", 0, role="hedge", exp=1), S("SELL", "CE", "atm", 0, sl=35)]},
    }


def sell_defaults():
    return {
        "name": "Short Straddle 09:20", "template": "short_straddle",
        "legs": copy.deepcopy(sell_templates()["short_straddle"]["legs"]),
        "entry": {"time": "09:20", "min_dte": 0, "max_dte": 7, "dows": [0, 1, 2, 3, 4], "ivp_min": 0, "ivp_max": 100, "gap_max": 0},
        "exit": {"time": "15:15", "basket_sl": 0, "basket_tp": 0, "lock_at": 0, "lock_to": 0, "trail_step": 0, "trail_lock": 0},
        "rules": {"sl_to_cost": True, "reentry_max": 0, "reentry_mode": "asap", "adjust": False, "adj_ratio": 2.0, "adj_delta": 0.0, "adj_max": 2,
                  "auto_hedge": False, "hedge_prem": 10},
        "size": {"mode": "fixed", "lots": 1, "risk_pct": 1.0},
        "margin": {"pct": 13.0, "hedge_benefit": 1.0},
        "general": {"capital": 500000, "from": "", "to": "", "slip": 0.5, "brokerage": 20, "charges_pct": 0.15, "split": 70},
    }


def sell_norm(raw):
    d = sell_defaults()
    raw = raw or {}
    for k in ("entry", "exit", "rules", "size", "margin", "general"):
        d[k].update(raw.get(k) or {})
    for k in ("name", "template"):
        if raw.get(k):
            d[k] = str(raw[k])
    if raw.get("legs"):
        d["legs"] = []
        for lg in raw["legs"]:
            s = lg.get("strike") or {}
            d["legs"].append({"act": "SELL" if str(lg.get("act", "SELL")).upper() == "SELL" else "BUY",
                              "opt": "PE" if str(lg.get("opt", "CE")).upper() == "PE" else "CE",
                              "strike": {"mode": s.get("mode", "atm") if s.get("mode") in ("atm", "pts", "prem", "delta") else "atm", "val": float(s.get("val", 0) or 0)},
                              "lots": max(1, int(lg.get("lots", 1) or 1)), "sl": float(lg.get("sl", 0) or 0), "tp": float(lg.get("tp", 0) or 0),
                              "role": "hedge" if lg.get("role") == "hedge" else "core", "exp": int(lg.get("exp", 0) or 0)})
    if not d["legs"]:
        raise ValueError("কমপক্ষে একটি লেগ দিন")
    for k in ("min_dte", "max_dte", "ivp_min", "ivp_max", "gap_max"):
        d["entry"][k] = float(d["entry"][k])
    for k in ("basket_sl", "basket_tp", "lock_at", "lock_to", "trail_step", "trail_lock"):
        d["exit"][k] = float(d["exit"][k] or 0)
    if hm(d["entry"]["time"]) >= hm(d["exit"]["time"]):
        raise ValueError("এন্ট্রির সময় স্কোয়ার-অফের আগে হতে হবে")
    for k in ("capital", "slip", "brokerage", "charges_pct", "split"):
        d["general"][k] = float(d["general"][k])
    return d


def _bar_at(ct, t):
    """(o,h,l,c) বা None — ঠিক t সময়ের বার"""
    tt = ct[0]
    j = int(np.searchsorted(tt, t))
    if j < len(tt) and tt[j] == t:
        return float(ct[1][j]), float(ct[2][j]), float(ct[3][j]), float(ct[4][j])
    return None


def _spot_at(D, t):
    i = int(np.searchsorted(D.t, t, "right")) - 1
    return float(D.o[i]) if i >= 0 and D.t[i] == t else (float(D.c[i]) if i >= 0 else None)


def _find_ct(D, ex, typ, stk, te):
    ct = D.opt.get((ex, typ, float(stk)))
    if ct is None:
        return None
    j = int(np.searchsorted(ct[0], te))
    if j >= len(ct[0]) or ct[0][j] - te > 10:
        return None
    return ct, j


def _pick_strike(D, ex, typ, spec, te, spot, avoid_beyond=None):
    stk = D.strikes.get((ex, typ))
    if stk is None or not len(stk):
        return None
    mode, val = spec["mode"], float(spec["val"])
    atm = int(np.argmin(np.abs(stk - spot)))
    if mode == "atm":
        k = atm + (int(val) if typ == "CE" else -int(val))
        return float(stk[k]) if 0 <= k < len(stk) else None
    if mode == "pts":
        tgt = float(stk[atm]) + (val if typ == "CE" else -val)
        return float(stk[int(np.argmin(np.abs(stk - tgt)))])
    cand = []
    T = _T_years(ex, te)
    for s_ in stk[max(0, atm - 45): atm + 46]:
        r = _find_ct(D, ex, typ, s_, te)
        if r is None:
            continue
        pr = float(r[0][1][r[1]])
        if pr <= 0:
            continue
        cand.append((float(s_), pr))
    if not cand:
        return None
    if mode == "prem":
        return min(cand, key=lambda x: abs(x[1] - val))[0]
    # delta
    ks = np.array([c[0] for c in cand])
    ps = np.array([c[1] for c in cand])
    iv = bs_iv(ps, spot, ks, T, typ)
    dl = np.abs(bs_greeks(spot, ks, T, np.where(np.isfinite(iv), iv, 0.2), typ)["delta"])
    dl = np.where(np.isfinite(iv), dl, 9.0)
    return float(ks[int(np.argmin(np.abs(dl - val)))])


def sell_margin(legs, spot, cfg, lot):
    """আনুমানিক মার্জিন: নেকেড সেলিং = pct% × নোশনাল; হেজড হলে স্প্রেডের সর্বোচ্চ-লসে সীমাবদ্ধ।"""
    pct = float(cfg["margin"]["pct"]) / 100.0
    side = {}
    for lg in legs:
        q = lg["qty"] * (1 if lg["act"] == "BUY" else -1)
        side.setdefault(lg["opt"], []).append((lg["strike"], q, lg["entry"]))
    m = {}
    prem = 0.0
    for typ, ls in side.items():
        shorts = [(s, -q) for s, q, e in ls if q < 0]
        longs = [(s, q) for s, q, e in ls if q > 0]
        sq = sum(q for s, q in shorts)
        if sq <= 0:
            continue
        naked = pct * spot * sq
        lq = sum(q for s, q in longs)
        if lq >= sq and longs:
            # প্রতিটি শর্টের সাথে সবচেয়ে কাছের লং জোড়া; সর্বোচ্চ লস = Σ |স্ট্রাইক-ব্যবধান| × qty
            width = 0.0
            lst = sorted(longs)
            rem = [list(x) for x in lst]
            for s, q in sorted(shorts):
                need = q
                for r_ in sorted(rem, key=lambda z: abs(z[0] - s)):
                    take = min(need, r_[1])
                    if take > 0:
                        # CE: লং স্ট্রাইক ওপরে থাকলে সীমিত ঝুঁকি; নিচে থাকলে (ITM-বাই) ঝুঁকি নেই
                        w = (r_[0] - s) if typ == "CE" else (s - r_[0])
                        width += max(w, 0.0) * take
                        r_[1] -= take
                        need -= take
                    if need <= 0:
                        break
                if need > 0:
                    width += pct * spot * need
            m[typ] = min(naked, width + 0.01 * spot * sq) * float(cfg["margin"].get("hedge_benefit", 1.0))
        else:
            m[typ] = naked
    if not m:
        return 0.0
    v = sorted(m.values(), reverse=True)
    return float(v[0] + (0.3 * v[1] if len(v) > 1 else 0.0))


def _sell_day_setup(D, cfg, d, ex, te, auto_hedge=True):
    """এন্ট্রি-মুহূর্তে লেগগুলো বেছে নেয়। ফেরত: (legs, spot) বা (None, কারণ)"""
    spot = _spot_at(D, te)
    if spot is None:
        return None, "স্পট বার নেই"
    exps = D.expiries
    k0 = bisect.bisect_left(exps, ex)
    out = []
    core_short = []
    for lg in cfg["legs"]:
        k = k0 + int(lg["exp"])
        if k >= len(exps):
            return None, "পরের এক্সপায়ারি নেই"
        e2 = exps[k]
        stk = _pick_strike(D, e2, lg["opt"], lg["strike"], te, spot)
        if stk is None:
            return None, "স্ট্রাইক পাওয়া যায়নি"
        r = _find_ct(D, e2, lg["opt"], stk, te)
        if r is None:
            return None, "কন্ট্র্যাক্টে বার নেই"
        out.append({"act": lg["act"], "opt": lg["opt"], "strike": float(stk), "ex": e2, "lots": lg["lots"], "sl": lg["sl"], "tp": lg["tp"], "role": lg["role"], "ct": r[0],
                    "j0": r[1], "spec": lg["strike"]})
    rules = cfg["rules"]
    if rules.get("auto_hedge"):
        for typ in ("CE", "PE"):
            sh = [x for x in out if x["opt"] == typ and x["act"] == "SELL"]
            lo = [x for x in out if x["opt"] == typ and x["act"] == "BUY"]
            if sh and not lo:
                far = max(sh, key=lambda x: x["strike"]) if typ == "CE" else min(sh, key=lambda x: x["strike"])
                stks = D.strikes.get((far["ex"], typ))
                best = None
                for s_ in stks:
                    if (typ == "CE" and s_ <= far["strike"]) or (typ == "PE" and s_ >= far["strike"]):
                        continue
                    r = _find_ct(D, far["ex"], typ, s_, te)
                    if r is None:
                        continue
                    pr = float(r[0][1][r[1]])
                    if pr <= 0:
                        continue
                    df = abs(pr - float(rules["hedge_prem"]))
                    if best is None or df < best[0]:
                        best = (df, float(s_), r)
                if best:
                    out.append({"act": "BUY", "opt": typ, "strike": best[1], "ex": far["ex"], "lots": max(x["lots"] for x in sh), "sl": 0, "tp": 0, "role": "hedge",
                                "ct": best[2][0], "j0": best[2][1], "spec": {"mode": "prem", "val": float(rules["hedge_prem"])}})
    out.sort(key=lambda x: 0 if x["act"] == "BUY" else 1)       # হেজ আগে
    return out, spot


def sell_backtest(D, raw, progress=None, only_day=None):
    cfg = sell_norm(raw)
    G = cfg["general"]
    E, X, R = cfg["entry"], cfg["exit"], cfg["rules"]
    te_m, sq_m = hm(E["time"]), hm(X["time"])
    slip, brk, chg = G["slip"], G["brokerage"], G["charges_pct"]
    d_from = day_int(G["from"]) if G["from"] else -10 ** 9
    d_to = day_int(G["to"]) if G["to"] else 10 ** 9
    exps = D.expiries
    eligible = []
    for d in D.du.tolist():
        if d < d_from or d > d_to:
            continue
        if ((d + 3) % 7) not in [int(x) for x in E["dows"]]:
            continue
        k = bisect.bisect_left(exps, d + int(E["min_dte"]))
        if k >= len(exps):
            continue
        ex = exps[k]
        if ex - d > E["max_dte"]:
            continue
        lo, hi = D.exp_range[ex]
        if lo <= d * 1440 + 1439 and hi >= d * 1440:
            eligible.append((d, ex))
    if only_day is not None:
        eligible = [x for x in eligible if x[0] == only_day]
    # IV ইতিহাস (IVP ফিল্টার ও রিপোর্টের জন্য): প্রতিদিন এন্ট্রি-সময়ে ATM IV
    ivh = {}
    for d, ex in eligible:
        te = d * 1440 + te_m
        spot = _spot_at(D, te)
        stk = D.strikes.get((ex, "CE"))
        if spot is None or stk is None:
            continue
        s_ = float(stk[int(np.argmin(np.abs(stk - spot)))])
        r = _find_ct(D, ex, "CE", s_, te)
        rp = _find_ct(D, ex, "PE", s_, te)
        if r is None:
            continue
        T = _T_years(ex, te)
        v = bs_iv(float(r[0][1][r[1]]), spot, s_, T, "CE")
        if rp is not None:
            v2 = bs_iv(float(rp[0][1][rp[1]]), spot, s_, T, "PE")
            v = np.nanmean([v, v2]) if np.isfinite(v2) or np.isfinite(v) else v
        if np.isfinite(v):
            ivh[d] = float(v)
    ivd = sorted(ivh)
    trades = []
    skipped = {}
    sk = lambda w: skipped.__setitem__(w, skipped.get(w, 0) + 1)
    total = max(len(eligible), 1)
    prev_close = {}
    for di, (d, ex) in enumerate(eligible):
        if progress and di % 10 == 0:
            progress(di / total)
        te = d * 1440 + te_m
        sqt = d * 1440 + sq_m
        # IV-ফিল্টার
        iv_now = ivh.get(d)
        ivp = None
        if iv_now is not None:
            past = [ivh[x] for x in ivd if x < d][-60:]
            if len(past) >= 10:
                ivp = 100.0 * sum(1 for x in past if x <= iv_now) / len(past)
        if (E["ivp_min"] > 0 or E["ivp_max"] < 100) and (ivp is None or ivp < E["ivp_min"] or ivp > E["ivp_max"]):
            sk("IV ফিল্টার")
            continue
        if E["gap_max"] > 0:
            i0 = int(np.searchsorted(D.day, d))
            j0 = int(np.searchsorted(D.du, d))
            if j0 > 0 and i0 < len(D.o):
                gp = abs(D.o[i0] / D.dC[j0 - 1] - 1) * 100
                if gp > E["gap_max"]:
                    sk("গ্যাপ ফিল্টার")
                    continue
        legs0, spot = _sell_day_setup(D, cfg, d, ex, te)
        if legs0 is None:
            sk(spot)
            continue
        lot = D.exp_lot.get(ex, 1)
        # লট-সাইজিং
        lots = int(cfg["size"]["lots"])
        if cfg["size"]["mode"] == "risk" and X["basket_sl"] > 0:
            lots = max(1, int(G["capital"] * float(cfg["size"]["risk_pct"]) / 100.0 // X["basket_sl"]))
        mult = lots
        legs = []
        for i, lg in enumerate(legs0):
            side = -1 if lg["act"] == "SELL" else 1
            raw_e = float(lg["ct"][1][lg["j0"]])
            legs.append(dict(lg, side=side, qty=lg["lots"] * mult * lot, entry_raw=raw_e, entry=raw_e + side * slip, open=True, exit=None, exit_raw=None,
                             t_in=int(lg["ct"][0][lg["j0"]]), t_out=None, why=None, last=raw_e, sl_px=None, tp_px=None, orig=raw_e, re_left=int(R["reentry_max"])))
            L = legs[-1]
            if L["sl"] > 0:
                L["sl_px"] = raw_e * (1 + L["sl"] / 100.0) if L["act"] == "SELL" else max(raw_e * (1 - L["sl"] / 100.0), 0.05)
            if L["tp"] > 0:
                L["tp_px"] = raw_e * (1 - L["tp"] / 100.0) if L["act"] == "SELL" else raw_e * (1 + L["tp"] / 100.0)
        margin = sell_margin([dict(act=x["act"], opt=x["opt"], strike=x["strike"], qty=x["qty"], entry=x["entry_raw"]) for x in legs], spot, cfg, lot)
        sl_b, tp_b = X["basket_sl"] * mult, X["basket_tp"] * mult
        lock_at, lock_to = X["lock_at"] * mult, X["lock_to"] * mult
        tr_step, tr_lock = X["trail_step"] * mult, X["trail_lock"] * mult
        allt = np.unique(np.concatenate([x["ct"][0][(x["ct"][0] >= te) & (x["ct"][0] <= sqt)] for x in legs]))
        peak = -1e18
        low = 1e18
        pend = []
        nadj = 0
        done_reason = None
        events = []
        all_legs = list(legs)
        t_end = te
        for t in allt.tolist():
            t_end = t
            bars = {}
            for x in all_legs:
                if x["open"]:
                    b = _bar_at(x["ct"], t)
                    if b is not None:
                        bars[id(x)] = b
            stopped = []
            # পেন্ডিং রি-এন্ট্রি
            for pr in list(pend):
                x0 = pr["leg"]
                b = _bar_at(x0["ct"], t)
                if b is None or t <= pr["after"]:
                    continue
                go = pr["mode"] == "asap" or (pr["mode"] == "recost" and ((b[3] <= pr["cost"]) if x0["act"] == "SELL" else (b[3] >= pr["cost"])))
                if go:
                    side = x0["side"]
                    nl = dict(x0, entry_raw=b[0], entry=b[0] + side * slip, open=True, exit=None, exit_raw=None, t_in=t, t_out=None, why=None, last=b[0], re_left=pr["left"] - 1, reent=True)
                    if nl["sl"] > 0:
                        nl["sl_px"] = b[0] * (1 + nl["sl"] / 100.0) if nl["act"] == "SELL" else max(b[0] * (1 - nl["sl"] / 100.0), 0.05)
                    all_legs.append(nl)
                    bars[id(nl)] = b
                    pend.remove(pr)
                    events.append("%s রি-এন্ট্রি %s %s" % (hm_str(t % 1440), nl["opt"], int(nl["strike"])))
            # লেগ SL / TP
            for x in all_legs:
                if not x["open"] or id(x) not in bars:
                    continue
                o_, h_, l_, c_ = bars[id(x)]
                x["last"] = c_
                hit = None
                if x["act"] == "SELL":
                    if x["sl_px"] is not None and h_ >= x["sl_px"]:
                        hit = ("SL", max(x["sl_px"], o_))
                    elif x["tp_px"] is not None and l_ <= x["tp_px"]:
                        hit = ("TP", min(x["tp_px"], o_))
                else:
                    if x["sl_px"] is not None and l_ <= x["sl_px"]:
                        hit = ("SL", min(x["sl_px"], o_))
                    elif x["tp_px"] is not None and h_ >= x["tp_px"]:
                        hit = ("TP", max(x["tp_px"], o_))
                if hit:
                    x["open"] = False
                    x["exit_raw"] = hit[1]
                    x["exit"] = hit[1] - x["side"] * slip
                    x["t_out"] = t
                    x["why"] = "Leg " + hit[0]
                    stopped.append((x, hit[0]))
            for x, kind in stopped:
                events.append("%s %s %s %s" % (hm_str(t % 1440), x["opt"], int(x["strike"]), x["why"]))
                if kind == "SL":
                    if R["sl_to_cost"]:
                        for y in all_legs:
                            if y["open"] and y["role"] == "core" and y["act"] == "SELL" and y is not x:
                                y["sl_px"] = y["entry_raw"] if y["sl_px"] is None else min(y["sl_px"], y["entry_raw"])
                    if x["re_left"] > 0:
                        pend.append({"leg": x, "mode": R["reentry_mode"], "after": t, "left": x["re_left"], "cost": x["orig"]})
            # MTM
            open_l = [x for x in all_legs if x["open"]]
            mtm = 0.0
            for x in all_legs:
                px = x["exit"] if not x["open"] else x["last"]
                if not x["open"]:
                    mtm += (x["exit"] - x["entry"]) * x["side"] * x["qty"] * (-1 if False else 1)
                else:
                    mtm += (px - x["entry"]) * x["side"] * x["qty"]
            # side: SELL=-1 → (px-entry)*(-1) = entry-px  ✓.  BUY=+1 ✓
            peak, low = max(peak, mtm), min(low, mtm)
            if not open_l and not pend:
                done_reason = "সব লেগ SL/TP-তে"
                break
            if sl_b > 0 and mtm <= -sl_b:
                done_reason = "Basket SL"
            elif tp_b > 0 and mtm >= tp_b:
                done_reason = "Basket TP"
            else:
                lk = None
                if lock_at > 0 and peak >= lock_at:
                    lk = lock_to
                if tr_step > 0 and tr_lock > 0 and lock_at > 0 and peak >= lock_at:
                    lk = lock_to + int((peak - lock_at) // tr_step) * tr_lock
                if lk is not None and mtm <= lk:
                    done_reason = "Lock/Trail Profit"
            # অটো-অ্যাডজাস্টমেন্ট
            if done_reason is None and R["adjust"] and nadj < int(R["adj_max"]):
                sh = [x for x in open_l if x["act"] == "SELL" and x["role"] == "core"]
                ce = [x for x in sh if x["opt"] == "CE"]
                pe = [x for x in sh if x["opt"] == "PE"]
                if ce and pe:
                    pc, pp = ce[0]["last"], pe[0]["last"]
                    trig = False
                    if R["adj_ratio"] > 1 and (pc >= R["adj_ratio"] * pp or pp >= R["adj_ratio"] * pc):
                        trig = True
                    if R["adj_delta"] > 0 and not trig:
                        sp_ = _spot_at(D, t) or spot
                        nd = 0.0
                        for x in open_l:
                            T = _T_years(x["ex"], t)
                            iv = bs_iv(x["last"], sp_, x["strike"], T, x["opt"])
                            if np.isfinite(iv):
                                nd += float(bs_greeks(sp_, x["strike"], T, iv, x["opt"])["delta"]) * x["side"] * -1 * -1 * x["qty"] / lot / max(mult, 1)
                        trig = abs(nd) > R["adj_delta"]
                    if trig:
                        win = ce[0] if pc < pp else pe[0]            # লাভজনক (কম প্রিমিয়াম) লেগ
                        tgt = max(pc, pp)
                        sp_ = _spot_at(D, t) or spot
                        stk = D.strikes.get((win["ex"], win["opt"]))
                        best = None
                        for s_ in stk[max(0, int(np.argmin(np.abs(stk - sp_))) - 40): int(np.argmin(np.abs(stk - sp_))) + 41]:
                            r = _find_ct(D, win["ex"], win["opt"], s_, t)
                            if r is None or r[0][0][r[1]] != t:
                                continue
                            pr_ = float(r[0][1][r[1]])
                            if best is None or abs(pr_ - tgt) < best[0]:
                                best = (abs(pr_ - tgt), float(s_), r, pr_)
                        if best and best[1] != win["strike"]:
                            side = win["side"]
                            win["open"] = False
                            win["exit_raw"] = win["last"]
                            win["exit"] = win["last"] - side * slip
                            win["t_out"] = t
                            win["why"] = "Adjust (roll)"
                            nl = dict(win, strike=best[1], ct=best[2][0], entry_raw=best[3], entry=best[3] + side * slip, open=True, exit=None, exit_raw=None,
                                      t_in=t, t_out=None, why=None, last=best[3], reent=False, adj=True)
                            nl["sl_px"] = best[3] * (1 + nl["sl"] / 100.0) if nl["sl"] > 0 else None
                            all_legs.append(nl)
                            nadj += 1
                            events.append("%s অ্যাডজাস্ট: %s %d → %d" % (hm_str(t % 1440), win["opt"], int(win["strike"]), int(best[1])))
            if done_reason:
                break
        # শেষ: সেল লেগ আগে, হেজ পরে ক্লোজ (খরচের হিসাবে একই)
        for x in sorted([y for y in all_legs if y["open"]], key=lambda y: 0 if y["act"] == "SELL" else 1):
            x["open"] = False
            x["exit_raw"] = x["last"]
            x["exit"] = x["last"] - x["side"] * slip
            x["t_out"] = t_end
            x["why"] = done_reason or "Square-off"
        gross = costs = turn = 0.0
        ltxt = []
        for x in all_legs:
            g = (x["exit"] - x["entry"]) * x["side"] * x["qty"]
            gross += g
            costs += 2 * brk
            turn += (x["entry"] if x["act"] == "SELL" else x["exit"]) * x["qty"]
            ltxt.append({"act": x["act"], "opt": x["opt"], "strike": int(x["strike"]), "qty": int(x["qty"]), "in": hm_str(x["t_in"] % 1440), "out": hm_str(x["t_out"] % 1440),
                         "entry": round(x["entry"], 2), "exit": round(x["exit"], 2), "pnl": round(g, 2), "why": x["why"], "role": x["role"]})
        costs += chg / 100.0 * turn
        net = gross - costs
        reason = done_reason or "Square-off"
        trades.append({"date": day_str(d), "case": cfg["name"], "side": cfg["template"], "expiry": day_str(ex), "in": hm_str(te % 1440), "out": hm_str(t_end % 1440),
                       "reason": reason, "lots": mult, "qty": int(sum(x["qty"] for x in legs if x["act"] == "SELL")), "gross": round(gross, 2), "costs": round(costs, 2),
                       "net": round(net, 2), "legs": ltxt, "events": events, "spot": round(spot, 2), "dte": int(ex - d), "dow": DOWN[(d + 3) % 7],
                       "margin": round(margin, 0), "rom": round(100.0 * net / margin, 2) if margin else None, "mtm_peak": round(peak, 0), "mtm_low": round(low, 0),
                       "iv": round(iv_now * 100, 2) if iv_now else None, "ivp": round(ivp, 0) if ivp is not None else None,
                       "hold": int(t_end - te), "r": round(net / sl_b, 2) if sl_b else None, "ret": 0.0, "entry": 0.0, "exit": 0.0, "bars": 0})
    cap = G["capital"]
    days_list = [day_str(d) for d, _ in eligible]
    cut = int(len(days_list) * G["split"] / 100.0)
    sd = days_list[cut] if 0 < cut < len(days_list) else None
    tr_tr = [t for t in trades if sd and t["date"] < sd] if sd else trades
    tr_te = [t for t in trades if sd and t["date"] >= sd] if sd else []
    out = {"stats": {"all": calc_stats(trades, cap), "train": calc_stats(tr_tr, cap), "test": calc_stats(tr_te, cap)},
           "meta": {"days": len(days_list), "from": days_list[0] if days_list else None, "to": days_list[-1] if days_list else None, "split_date": sd, "skipped": skipped,
                    "template": cfg["template"]},
           "trades": trades}
    if trades:
        eq, c_ = [], 0.0
        for t in trades:
            c_ += t["net"]
            eq.append(round(c_, 2))
        out["equity"] = eq
        mg = [t["margin"] for t in trades if t["margin"]]
        avg_m = float(np.mean(mg)) if mg else 0.0
        net = out["stats"]["all"]["net"]
        out["margin"] = {"avg": avg_m, "max": float(max(mg)) if mg else 0.0, "rom_total": float(100 * net / avg_m) if avg_m else 0.0,
                         "dd_pct_margin": float(100 * out["stats"]["all"]["dd"] / avg_m) if avg_m else 0.0}
        yr = max(len(days_list) / 252.0, 1e-9)
        out["margin"]["rom_annual"] = out["margin"]["rom_total"] / yr
        for kk, keyf in (("by_reason", lambda t: t["reason"]), ("by_dow", lambda t: t["dow"]), ("by_dte", lambda t: str(t["dte"]))):
            g = {}
            for t in trades:
                g.setdefault(keyf(t), []).append(t)
            out[kk] = [dict(k=k, **{a: b for a, b in calc_stats(v, cap).items() if a in ("n", "win", "net", "pf", "exp")}) for k, v in sorted(g.items())]
        mo = {}
        for t in trades:
            mo.setdefault(t["date"][:7], []).append(t)
        out["monthly"] = [dict(k=k, **calc_stats(v, cap)) for k, v in sorted(mo.items())]
        by = {}
        for t in trades:
            by[t["date"]] = by.get(t["date"], 0.0) + t["net"]
        dp = np.array([by.get(x, 0.0) for x in days_list], float)
        if len(dp) > 2 and dp.std(ddof=1) > 0:
            out["sharpe"] = float(dp.mean() / dp.std(ddof=1) * np.sqrt(252))
            dsd = float(np.sqrt((np.minimum(dp, 0) ** 2).mean()))
            out["sortino"] = float(dp.mean() / dsd * np.sqrt(252)) if dsd > 0 else 0.0
    return out


def sell_chain(D, raw, body):
    """একটি দিন-সময়ে স্ট্রাইকওয়ার্ক প্রিমিয়াম, IV (স্কিউ) ও ডেল্টা (OI ডেটা আমাদের ডেটাবেসে নেই)"""
    d = day_int(body["date"])
    tm = hm(body.get("time", "09:20"))
    exps = D.expiries
    k = bisect.bisect_left(exps, d)
    if k >= len(exps):
        raise ValueError("এক্সপায়ারি পাওয়া যায়নি")
    ex = exps[k]
    a = int(np.searchsorted(D.day, d, "left"))
    b = int(np.searchsorted(D.day, d, "right"))
    if b <= a:
        raise ValueError("এই দিনের স্পট নেই")
    T = D.t[a:b]
    i = min(int(np.searchsorted(T % 1440, tm)), len(T) - 1)
    te = int(T[i])
    spot = float(D.o[a + i])
    Ty = _T_years(ex, te)
    rows = []
    for typ in ("CE", "PE"):
        for s_ in D.strikes.get((ex, typ), []):
            if abs(s_ - spot) > 0.08 * spot:
                continue
            ct = D.opt.get((ex, typ, float(s_)))
            if ct is None:
                continue
            j = int(np.searchsorted(ct[0], te))
            if j >= len(ct[0]) or ct[0][j] != te:
                continue
            pr = float(ct[4][j])
            iv = float(bs_iv(pr, spot, float(s_), Ty, typ))
            dl = float(bs_greeks(spot, float(s_), Ty, iv, typ)["delta"]) if np.isfinite(iv) else None
            rows.append({"strike": float(s_), "opt": typ, "price": round(pr, 2), "iv": round(iv * 100, 2) if np.isfinite(iv) else None,
                         "delta": None if dl is None else round(dl, 3)})
    return {"spot": spot, "time": hm_str(te % 1440), "expiry": day_str(ex), "rows": rows}



def sell_preview(D, raw, date_str=None):
    """একটি দিনের এন্ট্রি-মুহূর্তের বাস্কেট (পে-অফ চার্টের জন্য): লেগ, প্রিমিয়াম, IV, গ্রিকস"""
    cfg = sell_norm(raw)
    E = cfg["entry"]
    exps = D.expiries
    days = [d for d in D.du.tolist()]
    if date_str:
        d = day_int(date_str)
    else:
        d = days[-1]
    if d not in set(days):
        d = max([x for x in days if x <= d] or [days[-1]])
    k = bisect.bisect_left(exps, d + int(E["min_dte"]))
    if k >= len(exps):
        raise ValueError("এই তারিখে এক্সপায়ারি পাওয়া গেল না")
    ex = exps[k]
    te = d * 1440 + hm(E["time"])
    legs, spot = _sell_day_setup(D, cfg, d, ex, te)
    if legs is None:
        raise ValueError("এই দিনে বাস্কেট বানানো যায়নি: %s" % spot)
    lot = D.exp_lot.get(ex, 1)
    res = []
    ivs = []
    for lg in legs:
        px = float(lg["ct"][1][lg["j0"]])
        T = _T_years(lg["ex"], te)
        iv = float(bs_iv(px, spot, lg["strike"], T, lg["opt"]))
        g = bs_greeks(spot, lg["strike"], T, iv if np.isfinite(iv) else 0.2, lg["opt"])
        res.append({"act": lg["act"], "opt": lg["opt"], "strike": lg["strike"], "lots": lg["lots"], "qty": lg["lots"] * lot, "premium": round(px, 2),
                    "iv": round(iv * 100, 2) if np.isfinite(iv) else None, "delta": round(float(g["delta"]), 3), "theta": round(float(max(g["theta"], -px)), 2),
                    "vega": round(float(g["vega"]), 2), "gamma": round(float(g["gamma"]), 5), "T_days": round(T * 365, 2), "role": lg["role"], "ex": day_str(lg["ex"])})
        ivs.append(iv)
    atm_iv = float(np.nanmean([v for v in ivs if np.isfinite(v)])) if any(np.isfinite(v) for v in ivs) else 0.2
    m = sell_margin([dict(act=x["act"], opt=x["opt"], strike=x["strike"], qty=x["qty"], entry=x["premium"]) for x in res], spot, cfg, lot)
    stk = D.strikes.get((ex, "CE"))
    return {"date": day_str(d), "expiry": day_str(ex), "spot": round(spot, 2), "lot": lot, "legs": res, "atm_iv": round(atm_iv * 100, 2), "margin": round(m, 0),
            "T_days": round(_T_years(ex, te) * 365, 2), "rf": _RF, "strike_step": float(np.median(np.diff(stk))) if stk is not None and len(stk) > 1 else 100.0}



# =====================================================================
#  হাইব্রিড LLM কো-পাইলট (ঐচ্ছিক)। LLM কোনো গণিত করে না — শুধু (ক) আইডিয়া → Case JSON, (খ) ফলাফলের ভাষ্য।
#  সব সংখ্যা ও যাচাই আমাদের ইঞ্জিনেই হয়; কী না থাকলে এই অংশ নিষ্ক্রিয়।
# =====================================================================
LLM_MODEL = "claude-sonnet-5-5"


LLM_OPTS = {"model": None, "json": False}
GEMINI_DEFAULT = "gemini-3.5-flash-lite"


def llm_key(given=None):
    if given:
        return str(given).strip()
    for pth in (os.environ.get("GEMINI_API_KEY"), os.environ.get("ANTHROPIC_API_KEY")):
        if pth:
            return pth.strip()
    try:
        d = (DATA.get("src") or {}).get("db_dir")
        if d and os.path.exists(os.path.join(d, "llm_key.txt")):
            return open(os.path.join(d, "llm_key.txt")).read().strip()
    except Exception:
        pass
    return ""


def llm_provider(key):
    k = (key or "").strip()
    if k.startswith("AIza"):
        return "gemini"
    if k.startswith("sk-ant-"):
        return "anthropic"
    raise ValueError("API Key চেনা যায়নি — Gemini Key 'AIza' দিয়ে, Anthropic Key 'sk-ant-' দিয়ে শুরু হয়")


def llm_call(system, user, key, max_tokens=1600):
    import requests
    prov = llm_provider(key)
    model = (LLM_OPTS.get("model") or "").strip()
    if prov == "gemini":
        model = model or GEMINI_DEFAULT
        gc = {"maxOutputTokens": max_tokens}
        if LLM_OPTS.get("json"):
            gc["responseMimeType"] = "application/json"
        r = requests.post("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model,
                          headers={"x-goog-api-key": key, "content-type": "application/json"},
                          json={"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}],
                                "generationConfig": gc}, timeout=60)
        if r.status_code != 200:
            raise ValueError("Gemini ত্রুটি %d: %s" % (r.status_code, r.text[:160]))
        j = r.json()
        parts = (((j.get("candidates") or [{}])[0].get("content") or {}).get("parts")) or []
        txt = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not txt:
            raise ValueError("Gemini খালি উত্তর দিয়েছে")
        return txt
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": model or LLM_MODEL, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}, timeout=60)
    if r.status_code != 200:
        raise ValueError("Anthropic ত্রুটি %d: %s" % (r.status_code, r.text[:160]))
    j = r.json()
    return "".join(b.get("text", "") for b in j.get("content", []))


def _extract_json(txt):
    i = txt.find("{")
    if i < 0:
        raise ValueError("LLM JSON ফেরত দেয়নি")
    depth = 0
    for k in range(i, len(txt)):
        if txt[k] == "{":
            depth += 1
        elif txt[k] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(txt[i:k + 1])
    raise ValueError("LLM-এর JSON অসম্পূর্ণ")


def copilot_idea(text, key, caller=None):
    caller = caller or llm_call
    src = ", ".join(sorted(SRC_IDS - {"number"}))
    ops = ", ".join(o[0] for o in OPS)
    system = ("তুমি Green Bull Algo Builder-এর জন্য ট্রেডিং-আইডিয়াকে সেটিংস JSON-এ রূপান্তর করো। শুধু একটি JSON অবজেক্ট ফেরত দাও, অন্য কিছু নয়।\n"
              "স্কিমা: {\"name\":str,\"action\":\"BUY\"|\"SELL\",\"opt\":\"CE\"|\"PE\",\"strike\":{\"mode\":\"offset\",\"off\":int}, "
              "\"entry\":[{\"l\":src,\"op\":op,\"r\":src|\"number\",\"num\":float,\"join\":\"AND\"|\"OR\"}], \"exit\":[same],"
              "\"sl\":{\"mode\":\"pct\"|\"pts\"|\"none\",\"val\":float},\"tp\":{...},\"trail\":{\"mode\":\"none\"|\"pct\",\"val\":float,\"act\":float},\"time_stop\":int}\n"
              "অনুমোদিত src: %s\nঅনুমোদিত op: %s\nঅজানা কিছু বানাবে না; আইডিয়া বোঝা না গেলে {\"error\":\"কারণ\"} দাও। কোনো গাণিতিক ফলাফল দাবি করবে না।" % (src, ops))
    LLM_OPTS["json"] = True
    try:
        out = _extract_json(caller(system, text, key))
    finally:
        LLM_OPTS["json"] = False
    if out.get("error"):
        raise ValueError("আইডিয়া বোঝা যায়নি: %s" % out["error"])
    c = default_case(1)
    for k in ("name", "action", "opt"):
        if out.get(k):
            c[k] = out[k]
    if isinstance(out.get("strike"), dict) and out["strike"].get("mode") == "offset":
        c["strike"]["off"] = int(float(out["strike"].get("off", 0)))
    for part in ("entry", "exit"):
        if part in out:
            lst = []
            for x in out[part] or []:
                if x.get("l") not in SRC_IDS or x.get("l") == "number" or x.get("op") not in {o[0] for o in OPS}:
                    raise ValueError("LLM অবৈধ শর্ত দিয়েছে: %s" % json.dumps(x, ensure_ascii=False)[:100])
                r = x.get("r", "number")
                if r not in SRC_IDS:
                    r = "number"
                lst.append(slot(x["l"], x["op"], r, float(x.get("num", 0) or 0), join=("OR" if str(x.get("join", "AND")).upper() == "OR" else "AND")))
            c[part] = lst
    for k in ("sl", "tp", "trail"):
        if isinstance(out.get(k), dict):
            c[k].update({a: b for a, b in out[k].items() if a in c[k]})
    if out.get("time_stop") is not None:
        c["time_stop"] = int(float(out["time_stop"]))
    chk = norm_settings({"cases": [c]})            # অবৈধ হলে এখানেই ValueError
    return chk["cases"][0]


def copilot_explain(payload, key, caller=None):
    caller = caller or llm_call
    system = ("তুমি একজন কোয়ান্ট-রিসার্চ সহকারী। নিচের JSON ফলাফল সহজ বাংলায় ব্যাখ্যা করো: কী ভালো, কী ঝুঁকিপূর্ণ, কোন সাজেশন বিশ্বাসযোগ্য এবং কেন। "
              "শুধু দেওয়া সংখ্যা ব্যবহার করো, নিজে কোনো গণনা বা নতুন সংখ্যা বানাবে না, ভবিষ্যৎ-প্রতিশ্রুতি দেবে না। ৮–১২ লাইনের মধ্যে রাখো।")
    return caller(system, json.dumps(payload, ensure_ascii=False)[:9000], key)



# =====================================================================
#  পোর্টফোলিও ব্যাকটেস্টার: একাধিক স্ট্র্যাটেজি একসাথে + কোরিলেশন ম্যাট্রিক্স
# =====================================================================
def portfolio_run(D, payload, progress=None):
    items = payload.get("items") or []
    if len(items) < 2:
        raise ValueError("পোর্টফোলিওতে কমপক্ষে ২টি স্ট্র্যাটেজি দিন")
    cap = float(payload.get("capital") or 0)
    d_from, d_to = payload.get("from") or "", payload.get("to") or ""
    runs = []
    for k, it in enumerate(items):
        w = float(it.get("weight") or 1.0)
        if it.get("id") and not it.get("settings"):
            it = dict(it, settings=VAULT.get(it["id"])["settings"])
        raw = copy.deepcopy(it["settings"])
        if d_from:
            raw.setdefault("general", {})["from"] = d_from
        if d_to:
            raw.setdefault("general", {})["to"] = d_to
        if it.get("type") == "sell":
            r = sell_backtest(D, raw)
        else:
            r = backtest(D, raw, light="trades")
        trs = r["trades"]
        runs.append({"name": str(it.get("name") or "S%d" % (k + 1)), "w": w, "trades": trs,
                     "cap": float((raw.get("general") or {}).get("capital") or 100000)})
        if progress:
            progress((k + 1) / (len(items) + 0.3))
    if cap <= 0:
        cap = sum(r["cap"] for r in runs)
    days = sorted({t["date"] for r in runs for t in r["trades"]})
    if len(days) < 5:
        raise ValueError("যৌথ ট্রেডিং-দিন খুব কম (%d)। তারিখ-সীমা বাড়ান।" % len(days))
    idx = {d: i for i, d in enumerate(days)}
    M = np.zeros((len(runs), len(days)))
    merged = []
    for a, r in enumerate(runs):
        for t in r["trades"]:
            M[a, idx[t["date"]]] += t["net"] * r["w"]
            u = dict(t)
            u["net"] = t["net"] * r["w"]
            u["gross"] = t.get("gross", t["net"]) * r["w"]
            u["costs"] = t.get("costs", 0.0) * r["w"]
            u["strat"] = r["name"]
            merged.append(u)
    merged.sort(key=lambda t: (t["date"], t.get("in", "")))
    comb = M.sum(axis=0)

    def dd_of(x):
        e = np.cumsum(x)
        return float((e - np.maximum.accumulate(np.r_[0.0, e])[1:]).min())

    def sh(x):
        sd = x.std(ddof=1) if len(x) > 1 else 0.0
        return float(x.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
    rows = []
    for a, r in enumerate(runs):
        x = M[a]
        yrs = max(len(days) / 252.0, 1e-9)
        dd = dd_of(x)
        rows.append({"name": r["name"], "w": r["w"], "n": len(r["trades"]), "net": float(x.sum()), "dd": dd, "sharpe": sh(x),
                     "calmar": float(x.sum() / yrs / abs(dd)) if dd < 0 else 0.0})
    # কোরিলেশন (দৈনিক P&L)
    C = np.corrcoef(M) if M.shape[0] > 1 else np.ones((1, 1))
    C = np.nan_to_num(C, nan=0.0)
    yrs = max(len(days) / 252.0, 1e-9)
    cdd = dd_of(comb)
    st = calc_stats(merged, cap)
    sum_dd = float(sum(r["dd"] for r in rows))
    out = {"days": days, "names": [r["name"] for r in runs], "rows": rows, "corr": [[round(float(v), 3) for v in row] for row in C],
           "equity": [round(float(v), 2) for v in np.cumsum(comb)],
           "series": [[round(float(v), 2) for v in np.cumsum(M[a])] for a in range(len(runs))],
           "combined": {"n": st["n"], "net": st["net"], "win": st["win"], "pf": st["pf"], "dd": cdd, "sharpe": sh(comb),
                        "calmar": float(comb.sum() / yrs / abs(cdd)) if cdd < 0 else 0.0, "sum_dd": sum_dd,
                        "dd_benefit_pct": float(100 * (1 - cdd / sum_dd)) if sum_dd < 0 else 0.0, "cap": cap},
           "note": "ওজন = লট-গুণক ধরে নেট রৈখিকভাবে বাড়ানো (আনুমানিক)। কোরিলেশন দৈনিক P&L-এর পিয়ার্সন।"}
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
        _hw = lambda w, t: (re.search(r"(?<![a-z0-9])" + re.escape(w.strip()) + r"(?![a-z0-9])", t) is not None) if (w.strip().isascii() and len(w.strip()) <= 3) else (w in t)
        for k, words in P.items():
            if any(_hw(w, c) for w in words):
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



def mc_fan(trades, cap, n=500, seed=7):
    """বুটস্ট্র্যাপ Monte Carlo: ট্রেডের ক্রম ওলট-পালট করে ইকুইটির P5/P25/P50/P75/P95 ফ্যান ও DD বণ্টন"""
    if len(trades) < 10:
        return None
    a = np.array([t["net"] for t in trades], float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(a), size=(n, len(a)))
    paths = np.cumsum(a[idx], axis=1)
    pk = np.maximum.accumulate(np.concatenate([np.zeros((n, 1)), paths], axis=1), axis=1)[:, 1:]
    mdd = (paths - pk).min(axis=1)
    q = np.percentile(paths, [5, 25, 50, 75, 95], axis=0)
    step = max(1, paths.shape[1] // 120)
    return {"p5": q[0][::step].round(0).tolist(), "p25": q[1][::step].round(0).tolist(), "p50": q[2][::step].round(0).tolist(),
            "p75": q[3][::step].round(0).tolist(), "p95": q[4][::step].round(0).tolist(),
            "dd_p50": float(np.percentile(mdd, 50)), "dd_p95": float(np.percentile(mdd, 5)),
            "end_p5": float(q[0][-1]), "end_p50": float(q[2][-1]), "end_p95": float(q[4][-1]),
            "p_profit": float(100 * (paths[:, -1] > 0).mean()), "p_ruin": float(100 * (paths.min(axis=1) < -0.5 * cap).mean()), "n": len(a), "step": step}


def _daily_curve(trades, dates):
    by = {}
    for t in trades:
        by[t["date"]] = by.get(t["date"], 0.0) + t["net"]
    c, out = 0.0, []
    for d in dates:
        c += by.get(d, 0.0)
        out.append(round(c, 0))
    return out


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

    # ---------------- ৩ক) ইন্ডিকেটর-প্যারামিটার ও এন্ট্রি-থ্রেশহোল্ড সার্চ (প্রতিবেশী-যাচাইসহ) ----------------
    IND_MAP = {"ma1": [("ind.ma1.len", [5, 8, 9, 10, 13, 20, 21, 34, 50])], "ma2": [("ind.ma2.len", [5, 8, 9, 10, 13, 20, 21, 34, 50])], "ma3": [("ind.ma3.len", [10, 20, 21, 34, 50, 100])],
               "rsi": [("ind.rsi.len", [7, 9, 14, 21])], "adx": [("ind.adx.len", [7, 10, 14, 20])], "dip": [("ind.adx.len", [7, 10, 14, 20])], "dim": [("ind.adx.len", [7, 10, 14, 20])],
               "atr": [("ind.atr.len", [7, 10, 14, 20])], "bodyatr": [("ind.atr.len", [7, 10, 14, 20])],
               "st1": [("ind.st1.len", [7, 10, 14]), ("ind.st1.mult", [1.0, 1.5, 2.0, 2.5, 3.0])], "st1dir": [("ind.st1.len", [7, 10, 14]), ("ind.st1.mult", [1.0, 1.5, 2.0, 2.5, 3.0])],
               "st2": [("ind.st2.len", [7, 10, 14]), ("ind.st2.mult", [2.0, 2.5, 3.0, 4.0])], "st2dir": [("ind.st2.len", [7, 10, 14]), ("ind.st2.mult", [2.0, 2.5, 3.0, 4.0])],
               "bbup": [("ind.bb.len", [14, 20, 30]), ("ind.bb.mult", [1.5, 2.0, 2.5])], "bblow": [("ind.bb.len", [14, 20, 30]), ("ind.bb.mult", [1.5, 2.0, 2.5])],
               "bbmid": [("ind.bb.len", [14, 20, 30])], "bbw": [("ind.bb.len", [14, 20, 30]), ("ind.bb.mult", [1.5, 2.0, 2.5])],
               "donhi": [("ind.don.n", [10, 15, 20, 30])], "donlo": [("ind.don.n", [10, 15, 20, 30])], "orbh": [("ind.orb.mins", [5, 15, 30])], "orbl": [("ind.orb.mins", [5, 15, 30])]}
    cands_i = []
    seen_p = set()
    for sl_i, sl_ in enumerate(comb["cases"][ci]["entry"]):
        if not sl_.get("on", True):
            continue
        for k_ in ("l", "r"):
            for pth, gv in IND_MAP.get(sl_[k_], []):
                if pth not in seen_p:
                    seen_p.add(pth)
                    cands_i.append(("ind", pth, gv))
        if sl_["r"] == "number":
            v0 = float(sl_.get("num", 0))
            if sl_["l"] in ("rsi",):
                gv = [round(v0 + d, 2) for d in (-10, -5, 5, 10)]
            elif v0 != 0:
                gv = [round(v0 * f, 2) for f in (0.7, 0.85, 1.15, 1.3)]
            else:
                gv = []
            if gv:
                cands_i.append(("slot", "cases.%d.entry.%d.num" % (ci, sl_i), gv))
    ind_rows = []
    cur_s, cur_tr = comb, comb_tr
    n_ind = 0
    for kind_, pth, gv in cands_i:
        if n_ind >= 30:
            break
        base_v = None
        try:
            o_ = cur_s
            for kk in pth.split("."):
                o_ = o_[int(kk)] if isinstance(o_, list) else o_[kk]
            base_v = o_
        except Exception:
            continue
        r0 = robust(cur_tr)
        best = None
        for v in gv:
            if v == base_v:
                continue
            s2 = copy.deepcopy(cur_s)
            set_path(s2, pth, v)
            try:
                tr2 = run(s2)["trades"]
            except Exception:
                continue
            n_ind += 1
            rb = robust(tr2)
            if rb is None or r0 is None:
                continue
            a_tr, a_te = min_split(tr2)
            p_tr, p_te = min_split(cur_tr)
            _, sta = ssc(a_tr); _, stp = ssc(p_tr); _, stta = ssc(a_te); _, sttp = ssc(p_te)
            rec = {"param": pth, "from": base_v, "to": v, "d_train": float(sta["net"] - stp["net"]), "d_test": float(stta["net"] - sttp["net"]), "n": len(tr2), "rob": rb, "rob_prev": r0}
            rec["ok"] = bool(rb > r0 * 1.05 and rec["d_test"] >= 0 and len(tr2) >= 0.6 * len(cur_tr))
            ind_rows.append(rec)
            if rec["ok"] and (best is None or rb > best[0]):
                best = (rb, v, s2, tr2, rec)
        if best:
            # প্ল্যাটো-যাচাই: নির্বাচিত মানের দুই পাশের মান অন্তত ৮৫% স্কোর ধরে রাখতে হবে
            gl = sorted(set([x for x in gv] + [base_v]), key=lambda z: float(z))
            ix = gl.index(best[1])
            nb = [gl[j] for j in (ix - 1, ix + 1) if 0 <= j < len(gl)]
            okp = True
            for v in nb:
                s3 = copy.deepcopy(cur_s)
                set_path(s3, pth, v)
                try:
                    r3 = robust(run(s3)["trades"])
                except Exception:
                    r3 = None
                if r3 is None or r3 < best[0] * 0.85:
                    okp = False
            best[4]["plateau"] = bool(okp)
            if okp:
                best[4]["chosen"] = True
                cur_s, cur_tr = best[2], best[3]
    out["ind_search"] = ind_rows
    if cur_s is not comb:
        comb, comb_tr = cur_s, cur_tr
    def pack(trs):
        a, b2 = min_split(trs)
        return {"all": _tr(calc_stats(trs, cap)), "train": _tr(calc_stats(a, cap)), "test": _tr(calc_stats(b2, cap)),
                "seg_pos": seg_pos(trs), "rob": robust(trs)}
    out["base"] = pack(bt)
    out["exit_best"] = pack(btr)
    out["final"] = pack(comb_tr)
    alld = sorted({t["date"] for t in bt} | {t["date"] for t in comb_tr})
    out["curves"] = {"dates": alld, "base": _daily_curve(bt, alld), "final": _daily_curve(comb_tr, alld), "split": split_date}
    out["mc"] = {"base": mc_fan(bt, cap), "final": mc_fan(comb_tr, cap)}
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
    for r_ in out["ind_search"]:
        if r_.get("chosen"):
            addc("ইন্ডিকেটর/থ্রেশহোল্ড " + r_["param"], r_["from"], r_["to"], [(r_["param"], r_["to"])])
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
                 "cprt", "cprb", "swh", "swl"}
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
    marks["slh"] = [dict(t=(d * 1440 + hm(a)) * 60, p=float(b)) for a, b in (body.get("slh") or []) if isinstance(a, str)]
    if body.get("sl0") is not None:
        marks["sl0"] = float(body["sl0"])
    # --- ভলিউম, FVG বক্স, মাল্টি-ডে প্রেক্ষাপট ---
    vol = []
    if float(np.nansum(B.v[i0:i1])) > 0:
        vol = [dict(t=int(T[k]), v=float(B.v[i0 + k]), up=bool(B.c[i0 + k] >= B.o[i0 + k])) for k in range(i1 - i0)]
    fvg = []
    for k in range(2, i1 - i0):
        a_, b_, c_ = i0 + k - 2, i0 + k - 1, i0 + k
        if B.l[c_] > B.h[a_]:
            lo_, hi_, kind = B.h[a_], B.l[c_], "bull"
        elif B.h[c_] < B.l[a_]:
            lo_, hi_, kind = B.h[c_], B.l[a_], "bear"
        else:
            continue
        end = i1 - i0 - 1
        for m_ in range(k + 1, i1 - i0):           # গ্যাপ ভরাট হলে বক্স সেখানেই শেষ
            if (kind == "bull" and B.l[i0 + m_] <= lo_) or (kind == "bear" and B.h[i0 + m_] >= hi_):
                end = m_
                break
        if (hi_ - lo_) / max(B.c[c_], 1e-9) * 100 >= 0.05:
            fvg.append(dict(a=int(T[k - 2]), b=int(T[end]), lo=float(lo_), hi=float(hi_), k=kind))
    ctx = []
    days_prev = [x for x in np.unique(B.day[:i0])][-2:] if i0 > 0 else []
    for dd_ in days_prev:
        p0 = int(np.searchsorted(B.day, dd_, "left")); p1 = int(np.searchsorted(B.day, dd_, "right"))
        ctx.append(dict(date=day_str(int(dd_)), bars=[[float(B.o[q]), float(B.h[q]), float(B.l[q]), float(B.c[q])] for q in range(p0, p1)]))
    ctx.append(dict(date=day_str(d), bars=[[float(B.o[q]), float(B.h[q]), float(B.l[q]), float(B.c[q])] for q in range(i0, i1)], today=True))
    pd_ = ctx[-2]["bars"] if len(ctx) >= 2 else None
    pdh = max(b_[1] for b_ in pd_) if pd_ else None
    pdl = min(b_[2] for b_ in pd_) if pd_ else None
    return {"spot": spot, "overlays": overlays, "panes": panes, "prem": prem, "lines": lines, "marks": marks,
            "vol": vol, "fvg": fvg[-12:], "ctx": ctx, "pdh": pdh, "pdl": pdl,
            "pov": pov, "ppan": ppan, "pmode": PMC,
            "tf": int(G["tf"]), "base": getattr(D, "base", BASE_TF), "open": OPEN_MIN * 60 + d * 86400}

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
#  StrategyVault — আপডেট-নিরাপদ স্থায়ী স্ট্র্যাটেজি-ভল্ট (কোড-ফোল্ডারের বাইরে)
#  অবস্থান: $GB_VAULT_DIR → <db_dir>/vault → ~/.greenbull_vault
#  গঠন: folders.json, strategies/st_<uuid>.json, trash/st_<uuid>.json
# =====================================================================
VAULT_SCHEMA = 2
GENERAL_FOLDER = "সাধারণ"
LEGACY_FOLDER = "পুরনো প্রিসেট"
DEFAULT_FOLDERS = [GENERAL_FOLDER, "Option Buying", "Option Selling", "Live Ready"]


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, path)


class StrategyVault:
    def __init__(self, root=None):
        self.lock = threading.RLock()
        self._root = root

    # ---- অবস্থান ----
    def root(self):
        if self._root:
            r = self._root
        elif os.environ.get("GB_VAULT_DIR"):
            r = os.environ["GB_VAULT_DIR"]
        else:
            d = (DATA.get("src") or {}).get("db_dir")
            r = os.path.join(d, "vault") if d else os.path.join(os.path.expanduser("~"), ".greenbull_vault")
        try:
            os.makedirs(os.path.join(r, "strategies"), exist_ok=True)
            os.makedirs(os.path.join(r, "trash"), exist_ok=True)
        except Exception:
            r = os.path.join(os.path.expanduser("~"), ".greenbull_vault")
            os.makedirs(os.path.join(r, "strategies"), exist_ok=True)
            os.makedirs(os.path.join(r, "trash"), exist_ok=True)
        return r

    def _p(self, *a):
        return os.path.join(self.root(), *a)

    # ---- ফোল্ডার ----
    def _folders_raw(self):
        fp = self._p("folders.json")
        seeded = False
        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
            fl = [str(x) for x in d.get("folders", [])]
            seeded = True
        except Exception:
            fl = []
        if not seeded:                     # ডিফল্ট ফোল্ডার শুধু প্রথমবার বসে (পরে নাম বদলালে/মুছলে ফিরে আসে না)
            for n in DEFAULT_FOLDERS:
                if n not in fl:
                    fl.append(n)
        if GENERAL_FOLDER in fl:
            fl.remove(GENERAL_FOLDER)
        fl.insert(0, GENERAL_FOLDER)
        return fl

    def _save_folders(self, fl):
        _atomic_write(self._p("folders.json"), {"schema_ver": VAULT_SCHEMA, "folders": fl})

    def folders(self):
        with self.lock:
            self._legacy_import()
            return self._folders_raw()

    def folder_create(self, name):
        name = str(name or "").strip()
        if not name:
            raise ValueError("ফোল্ডারের নাম দিন")
        with self.lock:
            fl = self._folders_raw()
            if name in fl:
                raise ValueError("এই নামে ফোল্ডার আছে")
            fl.append(name)
            self._save_folders(fl)
        return fl

    def folder_rename(self, old, new):
        new = str(new or "").strip()
        with self.lock:
            fl = self._folders_raw()
            if old == GENERAL_FOLDER:
                raise ValueError("'সাধারণ' ফোল্ডারের নাম বদলানো যায় না")
            if old not in fl or not new or (new in fl and new != old):
                raise ValueError("নাম বদলানো গেল না")
            fl[fl.index(old)] = new
            self._save_folders(fl)
            for sub in ("strategies", "trash"):
                for fn in os.listdir(self._p(sub)):
                    if fn.endswith(".json"):
                        rec = self._read(os.path.join(self._p(sub), fn))
                        if rec and rec.get("folder") == old:
                            rec["folder"] = new
                            _atomic_write(os.path.join(self._p(sub), fn), rec)
        return fl

    def folder_delete(self, name):
        with self.lock:
            fl = self._folders_raw()
            if name == GENERAL_FOLDER:
                raise ValueError("'সাধারণ' ফোল্ডার মোছা যায় না")
            if name not in fl:
                raise ValueError("ফোল্ডার নেই")
            moved = 0
            for sub in ("strategies", "trash"):
                for fn in os.listdir(self._p(sub)):
                    if fn.endswith(".json"):
                        rec = self._read(os.path.join(self._p(sub), fn))
                        if rec and rec.get("folder") == name:
                            rec["folder"] = GENERAL_FOLDER          # কখনো স্ট্র্যাটেজি মোছা হয় না
                            _atomic_write(os.path.join(self._p(sub), fn), rec)
                            moved += 1
            fl.remove(name)
            self._save_folders(fl)
        return {"folders": fl, "moved": moved}

    # ---- স্ট্র্যাটেজি ----
    def _read(self, fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            return None
        return self._migrate(rec)

    def _migrate(self, rec):
        if not isinstance(rec, dict) or "settings" not in rec:
            return None
        if int(rec.get("schema_ver", 1)) < VAULT_SCHEMA:
            try:
                rec["settings"] = norm_settings(rec["settings"]) if rec["settings"].get("cases") else rec["settings"]
            except Exception:
                pass
            rec["schema_ver"] = VAULT_SCHEMA
        rec.setdefault("folder", GENERAL_FOLDER)
        rec.setdefault("pinned", False)
        rec.setdefault("note", "")
        return rec

    def _path(self, sid, trash=False):
        sid = os.path.basename(str(sid))
        if not sid.startswith("st_"):
            sid = "st_" + sid
        return os.path.join(self._p("trash" if trash else "strategies"), sid + ".json")

    def _new_rec(self, name, settings, folder, summary, note):
        now = dt.datetime.now().isoformat(timespec="seconds")
        fl = self._folders_raw()
        return {"schema_ver": VAULT_SCHEMA, "id": uuid.uuid4().hex[:12], "name": str(name).strip(), "folder": folder if folder in fl else GENERAL_FOLDER,
                "pinned": False, "note": str(note or ""), "created": now, "updated": now, "settings": settings, "summary": summary or None}

    def save_new(self, name, settings, folder=None, summary=None, note=""):
        if not str(name or "").strip():
            raise ValueError("স্ট্র্যাটেজির নাম দিন")
        with self.lock:
            self._legacy_import()
            rec = self._new_rec(name, settings, folder or GENERAL_FOLDER, summary, note)
            _atomic_write(self._path(rec["id"]), rec)
        return rec["id"]

    def get(self, sid, trash=False):
        with self.lock:
            rec = self._read(self._path(sid, trash))
        if not rec:
            raise ValueError("স্ট্র্যাটেজি পাওয়া যায়নি")
        return rec

    def update(self, sid, settings=None, summary=None, name=None, note=None):
        with self.lock:
            rec = self.get(sid)
            if settings is not None:
                rec["settings"] = settings
            if summary is not None:
                rec["summary"] = summary
            if name:
                rec["name"] = str(name).strip()
            if note is not None:
                rec["note"] = str(note)
            rec["updated"] = dt.datetime.now().isoformat(timespec="seconds")
            _atomic_write(self._path(sid), rec)
        return rec["id"]

    def duplicate(self, sid):
        with self.lock:
            rec = self.get(sid)
            new = self._new_rec(rec["name"] + " (কপি)", copy.deepcopy(rec["settings"]), rec["folder"], copy.deepcopy(rec.get("summary")), rec.get("note"))
            _atomic_write(self._path(new["id"]), new)
        return new["id"]

    def move(self, sid, folder):
        with self.lock:
            if folder not in self._folders_raw():
                raise ValueError("ফোল্ডার নেই")
            rec = self.get(sid)
            rec["folder"] = folder
            _atomic_write(self._path(sid), rec)

    def pin(self, sid, on):
        with self.lock:
            rec = self.get(sid)
            rec["pinned"] = bool(on)
            _atomic_write(self._path(sid), rec)

    def delete(self, sid):
        """সফট ডিলিট: trash/-এ সরানো"""
        with self.lock:
            rec = self.get(sid)
            rec["deleted"] = dt.datetime.now().isoformat(timespec="seconds")
            _atomic_write(self._path(sid, True), rec)
            os.remove(self._path(sid))

    def restore(self, sid):
        with self.lock:
            rec = self.get(sid, True)
            rec.pop("deleted", None)
            if rec.get("folder") not in self._folders_raw():
                rec["folder"] = GENERAL_FOLDER
            _atomic_write(self._path(sid), rec)
            os.remove(self._path(sid, True))

    def purge(self, sid):
        """স্থায়ী মোছা — শুধু ট্র্যাশ থেকে"""
        with self.lock:
            fp = self._path(sid, True)
            if not os.path.exists(fp):
                raise ValueError("শুধু ট্র্যাশের স্ট্র্যাটেজি স্থায়ীভাবে মোছা যায়")
            os.remove(fp)

    def list(self, trash=False, with_settings=False):
        out = []
        with self.lock:
            self._legacy_import()
            d = self._p("trash" if trash else "strategies")
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".json"):
                    continue
                rec = self._read(os.path.join(d, fn))
                if rec:
                    if not with_settings:
                        rec = {k: v for k, v in rec.items() if k != "settings"}
                        rec["legs"] = None
                    out.append(rec)
        return out

    # ---- পুরনো presets.json আমদানি (একবার, মূল ফাইল না মুছে) ----
    def _legacy_import(self):
        mk = self._p(".presets_migrated")
        if os.path.exists(mk) or getattr(self, "_importing", False):
            return
        self._importing = True
        try:
            legacy = load_user_presets()
            n = 0
            if legacy:
                fl = self._folders_raw()
                if LEGACY_FOLDER not in fl:
                    fl.append(LEGACY_FOLDER)
                    self._save_folders(fl)
                for name, st in legacy.items():
                    meta = (st or {}).get("_meta") or {}
                    rec = self._new_rec(name, st, LEGACY_FOLDER, meta.get("stats"), meta.get("note", ""))
                    _atomic_write(self._path(rec["id"]), rec)
                    n += 1
            _atomic_write(mk, {"migrated": n, "at": dt.datetime.now().isoformat(timespec="seconds")})
        except Exception:
            traceback.print_exc()
        finally:
            self._importing = False


VAULT = StrategyVault()


def vault_api(op, b):
    V = VAULT
    if op == "list":
        return {"folders": V.folders(), "items": V.list(), "trash": len(V.list(trash=True)), "dir": V.root()}
    if op == "trash_list":
        return {"items": V.list(trash=True)}
    if op == "get":
        return V.get(b["id"], bool(b.get("trash")))
    if op == "save":
        sid = V.save_new(b.get("name"), b["settings"], b.get("folder"), b.get("summary"), b.get("note", ""))
        return {"id": sid, **vault_api("list", {})}
    if op == "update":
        V.update(b["id"], b.get("settings"), b.get("summary"), b.get("name"), b.get("note"))
    elif op == "duplicate":
        return {"id": V.duplicate(b["id"]), **vault_api("list", {})}
    elif op == "move":
        V.move(b["id"], b.get("folder"))
    elif op == "pin":
        V.pin(b["id"], b.get("on", True))
    elif op == "delete":
        V.delete(b["id"])
    elif op == "restore":
        V.restore(b["id"])
    elif op == "purge":
        V.purge(b["id"])
    elif op == "folder_create":
        V.folder_create(b.get("name"))
    elif op == "folder_rename":
        V.folder_rename(b.get("old"), b.get("new"))
    elif op == "folder_delete":
        V.folder_delete(b.get("name"))
    else:
        raise ValueError("অজানা ভল্ট-অপারেশন")
    return vault_api("list", {})
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
        keep = set(getattr(self, 'hold_keys', ()) or ())     # খোলা ট্রেডের কন্ট্র্যাক্ট কখনো বাদ নয়
        far = {k for k in self.fetched if k not in keep and abs(strikes.index(k[1]) - i) > self.band + 2} if self.fetched else set()
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
            _o = sess.get('open')
            if _o:
                try:
                    _p = _o['side'].split()
                    LS.hold_keys = {(_p[1], float(_p[2]))}
                except Exception:
                    LS.hold_keys = set()
            else:
                LS.hold_keys = set()
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
                                   "presets": {"builtin": builtin_presets(), "user": {}}, "vault": vault_api("list", {}), "vault_dir": VAULT.root()})
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
                LLM_OPTS["model"] = (body.get("model") or "").strip() or None
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
            if u.path.startswith("/api/vault/"):
                return self._json(vault_api(u.path[len("/api/vault/"):], body))
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
select,input[type=text],input[type=number],input[type=password],input[type=date],input[type=time],textarea{background:#0a120e;color:var(--tx);border:1px solid var(--line);border-radius:10px;padding:9px 10px;font-size:14px;width:100%;min-width:0;transition:border-color .15s,box-shadow .15s}
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
  <button data-tab="sell"><svg viewBox="0 0 24 24"><path d="M3 17l5-7 4 4 4-9 5 8"/><path d="M3 21h18"/></svg>Selling</button>
  <button data-tab="paper"><svg viewBox="0 0 24 24"><path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 13h7M9 17h5"/></svg>Paper</button>
  <button data-tab="pre"><svg viewBox="0 0 24 24"><path d="M6 3h12v18l-6-4-6 4z"/></svg>Library</button>
  <button data-tab="data"><svg viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/></svg>Data</button>
</nav>
<main>
  <section id="t-build"></section>
  <section id="t-res" hidden><div class="note">এখনও কোনো রান হয়নি। নিচে ▶ Run চাপুন।</div></section>
  <section id="t-tune" hidden></section>
  <section id="t-ai" hidden></section>
  <section id="t-sell" hidden></section>
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
      F('সাইজিং মোড', selCtl('general.size_mode', [['fixed', 'ফিক্সড লট'], ['compound', 'কম্পাউন্ডিং (মূলধন বাড়লে লট বাড়ে)'], ['risk', 'মূলধনের % ঝুঁকি (SL ধরে লট)']])),
      F('ঝুঁকি % প্রতি ট্রেড (risk মোডে)', numCtl('general.risk_pct', 'ঝুঁকি %', 0.25)),
      F('সর্বোচ্চ লট (কম্পাউন্ড/risk মোডে)', numCtl('general.max_lots', 'সর্বোচ্চ লট', 1)),
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


const MIR_SWAP = {orbh: 'orbl', orbl: 'orbh', donhi: 'donlo', donlo: 'donhi', bbup: 'bblow', bblow: 'bbup', pdh: 'pdl', pdl: 'pdh', swh: 'swl', swl: 'swh', fvgb: 'fvgs', fvgs: 'fvgb', r1: 's1', s1: 'r1', r2: 's2', s2: 'r2', r3: 's3', s3: 'r3', cprt: 'cprb', cprb: 'cprt'};
const MIR_OP = {cross_above: 'cross_below', cross_below: 'cross_above', above: 'below', below: 'above', rising: 'falling', falling: 'rising'};
const MIR_NEUTRAL = new Set(['adx', 'atr', 'volume', 'volsma', 'bbw', 'rng', 'body', 'bodyp', 'bodyatr', 'uw', 'lw', 'uwp', 'lwp', 'minsopen', 'cprw', 'orbr', 'gap', 'insb', 'nr7', 'rvol', 'number']);
function mirrorCase(c) {
  const m = JSON.parse(JSON.stringify(c));
  m.opt = c.opt === 'CE' ? 'PE' : 'CE'; m.name = c.name + ' → ' + m.opt + ' মিরর';
  const fl = s => {
    if (MIR_NEUTRAL.has(s.l)) return;
    s.op = MIR_OP[s.op] || s.op;
    if (MIR_SWAP[s.l]) s.l = MIR_SWAP[s.l];
    if (MIR_SWAP[s.r]) s.r = MIR_SWAP[s.r];
    if (s.l === 'rsi' && s.r === 'number') s.num = Math.round((100 - s.num) * 100) / 100;
    if ((s.l === 'cdir' || s.l === 'bos' || s.l === 'choch') && s.r === 'number') s.num = -s.num;
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
let VLT = {folder: 'all', q: '', sort: 'upd', active: null, trash: false, trashItems: []};
try { VLT.active = localStorage.getItem('gb_active_id') || null; } catch (e) {}
function setActive(id) { VLT.active = id; try { if (id) localStorage.setItem('gb_active_id', id); else localStorage.removeItem('gb_active_id'); } catch (e) {} }
function vsummary() {
  if (LAST && LASTRUN === JSON.stringify(S)) { const a = LAST.stats.all; return {n: a.n, net: Math.round(a.net), pf: +a.pf.toFixed(2), win: +a.win.toFixed(1), dd: Math.round(a.dd), from: LAST.meta.from, to: LAST.meta.to}; }
  return null;
}
async function saveStrategy(name, note, folder) {
  name = (name || '').trim();
  if (!name) { alert('স্ট্র্যাটেজির নাম দিন'); return false; }
  const r = await api('/api/vault/save', {name, note: (note || '').trim(), settings: JSON.parse(JSON.stringify(S)), summary: vsummary(), folder: folder || (VLT.folder !== 'all' && VLT.folder !== '__trash' ? VLT.folder : null)});
  if (r.error) { alert(r.error); return false; }
  META.vault = r; setActive(r.id); renderPresets();
  $('#status').textContent = '✓ ভল্টে সেভ হয়েছে: ' + name;
  return true;
}
async function updateActive() {
  if (!VLT.active) return false;
  const r = await api('/api/vault/update', {id: VLT.active, settings: JSON.parse(JSON.stringify(S)), summary: vsummary()});
  if (r.error) { alert(r.error); return false; }
  META.vault = r; renderPresets(); $('#status').textContent = '✓ আপডেট হয়েছে (একই স্ট্র্যাটেজি)'; return true;
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
  if (N.adv && !N.adv.err) drawAdv(root, N.adv);
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

/* ---------- উন্নত অ্যানালিটিক্স ও চার্ট ---------- */
function heatCell(v, mx) {
  const a = Math.min(1, Math.abs(v) / (mx || 1)) * 0.75;
  return '<td style="background:' + (v >= 0 ? 'rgba(38,194,129,' : 'rgba(255,93,93,') + a.toFixed(2) + ');text-align:center;font-size:10.5px;padding:3px 2px">' + (v === 0 ? '' : (Math.abs(v) >= 1000 ? (v / 1000).toFixed(1) + 'k' : Math.round(v))) + '</td>';
}
function drawAdv(root, V) {
  root.append(h('h3', {}, '📐 রিস্ক-অ্যাডজাস্টেড মেট্রিক্স'));
  root.append(tbl(['মেট্রিক', 'মান', 'মানে'], [
    ['Sharpe (দৈনিক P&L, ×√252)', f2(V.sharpe), '১+ ভালো, ২+ খুব ভালো'],
    ['Sortino', f2(V.sortino), 'শুধু লসের ওঠানামা ধরে'],
    ['Calmar (বার্ষিক নেট ÷ Max DD)', f2(V.calmar), '১+ গ্রহণযোগ্য'],
    ['SQN', f2(V.sqn), '২+ ভালো, ৩+ খুব ভালো (১০০+ ট্রেডে অর্থপূর্ণ)'],
    ['E-ratio (গড় MFE ÷ গড় MAE)', f2(V.eratio), '১-এর বেশি = এন্ট্রিতে সামান্য ধার আছে'],
    ['বার্ষিক নেট (আনুমানিক)', pn(V.ann_net) + ' (' + V.ann_pct.toFixed(0) + '% মূলধনের)', V.days + ' ট্রেডিং-দিনের ভিত্তিতে'],
    ['সবচেয়ে লম্বা আন্ডারওয়াটার', V.uw_max + ' দিন', 'এখন: ' + V.uw_now + ' দিন'],
    ['গড় বিনিয়োগ প্রতি ট্রেড (প্রিমিয়াম)', inr(V.avg_deployed), 'বিনিয়োগের ওপর ROI: ' + V.roi_deployed.toFixed(1) + '%'],
    ['লাভজনক দিন (ট্রেড হওয়া দিনের)', V.daily_win.toFixed(0) + '%', '']]));
  const R = V.regime || {}, keys = Object.keys(R);
  if (keys.length) {
    root.append(h('h3', {}, '🌦 মার্কেট রেজিম অনুযায়ী (সিগন্যালের সময়ের মান)'));
    keys.forEach(k => root.append(tbl(['রেজিম', 'ট্রেড', 'Win%', 'নেট ₹', 'PF', 'গড়/ট্রেড'], R[k].map(r => [r.k, r.n, r.win.toFixed(0) + '%', pn(r.net), f2(r.pf), pn(r.exp)]))));
    root.append(h('div', {class: 'note'}, 'কোন রেজিমে নেট ঋণাত্মক হলে সেখানে এন্ট্রি ফিল্টার (যেমন ADX শর্ত) যোগ করার কথা ভাবুন — AI v2-র ফিল্টার মাইনিং এটা যাচাই করে দেখে।'));
  }
  if (V.sync) {
    root.append(h('h3', {}, '📈 ইকুইটি + আন্ডারওয়াটার DD + স্পট বেঞ্চমার্ক'));
    const c3 = h('canvas', {style: 'width:100%;height:260px;background:#10151d;border-radius:10px'}); root.append(c3);
    root.append(h('div', {class: 'note'}, 'সবুজ = আপনার ইকুইটি; ধূসর = স্পট ইনডেক্স (একই মূলধনে বাই-অ্যান্ড-হোল্ড); নিচের লাল অংশ = ডড্রডাউন (শিখর থেকে কত নিচে)।'));
    setTimeout(() => {
      const W = c3.clientWidth || 340, H = 260, dpr = Math.min(window.devicePixelRatio || 1, 2); c3.width = W * dpr; c3.height = H * dpr; const g = c3.getContext('2d'); g.scale(dpr, dpr);
      const Y1 = V.sync, n = Y1.eq.length, L = 8, R = 8, top = 8, split = H * 0.68, bot = H - 14;
      const a1 = Y1.eq.concat(Y1.spot, [0]), mx = Math.max(...a1), mn = Math.min(...a1), dm = Math.min(...Y1.dd, -1);
      const X = i => L + (W - L - R) * i / Math.max(n - 1, 1), Ya = v => top + (split - top - 6) * (1 - (v - mn) / Math.max(mx - mn, 1)), Yb = v => split + 8 + (bot - split - 8) * (v / dm);
      g.strokeStyle = '#1c2431'; g.beginPath(); g.moveTo(L, Ya(0)); g.lineTo(W - R, Ya(0)); g.stroke();
      const ln = (arr, col, f, w) => { g.strokeStyle = col; g.lineWidth = w; g.beginPath(); arr.forEach((v, i) => i ? g.lineTo(X(i), f(v)) : g.moveTo(X(i), f(v))); g.stroke(); };
      ln(Y1.spot, '#8b96a8', Ya, 1.2); ln(Y1.eq, '#26c281', Ya, 2);
      g.fillStyle = 'rgba(255,93,93,.35)'; g.beginPath(); g.moveTo(X(0), Yb(0)); Y1.dd.forEach((v, i) => g.lineTo(X(i), Yb(v))); g.lineTo(X(n - 1), Yb(0)); g.closePath(); g.fill();
      g.fillStyle = '#7d889b'; g.font = '10px sans-serif'; g.textAlign = 'left'; g.fillText(Y1.dates[0], L, H - 2); g.textAlign = 'right'; g.fillText(Y1.dates[n - 1], W - R, H - 2);
      g.textAlign = 'left'; g.fillText(Math.round(mx).toLocaleString('en-IN'), L + 2, top + 8); g.fillText(Math.round(dm).toLocaleString('en-IN'), L + 2, bot - 2);
    }, 0);
  }
  if (V.top_dd && V.top_dd.length) {
    root.append(h('h3', {}, '🕳 টপ-৫ ড্রডাউন পিরিয়ড ও স্থবিরতা'));
    root.append(tbl(['শুরু', 'তলা', 'পুনরুদ্ধার', 'DD ₹', 'দিন'], V.top_dd.map(d => [d.from, d.trough, d.to || 'এখনও হয়নি', pn(d.dd), d.days])));
    root.append(h('div', {class: 'note'}, 'ইকুইটি নতুন শিখর না ছুঁয়ে সর্বোচ্চ ' + V.stagnation_days + ' ক্যালেন্ডার দিন কেটেছে — এই ধৈর্য আপনার আছে কি না আগে ভেবে নিন।'));
  }
  if (V.eratio_decay && V.eratio_decay.length) {
    root.append(h('h3', {}, '⏱ টাইম-ডিকে E-Ratio (এন্ট্রির পর গড় MFE ÷ MAE)'));
    root.append(tbl(['এন্ট্রির পর', 'ট্রেড', 'গড় MFE %', 'গড় MAE %', 'E-ratio'], V.eratio_decay.map(e => [e.min + ' মিনিট', e.n, e.mfe.toFixed(1), e.mae.toFixed(1), '<b>' + e.ratio.toFixed(2) + '</b>'])));
    root.append(h('div', {class: 'note'}, 'প্রিমিয়ামের শতাংশে। ১-এর বেশি এবং সময়ের সাথে বাড়লে এন্ট্রি-সিগন্যালে সত্যিকারের এজ আছে; কোন মিনিটে ratio সর্বোচ্চ সেটাই আদর্শ টাইম-স্টপের ইঙ্গিত।'));
  }
  /* MAE/MFE স্ক্যাটার */
  root.append(h('h3', {}, '🎯 MAE বনাম MFE (প্রতি ট্রেড)'));
  const cv = h('canvas', {style: 'width:100%;height:230px;background:#10151d;border-radius:10px'});
  root.append(cv);
  root.append(h('div', {class: 'note'}, 'অনুভূমিক = সর্বোচ্চ উল্টো যাওয়া (MAE %), উল্লম্ব = সর্বোচ্চ ফেভারে যাওয়া (MFE %)। সবুজ = জেতা, লাল = হারা। লাল বিন্দু বাঁ-নিচে জমলে SL আরও টাইট করা যায়; সবুজ ডানে ছড়ালে SL ঢিলে রাখা দরকার।'));
  setTimeout(() => {
    const W = cv.clientWidth || 340, H = 230, dpr = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = W * dpr; cv.height = H * dpr; const g = cv.getContext('2d'); g.scale(dpr, dpr);
    const P = V.scatter, mx = Math.max(10, ...P.map(p => p[0])) * 1.05, my = Math.max(10, ...P.map(p => p[1])) * 1.05, L = 34, B = 20;
    g.strokeStyle = '#1c2431'; g.fillStyle = '#7d889b'; g.font = '10px sans-serif';
    for (let i = 0; i <= 4; i++) {
      const x = L + (W - L - 6) * i / 4, y = 6 + (H - B - 6) * (1 - i / 4);
      g.beginPath(); g.moveTo(x, 6); g.lineTo(x, H - B); g.stroke(); g.beginPath(); g.moveTo(L, y); g.lineTo(W - 6, y); g.stroke();
      g.textAlign = 'center'; g.fillText((mx * i / 4).toFixed(0), x, H - 6); g.textAlign = 'right'; g.fillText((my * i / 4).toFixed(0), L - 3, y + 3);
    }
    P.forEach(p => { g.fillStyle = p[2] ? 'rgba(38,194,129,.55)' : 'rgba(255,93,93,.55)'; g.beginPath(); g.arc(L + p[0] / mx * (W - L - 6), 6 + (H - B - 6) * (1 - p[1] / my), 2.6, 0, 6.3); g.fill(); });
  }, 0);
  /* হিটম্যাপ: দিন × সময় */
  const slots = [...new Set(V.heat_slot.map(x => x.slot))].sort(), dows = ['সোম', 'মঙ্গল', 'বুধ', 'বৃহ', 'শুক্র'];
  const hm = {}; let m1 = 1; V.heat_slot.forEach(x => { hm[x.dow + x.slot] = x.net; m1 = Math.max(m1, Math.abs(x.net)); });
  root.append(h('h3', {}, '🔥 হিটম্যাপ: সময়-স্লট × সপ্তাহের দিন (নেট ₹)'));
  root.append(h('div', {class: 'scroll', html: '<table><tr><th></th>' + dows.map(d => '<th>' + d + '</th>').join('') + '</tr>' + slots.map(sl => '<tr><th>' + sl + '</th>' + dows.map(d => heatCell(hm[d + sl] || 0, m1)).join('') + '</tr>').join('') + '</table>'}));
  /* হিটম্যাপ: বছর × মাস */
  const ys = [...new Set(V.heat_month.map(x => x.y))], hmm = {}; let m2 = 1; V.heat_month.forEach(x => { hmm[x.y + '-' + x.m] = x.net; m2 = Math.max(m2, Math.abs(x.net)); });
  const MN = ['জানু', 'ফেব্রু', 'মার্চ', 'এপ্রি', 'মে', 'জুন', 'জুলা', 'আগ', 'সেপ্টে', 'অক্টো', 'নভে', 'ডিসে'];
  root.append(h('h3', {}, '📅 মাসিক হিটম্যাপ (নেট ₹)'));
  root.append(h('div', {class: 'scroll', html: '<table><tr><th></th>' + MN.map(m => '<th>' + m + '</th>').join('') + '</tr>' + ys.map(y => '<tr><th>' + y + '</th>' + MN.map((m, i) => hmm[y + '-' + (i + 1)] == null ? '<td></td>' : heatCell(hmm[y + '-' + (i + 1)], m2)).join('') + '</tr>').join('') + '</table>'}));
  /* স্ট্রেস টেস্ট */
  const box = h('div');
  root.append(h('h3', {}, '🧪 স্লিপেজ / খরচ স্ট্রেস টেস্ট'));
  root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, h('button', {class: 'btn p', on: {click: async () => {
    const r = await runJob({kind: 'stress', settings: S}, 'স্ট্রেস টেস্ট');
    if (r) { box.innerHTML = ''; box.append(tbl(['পরিস্থিতি', 'ট্রেড', 'নেট ₹', 'PF', 'Win%', 'Max DD'], r.rows.map(x => [x.k, x.n, pn(x.net), f2(x.pf), x.win.toFixed(0) + '%', inr(x.dd)]))); box.append(h('div', {class: 'note'}, 'স্লিপেজ ×৩-এও নেট ধনাত্মক থাকলে স্ট্র্যাটেজি বাস্তবে (খারাপ ফিলে) টিকবে বলে আশা করা যায়; ×২-এই ঋণাত্মক হলে এজ খুব পাতলা।')); }
  }}}, '▶ স্ট্রেস টেস্ট চালান'), h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, 'আসল ইঞ্জিনে ৬ বার চালায় (স্লিপেজ ২×/৩×, ব্রোকারেজ ১.৫×, +১/+২ ক্যান্ডেল দেরিতে এন্ট্রি)।'))));
  root.append(box);
}
/* ---------- একটি ট্রেডের চার্ট (স্পট + প্রিমিয়াম) ---------- */
const COLS = ['#f1c40f', '#c678dd', '#56b6c2', '#e5894f', '#98c379', '#e06c9f'];
function hmOf(t) { const d = new Date(t * 1000); return String(d.getUTCHours()).padStart(2, '0') + ':' + String(d.getUTCMinutes()).padStart(2, '0'); }
/* ---------- চার্ট (নতুন): পিঞ্চ-জুম, ক্রসহেয়ার, সিঙ্ক ---------- */
let CH = {i: 0};
const CHS = {view: 'both', hide: {}, vol: true, fvg: true, ctx: false, ruler: false};
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
  (o.steps || []).forEach(L => L.pts.forEach(p => { if (vis(p.t)) vals.push(p.v); })); (o.hl || []).forEach(v => vals.push(v)); (o.hlines || []).forEach(L => vals.push(L.v));
  (o.hist || []).forEach(k => { if (vis(k.t)) vals.push(k.v, 0); });
  g.fillStyle = '#10151d'; g.fillRect(0, 0, W, H);
  if (!vals.length) { g.fillStyle = '#8b96a8'; g.font = '11px sans-serif'; g.fillText(o.title + ' — এই অংশে ডেটা নেই', 8, 14); return; }
  let mn = Math.min(...vals), mx = Math.max(...vals); if (mx === mn) { mx += 1; mn -= 1; }
  const rg = mx - mn, pd = rg * 0.07; mn -= pd; mx += pd;
  const Y = v => padT + (H - padT - padB) * (1 - (v - mn) / (mx - mn));
  cv._inv = y => mn + (mx - mn) * (1 - (y - padT) / (H - padT - padB));
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
  // FVG বক্স ও ভলিউম হিস্টোগ্রাম
  (o.boxes || []).forEach(bx => { const xa = X(bx.a), xb = X(bx.b + o.step), y1 = Y(bx.hi), y2 = Y(bx.lo); g.fillStyle = bx.k === 'bull' ? 'rgba(38,194,129,.14)' : 'rgba(255,93,93,.14)'; g.fillRect(xa, y1, Math.max(xb - xa, 2), Math.max(y2 - y1, 1)); g.fillStyle = bx.k === 'bull' ? '#26c281' : '#ff5d5d'; g.font = '9px sans-serif'; g.textAlign = 'left'; g.fillText('FVG', xa + 2, y1 + 8); });
  (o.hist || []).forEach(k => { if (!vis(k.t)) return; const w = Math.max(1, (X(k.t + o.step) - X(k.t)) * 0.7), xc = X(k.t + o.step / 2); g.fillStyle = k.up ? 'rgba(38,194,129,.7)' : 'rgba(255,93,93,.7)'; g.fillRect(xc - w / 2, Y(k.v), w, Math.max(1, Y(0) - Y(k.v))); });
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
  if (R.ruler && R.ruler.id === cv._id && R.ruler.a) {
    const A = R.ruler.a, Bp = R.ruler.b || null, xa = X(A.t), ya = Y(A.p);
    g.fillStyle = '#f5c542'; g.beginPath(); g.arc(xa, ya, 4, 0, 6.3); g.fill();
    if (Bp) {
      const xb = X(Bp.t), yb = Y(Bp.p), dp = Bp.p - A.p, pc_ = A.p ? dp / A.p * 100 : 0, nb = Math.round((Bp.t - A.t) / o.step);
      g.strokeStyle = '#f5c542'; g.lineWidth = 1.5; g.setLineDash([5, 3]); g.beginPath(); g.moveTo(xa, ya); g.lineTo(xb, yb); g.stroke(); g.setLineDash([]);
      g.beginPath(); g.arc(xb, yb, 4, 0, 6.3); g.fill();
      pill(g, (dp >= 0 ? '+' : '') + fnum(dp, 100) + ' (' + (pc_ >= 0 ? '+' : '') + pc_.toFixed(2) + '%) · ' + nb + ' বার', Math.min(Math.max((xa + xb) / 2, 70), W - padR - 70), Math.min(ya, yb) - 14, '#f5c542', '#0b0f14', 'center');
    }
  }
  // ট্রেইলিং / ব্রেকইভেন SL-এর সিঁড়ি-রেখা
  (o.steps || []).forEach(L => {
    if (L.pts.length < 2) return;
    g.strokeStyle = L.color; g.lineWidth = 1.6; g.setLineDash([2, 2]); g.beginPath();
    L.pts.forEach((p, i) => { const x = X(p.t + o.step / 2), y = Y(p.v); i ? g.lineTo(x, y) : g.moveTo(x, y); });
    g.stroke(); g.setLineDash([]);
    const q = L.pts[L.pts.length - 1]; pill(g, 'SL ' + fnum(q.v, 100), X(q.t + o.step / 2), Y(q.v) + 12, L.color, '#0b0f14', 'center');
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
function stepSL(d, mIn, mOut) {
  const sl = d.marks && d.marks.slh; if (!mIn || !mOut || !sl || !sl.length || d.marks.sl0 == null) return [];
  const pts = [{t: mIn.t, v: d.marks.sl0}]; let cur = d.marks.sl0;
  sl.forEach(x => { pts.push({t: x.t, v: cur}); pts.push({t: x.t, v: x.p}); cur = x.p; });
  pts.push({t: mOut.t, v: cur});
  return [{color: '#e5894f', pts}];
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
  try { d = await api('/api/chart', {settings: JSON.parse(LASTRUN || JSON.stringify(S)), date: T.date, side: T.side, expiry: T.expiry, in: T.in, out: T.out, entry: T.entry, exit: T.exit, case: T.case, legs: T.legs, slh: T.slh, sl0: T.sl0}); } catch (e) { d = {error: String(e)}; }
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
  const ctxBox = h('div', {style: 'margin:0 10px 8px'}); ov.append(ctxBox);
  const drawCtxBox = () => { ctxBox.innerHTML = ''; if (!CHS.ctx || !d.ctx) return; const cv = h('canvas', {style: 'display:block;border-radius:8px'}); ctxBox.append(h('div', {style: 'font-size:11px;color:#8b96a8;margin-bottom:3px'}, 'মাল্টি-ডে প্রেক্ষাপট (আগের দিনগুলো + আজ, ' + d.tf + 'm বার; PDH/PDL ও গ্যাপ)'), cv); drawCtx(cv, d); };
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
      list.push(['spot', Hm, {title: 'স্পট ' + d.tf + 'm', bars: d.spot, step: tfs, lstep: tfs, lines: lines(d.overlays, 's:'), zone, boxes: CHS.fvg ? d.fvg : [], hlines: [d.pdh != null ? {v: d.pdh, name: 'PDH', color: '#8b96a8'} : null, d.pdl != null ? {v: d.pdl, name: 'PDL', color: '#8b96a8'} : null].filter(Boolean), marks: d.marks.spot.map(m => Object.assign({win}, m))}]);
      if (CHS.vol && d.vol && d.vol.length) list.push(['vol', 90, {title: 'ভলিউম (শেষ বারের RVOL: ' + rvolTxt(d) + ')', step: tfs, lstep: tfs, hist: d.vol, zone}]);
      (d.panes || []).forEach((p, k) => list.push(['sp' + k, 110, {title: p.name, step: tfs, lstep: tfs, lines: [{name: '', data: p.data, color: '#56b6c2'}], hl: p.hl, zone}]));
    }
    if (v !== 'spot' && d.prem.length) {
      list.push(['prem', Hm, {title: 'প্রিমিয়াম ' + T.side.replace(/^BUY |^SELL /, '') + (d.pmode ? ' · ' + d.tf + 'm সিগন্যাল' : ''), bars: d.prem, step: base, lstep: tfs, lines: lines(d.pov, 'p:'), hlines: d.lines, steps: stepSL(d, mIn, mOut), zone, bands, marks: d.marks.prem.map(m => Object.assign({win}, m))}]);
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
    ctl.append(chip('পুরো দিন', fitDay), chip('ট্রেডে ফোকাস', fitTrade), chip('＋', () => zoom(0.6)), chip('－', () => zoom(1.6)), chip('⟵', () => pan(-0.3)), chip('⟶', () => pan(0.3)),
      chip('📏 রুলার', () => { CHS.ruler = !CHS.ruler; R.ruler = null; drawCtl(); redraw(); }, CHS.ruler), chip('▮ ভলিউম', () => { CHS.vol = !CHS.vol; drawCtl(); build(); }, CHS.vol), chip('▭ FVG', () => { CHS.fvg = !CHS.fvg; drawCtl(); build(); }, CHS.fvg), chip('📆 ৩ দিন', () => { CHS.ctx = !CHS.ctx; drawCtl(); drawCtxBox(); }, CHS.ctx));
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
      if (CHS.ruler && L.cv._inv) {
        const tt = t0 + Math.floor((tAt(L.x) - t0) / L.cv._o.step) * L.cv._o.step, pt = {t: tt, p: L.cv._inv(L.y)};
        if (!R.ruler || R.ruler.id !== L.cv._id || R.ruler.b) R.ruler = {id: L.cv._id, a: pt, b: null}; else R.ruler.b = pt;
        redraw(); return;
      }
      const now = Date.now(); if (now - tapAt < 320) { fitDay(); tapAt = 0; } else tapAt = now;
    }
  };
  box.addEventListener('pointerup', up); box.addEventListener('pointercancel', e => { P.delete(e.pointerId); pinch = null; });
  box.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') { R.cx = null; redraw(); } });
  box.addEventListener('wheel', e => { e.preventDefault(); const L = local(e), t = tAt(L.x), k = e.deltaY > 0 ? 1.18 : 0.85, f = (L.x - 2) / plotW(); const span = (R.b - R.a) * k; setRange(t - f * span, t - f * span + span); }, {passive: false});
  ov._rs && window.removeEventListener('resize', ov._rs); ov._rs = () => resize(); window.addEventListener('resize', ov._rs);
  drawCtl(); build(); drawCtxBox();
  ov.append(h('div', {class: 'note', style: 'margin:0 10px 24px'}, 'IN = এন্ট্রি (নীল), OUT = এক্সিট (সবুজ/লাল)। প্রিমিয়ামে নীল রেখা = এন্ট্রি, লাল = প্রথম SL, সবুজ = টার্গেট; হালকা লাল/সবুজ জোন = ঝুঁকি/লাভের এলাকা। ইন্ডিকেটর শুধু এই Case-এর শর্তে ব্যবহার হওয়াগুলো দেখায়।'));
}
function rvolTxt(d) { try { const v = d.vol.map(x => x.v), n = v.length; if (n < 6) return '–'; const avg = v.slice(0, n - 1).reduce((a, b) => a + b, 0) / (n - 1); return (v[n - 1] / avg).toFixed(2) + '×'; } catch (e) { return '–'; } }
function drawCtx(cv, d) {
  const W = Math.max(cv.parentNode.clientWidth - 20, 220), H = 190, dpr = Math.min(window.devicePixelRatio || 1, 2);
  cv.width = W * dpr; cv.height = H * dpr; cv.style.width = W + 'px'; cv.style.height = H + 'px'; const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.fillStyle = '#10151d'; g.fillRect(0, 0, W, H);
  const all = []; d.ctx.forEach(c => c.bars.forEach(b => all.push(b))); if (!all.length) return;
  let mx = Math.max(...all.map(b => b[1])), mn = Math.min(...all.map(b => b[2])); const pd = (mx - mn) * 0.06; mx += pd; mn -= pd;
  const n = all.length, L = 4, R_ = 52, bw = (W - L - R_) / n, Y = v => 8 + (H - 22) * (1 - (v - mn) / (mx - mn));
  let k = 0;
  d.ctx.forEach((c, ci) => {
    c.bars.forEach(b => { const x = L + (k + 0.5) * bw, up = b[3] >= b[0]; g.strokeStyle = g.fillStyle = up ? CU : CD; g.beginPath(); g.moveTo(x, Y(b[1])); g.lineTo(x, Y(b[2])); g.stroke(); g.fillRect(x - Math.max(bw * 0.35, 0.5), Y(Math.max(b[0], b[3])), Math.max(bw * 0.7, 1), Math.max(1, Math.abs(Y(b[0]) - Y(b[3])))); k++; });
    if (ci > 0) { const x = L + (k - c.bars.length) * bw; g.strokeStyle = '#2a3445'; g.beginPath(); g.moveTo(x, 4); g.lineTo(x, H - 14); g.stroke();
      const pv = d.ctx[ci - 1].bars, gap = c.bars[0][0] - pv[pv.length - 1][3]; g.fillStyle = gap >= 0 ? CU : CD; g.font = '10px sans-serif'; g.textAlign = 'left'; g.fillText('গ্যাপ ' + (gap >= 0 ? '+' : '') + gap.toFixed(0), x + 3, 12); }
    g.fillStyle = '#7d889b'; g.font = '10px sans-serif'; g.textAlign = 'center'; g.fillText(c.date.slice(5), L + (k - c.bars.length / 2) * bw, H - 4);
  });
  [[d.pdh, 'PDH'], [d.pdl, 'PDL']].forEach(([v, nm]) => { if (v == null) return; g.strokeStyle = '#8b96a8'; g.setLineDash([4, 3]); g.beginPath(); g.moveTo(L, Y(v)); g.lineTo(W - R_, Y(v)); g.stroke(); g.setLineDash([]); g.fillStyle = '#8b96a8'; g.textAlign = 'left'; g.fillText(nm + ' ' + v.toFixed(0), W - R_ + 3, Y(v) + 3); });
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
  drawSweepViz(box, R);
  box.append(t, h('div', {class: 'note'}, '✓ = নেট লাভ, PF ≥ ১.২ এবং অন্তত অর্ধেক মাস লাভে। সবচেয়ে ভালো একটা সংখ্যার দিকে না তাকিয়ে এমন মান বাছুন যার আশেপাশের মানগুলোও ভালো, তাতে ভাগ্যের ওপর নির্ভরতা কমে।'));
}

/* ---------- প্রিসেট ---------- */

/* ---------- 📊 পোর্টফোলিও: একাধিক স্ট্র্যাটেজি একসাথে + কোরিলেশন ---------- */
let PORT = {sel: {}, w: {}, res: null, sellOn: false};
function renderPortfolio(root) {
  const vitems = ((META.vault || {}).items || []);
  const box = h('details', {class: 'card'}), b = h('div', {class: 'body'});
  box.append(h('summary', {style: 'padding:11px 12px;font-weight:600;display:block'}, '📊 পোর্টফোলিও ব্যাকটেস্ট (একাধিক স্ট্র্যাটেজি একসাথে)'));
  if (vitems.length < 1) b.append(h('div', {class: 'note'}, 'আগে কমপক্ষে ২টি স্ট্র্যাটেজি ভল্টে সেভ করুন।'));
  vitems.forEach(it => {
    const n = it.id;
    const cb = h('input', {type: 'checkbox'}); cb.checked = !!PORT.sel[n]; cb.addEventListener('change', () => PORT.sel[n] = cb.checked);
    const w = h('input', {type: 'number', value: PORT.w[n] || 1, min: 0.1, step: 0.5, style: 'width:62px'}); w.addEventListener('change', () => PORT.w[n] = parseFloat(w.value) || 1);
    b.append(h('div', {style: 'flex:1 1 100%;display:flex;gap:8px;align-items:center'}, cb, h('span', {style: 'flex:1'}, it.name), h('span', {class: 'note', style: 'margin:0'}, 'ওজন ×'), w));
  });
  if (SELL && SELL.legs) {
    const cb = h('input', {type: 'checkbox'}); cb.checked = PORT.sellOn; cb.addEventListener('change', () => PORT.sellOn = cb.checked);
    b.append(h('div', {style: 'flex:1 1 100%;display:flex;gap:8px;align-items:center'}, cb, h('span', {style: 'flex:1'}, '🧩 Option Selling: ' + (SELL.name || 'বর্তমান বাস্কেট'))));
  }
  const fa = h('input', {type: 'date'}), fb = h('input', {type: 'date'});
  b.append(F('শুরু (ঐচ্ছিক)', fa), F('শেষ (ঐচ্ছিক)', fb), h('button', {class: 'btn p', on: {click: async () => {
    const items = vitems.filter(it => PORT.sel[it.id]).map(it => ({id: it.id, name: it.name, type: 'buy', weight: PORT.w[it.id] || 1}));
    if (PORT.sellOn && SELL) items.push({name: 'Selling: ' + (SELL.name || 'বাস্কেট'), type: 'sell', weight: 1, settings: SELL});
    if (items.length < 2) { alert('কমপক্ষে ২টি বাছুন'); return; }
    const r = await runJob({kind: 'portfolio', settings: {items, from: fa.value, to: fb.value}}, 'পোর্টফোলিও'); if (r) { PORT.res = r; renderPresets(); }
  }}}, '▶ পোর্টফোলিও চালান'));
  box.append(b); root.append(box);
  const R = PORT.res; if (!R) return;
  const C = R.combined;
  root.append(h('h3', {}, 'যৌথ ফল'));
  root.append(h('div', {class: 'kpis'}, kpiBox('যৌথ নেট', inr(C.net), cls(C.net)), kpiBox('যৌথ Max DD', inr(C.dd), 'neg'), kpiBox('Sharpe', f2(C.sharpe)), kpiBox('Calmar', f2(C.calmar)),
    kpiBox('DD সাশ্রয় (একা-একা যোগফলের তুলনায়)', C.dd_benefit_pct.toFixed(0) + '%'), kpiBox('ট্রেড / PF', C.n + ' / ' + f2(C.pf))));
  root.append(tbl(['স্ট্র্যাটেজি', 'ওজন', 'ট্রেড', 'নেট ₹', 'Max DD', 'Sharpe', 'Calmar'], R.rows.map(x => [x.name, x.w, x.n, pn(x.net), inr(x.dd), f2(x.sharpe), f2(x.calmar)])));
  root.append(h('h3', {}, 'যৌথ ইকুইটি বনাম প্রতিটি স্ট্র্যাটেজি'));
  const cv = h('canvas', {style: 'width:100%;height:210px;background:#10151d;border-radius:10px'}); root.append(cv);
  setTimeout(() => {
    const W = cv.clientWidth || 340, H = 210, dpr = Math.min(window.devicePixelRatio || 1, 2); cv.width = W * dpr; cv.height = H * dpr; const g = cv.getContext('2d'); g.scale(dpr, dpr);
    const all = [R.equity].concat(R.series), mn = Math.min(0, ...all.map(a => Math.min(...a))), mx = Math.max(0, ...all.map(a => Math.max(...a))), n = R.equity.length, Lm = 6, Rm = 50, Tm = 8, Bm = 14;
    const X = i => Lm + (W - Lm - Rm) * i / Math.max(n - 1, 1), Y = v => Tm + (H - Tm - Bm) * (1 - (v - mn) / Math.max(mx - mn, 1));
    g.strokeStyle = '#1c2431'; g.fillStyle = '#7d889b'; g.font = '10px sans-serif';
    for (let k = 0; k <= 3; k++) { const v = mn + (mx - mn) * k / 3; g.beginPath(); g.moveTo(Lm, Y(v)); g.lineTo(W - Rm, Y(v)); g.stroke(); g.fillText(Math.round(v / 1000) + 'k', W - Rm + 4, Y(v) + 3); }
    const cols = ['#f1c40f', '#c678dd', '#56b6c2', '#e5894f', '#98c379', '#e06c9f'];
    R.series.forEach((a, k) => { g.strokeStyle = cols[k % 6]; g.globalAlpha = .75; g.lineWidth = 1.2; g.beginPath(); a.forEach((v, i) => i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))); g.stroke(); });
    g.globalAlpha = 1; g.strokeStyle = '#26c281'; g.lineWidth = 2.4; g.beginPath(); R.equity.forEach((v, i) => i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))); g.stroke();
  }, 0);
  root.append(h('div', {class: 'note', html: 'মোটা সবুজ = যৌথ; বাকিগুলো একেকটি স্ট্র্যাটেজি। ' + R.names.map((nm, k) => '<span style="color:' + ['#f1c40f', '#c678dd', '#56b6c2', '#e5894f', '#98c379', '#e06c9f'][k % 6] + '">■</span> ' + nm.replace(/</g, '&lt;')).join('  ')}));
  root.append(h('h3', {}, '🔗 কোরিলেশন ম্যাট্রিক্স (দৈনিক P&L)'));
  root.append(h('div', {class: 'scroll', html: '<table><tr><th></th>' + R.names.map((_, i) => '<th>S' + (i + 1) + '</th>').join('') + '</tr>' + R.corr.map((row, i) => '<tr><th style="text-align:left">S' + (i + 1) + ' ' + R.names[i].slice(0, 16).replace(/</g, '&lt;') + '</th>' + row.map((v, j) => '<td style="text-align:center;background:' + (i === j ? 'rgba(125,136,155,.25)' : v >= 0 ? 'rgba(255,93,93,' + (Math.abs(v) * .7).toFixed(2) + ')' : 'rgba(38,194,129,' + (Math.abs(v) * .7).toFixed(2) + ')') + '">' + v.toFixed(2) + '</td>').join('') + '</tr>').join('') + '</table>'}));
  root.append(h('div', {class: 'note'}, 'সবুজ/শূন্যের কাছাকাছি = কম কোরিলেশন (ভালো, একে অপরের ড্রডাউন ঢাকে); লাল = একসাথে হারে। ' + R.note));
}
function kpiBox(l, v, c) { return h('div', {class: 'kpi ' + (c === 'neg' ? 'bad' : c === 'pos' ? 'good' : '')}, h('small', {}, l), h('b', {class: c || ''}, v)); }
function renderPresets() {
  const root = $('#t-pre'); root.innerHTML = '';
  const V = META.vault || {folders: [], items: [], trash: 0, dir: ''};
  const act = V.items.find(x => x.id === VLT.active);
  if (VLT.active && !act) setActive(null);
  /* --- Smart Save --- */
  const nm = h('input', {type: 'text', placeholder: 'স্ট্র্যাটেজির নাম'}), nt = h('input', {type: 'text', placeholder: 'নোট (ঐচ্ছিক)'});
  const fsel = h('select', {}, ...V.folders.map(f => h('option', {value: f}, f)));
  if (act) fsel.value = act.folder;
  const sb = h('div', {class: 'body', style: 'padding-top:12px'});
  if (act) sb.append(h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, '📌 লোড করা আছে: ', h('b', {}, act.name)), h('button', {class: 'btn p', on: {click: updateActive}}, '🔄 আপডেট করুন'));
  sb.append(F(act ? 'নতুন কপি হিসেবে সেভ (নাম)' : 'বর্তমান সেটিং সেভ করুন', nm), F('নোট', nt), F('ফোল্ডার', fsel),
    h('button', {class: 'btn ' + (act ? 's' : 'p'), style: 'align-self:flex-end', on: {click: () => saveStrategy(nm.value, nt.value, fsel.value)}}, act ? '💾 নতুন কপি সেভ' : '💾 সেভ'));
  root.append(h('div', {class: 'card'}, sb));
  /* --- পোর্টফোলিও --- */
  renderPortfolio(root);
  /* --- ফোল্ডার চিপ বার --- */
  const chips = h('div', {style: 'display:flex;gap:6px;overflow-x:auto;padding:4px 2px 8px;-webkit-overflow-scrolling:touch'});
  const chip = (label, key, extra) => h('button', {class: 'btn s' + (VLT.folder === key ? ' p' : ''), style: 'white-space:nowrap;flex:0 0 auto;padding:5px 11px', on: {click: async () => { VLT.folder = key; if (key === '__trash') { const r = await api('/api/vault/trash_list', {}); VLT.trashItems = r.items || []; } renderPresets(); }}}, label);
  chips.append(chip('সব (' + V.items.length + ')', 'all'));
  V.folders.forEach(f => chips.append(chip(f + ' (' + V.items.filter(x => x.folder === f).length + ')', f)));
  chips.append(h('button', {class: 'btn s', style: 'white-space:nowrap;flex:0 0 auto;padding:5px 11px', on: {click: async () => { const n = prompt('নতুন ফোল্ডারের নাম'); if (!n) return; const r = await api('/api/vault/folder_create', {name: n}); if (r.error) { alert(r.error); return; } META.vault = r; renderPresets(); }}}, '＋ ফোল্ডার'));
  chips.append(chip('🗑 ট্র্যাশ (' + V.trash + ')', '__trash'));
  root.append(chips);
  if (VLT.folder !== 'all' && VLT.folder !== '__trash' && VLT.folder !== 'সাধারণ') {
    root.append(h('div', {style: 'display:flex;gap:6px;margin-bottom:6px'},
      h('button', {class: 'btn s', on: {click: async () => { const n = prompt('ফোল্ডারের নতুন নাম', VLT.folder); if (!n || n === VLT.folder) return; const r = await api('/api/vault/folder_rename', {old: VLT.folder, new: n}); if (r.error) { alert(r.error); return; } VLT.folder = n; META.vault = r; renderPresets(); }}}, '✎ নাম বদলান'),
      h('button', {class: 'btn d s', on: {click: async () => { if (!confirm('"' + VLT.folder + '" ফোল্ডার মুছবেন? ভেতরের স্ট্র্যাটেজি মুছবে না — "সাধারণ"-এ সরে যাবে।')) return; const r = await api('/api/vault/folder_delete', {name: VLT.folder}); if (r.error) { alert(r.error); return; } VLT.folder = 'all'; META.vault = r; renderPresets(); }}}, '✕ ফোল্ডার মুছুন')));
  }
  /* --- সার্চ + সর্ট --- */
  const q = h('input', {type: 'text', placeholder: '🔍 খুঁজুন…', value: VLT.q}); const so = h('select', {}, h('option', {value: 'upd'}, 'সাম্প্রতিক আপডেট'), h('option', {value: 'net'}, 'সর্বোচ্চ নেট'), h('option', {value: 'pf'}, 'সর্বোচ্চ PF')); so.value = VLT.sort;
  q.addEventListener('input', () => { VLT.q = q.value; drawList(); }); so.addEventListener('change', () => { VLT.sort = so.value; drawList(); });
  root.append(h('div', {style: 'display:flex;gap:8px;margin-bottom:8px'}, q, so));
  const box = h('div'); root.append(box);
  function drawList() {
    box.innerHTML = '';
    const trash = VLT.folder === '__trash';
    let items = trash ? VLT.trashItems : V.items.filter(x => VLT.folder === 'all' || x.folder === VLT.folder);
    const qq = VLT.q.trim().toLowerCase(); if (qq) items = items.filter(x => (x.name + ' ' + (x.note || '')).toLowerCase().includes(qq));
    const key = {upd: x => x.updated || '', net: x => (x.summary && x.summary.net) || -1e12, pf: x => (x.summary && x.summary.pf) || -1e12}[VLT.sort];
    items = items.slice().sort((a, b) => (b.pinned - a.pinned) || (key(a) < key(b) ? 1 : key(a) > key(b) ? -1 : 0));
    if (!items.length) box.append(h('div', {class: 'note'}, trash ? 'ট্র্যাশ ফাঁকা।' : 'এই ফোল্ডারে কিছু নেই।'));
    items.forEach(it => {
      const sm = it.summary;
      const card = h('div', {class: 'card', style: 'margin-bottom:8px;' + (it.id === VLT.active ? 'border-color:#26c281' : '')});
      const b = h('div', {class: 'body'});
      b.append(h('div', {style: 'flex:1 1 100%;display:flex;gap:8px;align-items:center'},
        trash ? h('span', {}, '🗑') : h('button', {class: 'btn s', style: 'padding:2px 8px;color:' + (it.pinned ? '#f5c542' : '#7d889b'), on: {click: async () => { const r = await api('/api/vault/pin', {id: it.id, on: !it.pinned}); META.vault = r; renderPresets(); }}}, it.pinned ? '★' : '☆'),
        h('b', {style: 'flex:1'}, it.name), h('span', {class: 'pill'}, it.folder)));
      b.append(h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, sm ? 'ট্রেড ' + sm.n + ' | নেট ' + inr(sm.net) + ' | PF ' + sm.pf + ' | Win ' + sm.win + '% | DD ' + inr(sm.dd) : 'ব্যাকটেস্ট সারাংশ নেই (সেভের আগে Run দিন)', it.note ? ' — ' + it.note : ''));
      const row = h('div', {style: 'flex:1 1 100%;display:flex;gap:6px;flex-wrap:wrap'});
      if (trash) {
        row.append(h('button', {class: 'btn s', on: {click: async () => { const r = await api('/api/vault/restore', {id: it.id}); META.vault = r; const t = await api('/api/vault/trash_list', {}); VLT.trashItems = t.items || []; renderPresets(); }}}, '↩ ফিরিয়ে আনুন'),
          h('button', {class: 'btn d s', on: {click: async () => { if (!confirm('"' + it.name + '" স্থায়ীভাবে মুছবেন? আর ফেরানো যাবে না।')) return; await api('/api/vault/purge', {id: it.id}); const r = await api('/api/vault/list', {}); META.vault = r; const t = await api('/api/vault/trash_list', {}); VLT.trashItems = t.items || []; renderPresets(); }}}, '✕ স্থায়ী মুছুন'));
      } else {
        row.append(h('button', {class: 'btn p s', on: {click: async () => { const r = await api('/api/vault/get', {id: it.id}); if (r.error) { alert(r.error); return; } S = fixSettings(r.settings); persist(); setActive(it.id); renderBuild(); showTab('build'); $('#status').textContent = '✓ লোড: ' + it.name; }}}, 'লোড'),
          h('button', {class: 'btn s', on: {click: async () => { const f = prompt('কোন ফোল্ডারে সরাবেন?\n' + V.folders.join(' | '), it.folder); if (!f || f === it.folder) return; const r = await api('/api/vault/move', {id: it.id, folder: f}); if (r.error) { alert(r.error); return; } META.vault = r; renderPresets(); }}}, '📁 সরান'),
          h('button', {class: 'btn s', on: {click: async () => { const r = await api('/api/vault/duplicate', {id: it.id}); META.vault = r; renderPresets(); }}}, '⎘ কপি'),
          h('button', {class: 'btn d s', on: {click: async () => { if (!confirm('"' + it.name + '" ট্র্যাশে পাঠাবেন? (পরে ফিরিয়ে আনা যাবে)')) return; const r = await api('/api/vault/delete', {id: it.id}); if (it.id === VLT.active) setActive(null); META.vault = r; renderPresets(); }}}, '🗑 মুছুন'));
      }
      b.append(row); card.append(b); box.append(card);
    });
  }
  drawList();
  root.append(h('div', {class: 'note'}, '📂 ভল্ট ফোল্ডার (ব্যাকআপ নিতে এটা কপি করুন): ', h('code', {}, V.dir || META.vault_dir || ''), ' — কোড আপডেট/git pull-এ এটা মোছে না।'));
  /* --- নমুনা টেমপ্লেট --- */
  const tb = h('div', {class: 'card'}, h('summary', {style: 'padding:11px 12px;font-weight:600;display:block'}, 'নমুনা টেমপ্লেট (শুধু উদাহরণ, নিজের মতো বদলান)')), tbd = h('div', {class: 'body'});
  Object.keys(META.presets.builtin).forEach(k => tbd.append(h('div', {style: 'flex:1 1 100%;display:flex;gap:6px;align-items:center'}, h('span', {style: 'flex:1'}, k), h('button', {class: 'btn s', on: {click: () => { S = fixSettings(META.presets.builtin[k]); persist(); setActive(null); renderBuild(); showTab('build'); }}}, 'লোড'))));
  tb.append(tbd); root.append(tb);
  const ta = h('textarea', {rows: 6, placeholder: 'এখানে JSON পেস্ট করে Import করুন', style: 'font-family:monospace;font-size:11px'});
  root.append(h('div', {class: 'card'}, h('div', {class: 'body', style: 'padding-top:12px'}, ta,
    h('button', {class: 'btn s', on: {click: () => { ta.value = JSON.stringify(S, null, 1); }}}, 'Export (বর্তমান সেটিং)'),
    h('button', {class: 'btn s', on: {click: () => { try { S = fixSettings(JSON.parse(ta.value)); persist(); renderBuild(); showTab('build'); } catch (e) { alert('JSON ঠিক নেই: ' + e.message); } }}}, 'Import'),
    h('button', {class: 'btn d s', on: {click: () => { if (confirm('সব সেটিং ডিফল্টে ফেরাবেন?')) { S = fixSettings(META.defaults); persist(); setActive(null); renderBuild(); showTab('build'); } }}}, 'ডিফল্টে ফেরান'))));
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




/* ---------- Optimize: 2D Heatmap ও 1D স্টেবিলিটি কার্ভ ---------- */
let HMM = 'net';
const HM_METRICS = {net: ['নেট ₹', r => r.all.net], pf: ['Profit Factor', r => r.all.pf], rec: ['Recovery', r => r.all.rec], dd: ['Max DD (কম লস = ভালো)', r => r.all.dd], win: ['Win %', r => r.all.win], test: ['TEST নেট ₹', r => r.test.net]};
function hmColor(t) { t = Math.max(0, Math.min(1, t)); const a = t < .5 ? [214, 69, 65] : [241, 196, 15], b = t < .5 ? [241, 196, 15] : [38, 194, 129], k = t < .5 ? t * 2 : (t - .5) * 2; return 'rgb(' + a.map((x, i) => Math.round(x + (b[i] - x) * k)).join(',') + ')'; }
function drawSweepViz(box, R) {
  if (R.paths.length > 2) return;
  const wrap = h('div'); box.append(h('h3', {}, R.paths.length === 2 ? '🟩 2D Parameter Heatmap' : '📈 স্টেবিলিটি কার্ভ (প্ল্যাটো খুঁজুন)'), wrap);
  const sel = h('select', {on: {change: e => { HMM = e.target.value; draw(); }}}, ...Object.entries(HM_METRICS).map(([k, v]) => h('option', {value: k}, v[0]))); sel.value = HMM;
  wrap.append(F('মেট্রিক', sel)); const area = h('div'); wrap.append(area);
  function draw() {
    area.innerHTML = ''; const f = HM_METRICS[HMM][1], rows = R.rows.filter(r => r.all.n >= 1);
    if (!rows.length) return;
    if (R.paths.length === 2) {
      const xs = [...new Set(rows.map(r => r.vals[0]))].sort((a, b) => a - b), ys = [...new Set(rows.map(r => r.vals[1]))].sort((a, b) => a - b), m = {};
      rows.forEach(r => m[r.vals[0] + '|' + r.vals[1]] = r);
      const vs = rows.map(f), mn = Math.min(...vs), mx = Math.max(...vs);
      let html = '<table style="border-collapse:separate;border-spacing:2px"><tr><th>' + R.labels[1].replace(/\[.*?\]/, '') + ' ↓ / ' + R.labels[0].replace(/\[.*?\]/, '') + ' →</th>' + xs.map(x => '<th>' + x + '</th>').join('') + '</tr>';
      ys.forEach(y => { html += '<tr><th>' + y + '</th>' + xs.map(x => { const r = m[x + '|' + y]; if (!r) return '<td></td>'; const v = f(r); return '<td data-k="' + x + '|' + y + '" style="background:' + hmColor((v - mn) / Math.max(mx - mn, 1e-9)) + ';color:#06140c;text-align:center;font-weight:600;cursor:pointer;padding:6px 3px;border-radius:5px;font-size:11px">' + (Math.abs(v) >= 1000 ? Math.round(v / 1000) + 'k' : (Math.abs(v) < 10 ? v.toFixed(2) : Math.round(v))) + '</td>'; }).join('') + '</tr>'; });
      const t = h('div', {class: 'scroll', html: html + '</table>'}); const det = h('div');
      t.addEventListener('click', e => { const td = e.target.closest('td[data-k]'); if (!td) return; const r = m[td.dataset.k]; det.innerHTML = '';
        det.append(tbl(['', 'সব', 'Train', 'Test'], [['ট্রেড', r.all.n, r.train.n, r.test.n], ['নেট', pn(r.all.net), pn(r.train.net), pn(r.test.net)], ['PF', f2(r.all.pf), f2(r.train.pf), f2(r.test.pf)], ['Win%', r.all.win.toFixed(0), r.train.win.toFixed(0), r.test.win.toFixed(0)], ['DD', inr(r.all.dd), inr(r.train.dd), inr(r.test.dd)]]));
        det.append(h('button', {class: 'btn p', on: {click: () => { R.paths.forEach((p, i) => setp(p, r.vals[i])); renderBuild(); showTab('build'); $('#status').textContent = 'সেটিং লাগানো হয়েছে, ▶ Run চাপুন'; }}}, '✔ এই সেটিং প্রয়োগ (' + r.vals.join(', ') + ')')); });
      area.append(t, det, h('div', {class: 'note'}, 'লাল = খারাপ, সবুজ = ভালো। একক উজ্জ্বল সবুজ ঘর (চারপাশে লাল) = স্পাইক, ওভারফিটের ঝুঁকি; পাশাপাশি অনেক সবুজ ঘরের চওড়া এলাকা = টেকসই প্ল্যাটো — তার মাঝখান থেকে বাছুন।'));
    } else {
      const cv = h('canvas', {style: 'width:100%;height:200px;background:#10151d;border-radius:10px'}); area.append(cv, h('div', {class: 'note'}, 'সবুজ = মেট্রিক; হলুদ ড্যাশ = ৩-বিন্দু গড় (স্মুথ); নীল রেখা = এখনকার মান। মসৃণ উঁচু এলাকা চাই, একক চূড়া নয়।'));
      const rs = rows.slice().sort((a, b) => a.vals[0] - b.vals[0]), v = rs.map(f), sm = v.map((_, i) => { const a = v.slice(Math.max(0, i - 1), i + 2); return a.reduce((x, y) => x + y, 0) / a.length; });
      setTimeout(() => {
        const W = cv.clientWidth || 340, H = 200, dpr = Math.min(window.devicePixelRatio || 1, 2); cv.width = W * dpr; cv.height = H * dpr; const g = cv.getContext('2d'); g.scale(dpr, dpr);
        const mn = Math.min(...v, ...sm), mx = Math.max(...v, ...sm), x0 = rs[0].vals[0], x1 = rs[rs.length - 1].vals[0], L = 34, R_ = 8, T = 10, B = 20;
        const X = a => L + (W - L - R_) * (rs.length === 1 ? .5 : (a - x0) / Math.max(x1 - x0, 1e-9)), Y = a => T + (H - T - B) * (1 - (a - mn) / Math.max(mx - mn, 1e-9));
        g.fillStyle = '#7d889b'; g.font = '10px sans-serif'; g.textAlign = 'right'; g.fillText(Math.abs(mx) < 10 ? mx.toFixed(2) : Math.round(mx), L - 3, T + 6); g.fillText(Math.abs(mn) < 10 ? mn.toFixed(2) : Math.round(mn), L - 3, H - B);
        g.textAlign = 'center'; rs.forEach((r, i) => { if (rs.length < 14 || i % 2 === 0) g.fillText(r.vals[0], X(r.vals[0]), H - 6); });
        const cur = getp(R.paths[0]); if (isFinite(cur)) { g.strokeStyle = '#4f9cff'; g.beginPath(); g.moveTo(X(cur), T); g.lineTo(X(cur), H - B); g.stroke(); }
        const ln = (arr, col, dash) => { g.strokeStyle = col; g.setLineDash(dash); g.lineWidth = 2; g.beginPath(); arr.forEach((a, i) => i ? g.lineTo(X(rs[i].vals[0]), Y(a)) : g.moveTo(X(rs[i].vals[0]), Y(a))); g.stroke(); g.setLineDash([]); };
        ln(sm, '#f1c40f', [5, 3]); ln(v, '#26c281', []); g.fillStyle = '#26c281'; rs.forEach((r, i) => { g.beginPath(); g.arc(X(r.vals[0]), Y(v[i]), 3, 0, 7); g.fill(); });
      }, 0);
    }
  }
  draw();
}

/* ---------- 🧩 Option Selling Lab (Sensibull-ধাঁচ) ---------- */
let SELL = null, SELLR = null, SELLP = null, SELLW = {spot: 0, iv: 0, day: 0};
function sellLoad() {
  if (SELL) return;
  try { SELL = JSON.parse(localStorage.getItem('gb_sell') || 'null'); } catch (e) {}
  if (!SELL || !SELL.legs) SELL = JSON.parse(JSON.stringify(META.sell.defaults));
  for (const k of ['entry', 'exit', 'rules', 'size', 'margin', 'general']) SELL[k] = Object.assign({}, META.sell.defaults[k], SELL[k] || {});
}
function sellSave() { try { localStorage.setItem('gb_sell', JSON.stringify(SELL)); } catch (e) {} }
function sNum(o, k, step, w) {
  const i = h('input', {type: 'number', inputmode: 'decimal', step: step || 'any', value: o[k], style: w ? 'width:' + w : ''});
  i.addEventListener('change', () => { const v = parseFloat(i.value); o[k] = isFinite(v) ? v : 0; sellSave(); }); return i;
}
function sTxt(o, k) { const i = h('input', {type: 'text', value: o[k]}); i.addEventListener('change', () => { o[k] = i.value; sellSave(); }); return i; }
function sChk(o, k, lb) { const i = h('input', {type: 'checkbox'}); i.checked = !!o[k]; i.addEventListener('change', () => { o[k] = i.checked; sellSave(); }); return h('label', {class: 'f', style: 'flex-direction:row;gap:6px;align-items:center'}, i, h('span', {}, lb)); }
function sSel(o, k, opts, cb) { const s = h('select', {}, ...opts.map(([v, t]) => h('option', {value: v}, t))); s.value = o[k]; s.addEventListener('change', () => { o[k] = (typeof opts[0][0] === 'number') ? parseInt(s.value) : s.value; sellSave(); if (cb) cb(); }); return s; }

function renderSell() {
  sellLoad();
  const root = $('#t-sell'); root.innerHTML = '';
  const T = META.sell.templates;
  root.append(h('div', {class: 'h2'}, '🧩 Option Selling Lab'));
  root.append(h('div', {class: 'note'}, 'মাল্টি-লেগ বাস্কেট (Straddle, Strangle, Iron Condor…) ব্যাকটেস্ট, Black-Scholes Greeks/IV ও Payoff। এখনকার সংস্করণ ইন্ট্রাডে (দিনের শেষে স্কোয়ার-অফ)। এটা Strategy ট্যাবের বায়িং-ইঞ্জিন থেকে আলাদা, তবে একই ডেটা ও খরচ-মডেল ব্যবহার করে।'));
  const ts = h('select', {}, ...Object.entries(T).map(([k, v]) => h('option', {value: k}, v.name + ' — ' + v.view))); ts.value = SELL.template;
  const tdesc = h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, (T[SELL.template] || {}).desc || '');
  ts.addEventListener('change', () => { SELL.template = ts.value; SELL.legs = JSON.parse(JSON.stringify(T[ts.value].legs)); SELL.name = T[ts.value].name + ' ' + SELL.entry.time; sellSave(); SELLP = null; renderSell(); });
  root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, F('টেমপ্লেট (এক-ক্লিকে)', ts), F('নাম', sTxt(SELL, 'name')), tdesc)));
  /* লেগ */
  root.append(h('h3', {}, 'লেগ (প্রতিটি আলাদা SL)'));
  const lw = h('div'); root.append(lw);
  const drawLegs = () => {
    lw.innerHTML = '';
    SELL.legs.forEach((lg, i) => {
      lw.append(h('div', {class: 'card'}, h('div', {class: 'body', style: 'gap:6px'},
        sSel(lg, 'act', [['SELL', 'SELL'], ['BUY', 'BUY']]), sSel(lg, 'opt', [['CE', 'CE'], ['PE', 'PE']]),
        sSel(lg.strike, 'mode', [['atm', 'ATM ± স্ট্রাইক-সংখ্যা'], ['pts', 'ATM ± পয়েন্ট'], ['prem', 'প্রিমিয়াম ≈ ₹'], ['delta', 'ডেল্টা ≈ (০.২০)']]),
        F('মান', sNum(lg.strike, 'val', 'any', '84px')), F('লট', sNum(lg, 'lots', 1, '60px')), F('লেগ SL %', sNum(lg, 'sl', 1, '64px')), F('লেগ TP %', sNum(lg, 'tp', 1, '64px')),
        sSel(lg, 'role', [['core', 'মূল'], ['hedge', 'হেজ']]), sSel(lg, 'exp', [[0, 'কাছের এক্সপায়ারি'], [1, 'পরের এক্সপায়ারি']]),
        h('button', {class: 'btn s', on: {click: () => { SELL.legs.splice(i, 1); sellSave(); drawLegs(); }}}, '✕'))));
    });
  };
  drawLegs();
  root.append(h('button', {class: 'btn s', on: {click: () => { SELL.legs.push({act: 'SELL', opt: 'CE', strike: {mode: 'atm', val: 0}, lots: 1, sl: 30, tp: 0, role: 'core', exp: 0}); sellSave(); drawLegs(); }}}, '＋ লেগ যোগ'));
  /* এন্ট্রি / এক্সিট */
  const E = SELL.entry, X = SELL.exit, R = SELL.rules, Z = SELL.size, M = SELL.margin, G = SELL.general;
  root.append(h('h3', {}, 'এন্ট্রি'));
  const dw = ['সোম', 'মঙ্গল', 'বুধ', 'বৃহ', 'শুক্র'].map((n, i) => { const c = h('input', {type: 'checkbox'}); c.checked = E.dows.includes(i); c.addEventListener('change', () => { E.dows = E.dows.filter(x => x !== i); if (c.checked) E.dows.push(i); sellSave(); }); return h('label', {class: 'f', style: 'flex-direction:row;gap:4px;align-items:center'}, c, h('span', {}, n)); });
  root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, F('এন্ট্রি সময় (HH:MM)', sTxt(E, 'time')), F('ন্যূনতম DTE', sNum(E, 'min_dte', 1)), F('সর্বোচ্চ DTE', sNum(E, 'max_dte', 1)),
    F('IVP ন্যূনতম % (০=বন্ধ)', sNum(E, 'ivp_min', 1)), F('IVP সর্বোচ্চ % (১০০=বন্ধ)', sNum(E, 'ivp_max', 1)), F('গ্যাপ > % হলে বাদ (০=বন্ধ)', sNum(E, 'gap_max', 'any')),
    h('div', {style: 'display:flex;gap:8px;flex-wrap:wrap;flex:1 1 100%'}, ...dw),
    h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, 'IVP = এন্ট্রি-সময়ের ATM IV গত ≤৬০ দিনের তুলনায় কত শতাংশ ওপরে (Black-Scholes থেকে নিজে বের করা)। সেলিংয়ে IVP>৬০ ফিল্টার জনপ্রিয়।'))));
  root.append(h('h3', {}, 'বাস্কেট এক্সিট (১ লটের জন্য ₹; লট বাড়লে গুণ হবে)'));
  root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, F('স্কোয়ার-অফ সময়', sTxt(X, 'time')), F('Basket SL ₹ (০=নেই)', sNum(X, 'basket_sl', 100)), F('Basket TP ₹', sNum(X, 'basket_tp', 100)),
    F('Lock: লাভ ছুঁলে ₹', sNum(X, 'lock_at', 100)), F('Lock: এই লাভ রক্ষা ₹', sNum(X, 'lock_to', 100)), F('Trail: প্রতি +₹', sNum(X, 'trail_step', 100)), F('Trail: লক বাড়বে ₹', sNum(X, 'trail_lock', 100)))));
  root.append(h('h3', {}, 'অ্যাডজাস্টমেন্ট ও ঝুঁকি-নিয়ম'));
  root.append(h('div', {class: 'card'}, h('div', {class: 'body'},
    sChk(R, 'sl_to_cost', 'Move SL to Cost: একদিকে SL লাগলে অন্য সেল-লেগের SL এন্ট্রি-দামে'),
    F('রি-এন্ট্রি সর্বোচ্চ বার', sNum(R, 'reentry_max', 1)), F('রি-এন্ট্রি মোড', sSel(R, 'reentry_mode', [['asap', 'ASAP (পরের বারেই)'], ['recost', 'Re-Cost (দাম এন্ট্রিতে ফিরলে)']])),
    sChk(R, 'adjust', 'Auto-Adjust: লাভজনক লেগ রোল-ইন'), F('প্রিমিয়াম অনুপাত ≥', sNum(R, 'adj_ratio', 0.1)), F('|Net Δ| > (০=বন্ধ)', sNum(R, 'adj_delta', 0.05)), F('সর্বোচ্চ অ্যাডজাস্ট', sNum(R, 'adj_max', 1)),
    sChk(R, 'auto_hedge', 'Auto-Hedge: হেজ না থাকলে সস্তা OTM বাই নিজে যোগ'), F('হেজ প্রিমিয়াম ≈ ₹', sNum(R, 'hedge_prem', 1)))));
  root.append(h('h3', {}, 'সাইজিং, মার্জিন ও সাধারণ'));
  root.append(h('div', {class: 'card'}, h('div', {class: 'body'},
    F('সাইজিং', sSel(Z, 'mode', [['fixed', 'ফিক্সড লট'], ['risk', 'মূলধনের % ঝুঁকি (Basket SL ধরে)']])), F('লট', sNum(Z, 'lots', 1)), F('ঝুঁকি % মূলধনের', sNum(Z, 'risk_pct', 0.1)),
    F('নেকেড মার্জিন % (আনুমানিক)', sNum(M, 'pct', 0.5)), F('মূলধন ₹', sNum(G, 'capital', 10000)), F('স্লিপেজ (পয়েন্ট)', sNum(G, 'slip', 0.1)), F('ব্রোকারেজ/অর্ডার ₹', sNum(G, 'brokerage', 1)), F('চার্জ % (সেল-টার্নওভারে)', sNum(G, 'charges_pct', 0.01)),
    F('তারিখ থেকে (YYYY-MM-DD)', sTxt(G, 'from')), F('তারিখ পর্যন্ত', sTxt(G, 'to')), F('Train %', sNum(G, 'split', 5)),
    h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, 'মার্জিন একটি আনুমানিক মডেল (SPAN ঠিক নয়): নেকেড সেলিং = ' + M.pct + '% × নোশনাল, হেজড হলে স্প্রেডের সর্বোচ্চ লসে সীমাবদ্ধ। আসল মার্জিনের জন্য ব্রোকারের ক্যালকুলেটর দেখুন; ROM তারই ভিত্তিতে।'))));
  /* অ্যাকশন */
  const pd = h('input', {type: 'text', placeholder: 'YYYY-MM-DD (খালি = শেষ দিন)', style: 'width:170px'});
  root.append(h('div', {class: 'card'}, h('div', {class: 'body'},
    h('button', {class: 'btn g', on: {click: async () => { const r = await runJob({kind: 'sell', settings: SELL}, 'সেলিং ব্যাকটেস্ট'); if (r) { SELLR = r; drawSellRes(); } }}}, '▶ সেলিং ব্যাকটেস্ট'),
    h('button', {class: 'btn p', on: {click: async () => { const r = await runJob({kind: 'sellprev', settings: SELL, opts: {date: pd.value.trim()}}, 'Payoff'); if (r) { SELLP = r; SELLW = {spot: 0, iv: 0, day: 0}; drawPayoff(); } }}}, '📉 Payoff / Greeks দেখুন'), pd)));
  root.append(h('div', {id: 'sell_pay'}), h('div', {id: 'sell_res'}));
  if (SELLP) drawPayoff();
  if (SELLR) drawSellRes();
}

/* ---- JS Black-Scholes ---- */
function jN(x) { const t = 1 / (1 + 0.2316419 * Math.abs(x)), p = t * (0.31938153 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429)))), c = 1 - Math.exp(-x * x / 2) / Math.sqrt(2 * Math.PI) * p; return x >= 0 ? c : 1 - c; }
function jP(S, K, T, s, typ, r) {
  if (T <= 1e-6) return Math.max(typ === 'CE' ? S - K : K - S, 0);
  const q = s * Math.sqrt(T), d1 = (Math.log(S / K) + (r + s * s / 2) * T) / q, d2 = d1 - q;
  return typ === 'CE' ? S * jN(d1) - K * Math.exp(-r * T) * jN(d2) : K * Math.exp(-r * T) * jN(-d2) - S * jN(-d1);
}
function jG(S, K, T, s, typ, r) {
  T = Math.max(T, 1e-6); const q = s * Math.sqrt(T), d1 = (Math.log(S / K) + (r + s * s / 2) * T) / q, d2 = d1 - q, pdf = Math.exp(-d1 * d1 / 2) / Math.sqrt(2 * Math.PI);
  const delta = typ === 'CE' ? jN(d1) : jN(d1) - 1, vega = S * pdf * Math.sqrt(T) / 100;
  let th = -S * pdf * s / (2 * Math.sqrt(T)); th += typ === 'CE' ? -r * K * Math.exp(-r * T) * jN(d2) : r * K * Math.exp(-r * T) * jN(-d2);
  return {delta, vega, theta: th / 365};
}
function payoffCalc(P, w) {
  const sg = l => l.act === 'BUY' ? 1 : -1, S0 = P.spot, Tt = Math.max(P.T_days - w.day, 0) / 365, iv0 = P.atm_iv / 100;
  const lo = S0 * 0.82, hi = S0 * 1.18, N = 260, xs = [], ex = [], t0 = [];
  for (let i = 0; i <= N; i++) {
    const S = lo + (hi - lo) * i / N; xs.push(S); let a = 0, b = 0;
    P.legs.forEach(l => {
      const iv = Math.max(((l.iv || P.atm_iv) / 100) * (1 + w.iv / 100), 0.02), q = l.qty * sg(l), intr = Math.max(l.opt === 'CE' ? S - l.strike : l.strike - S, 0);
      a += q * (intr - l.premium);
      const Tl = Math.max((l.T_days - w.day), 0) / 365; b += q * (jP(S, l.strike, Tl, iv, l.opt, P.rf) - l.premium);
    });
    ex.push(a); t0.push(b);
  }
  const mx = Math.max(...ex), mn = Math.min(...ex);
  const slopeL = ex[1] - ex[0], slopeR = ex[N] - ex[N - 1];
  const be = []; for (let i = 1; i <= N; i++) if ((ex[i - 1] < 0) !== (ex[i] < 0)) be.push(xs[i - 1] + (xs[i] - xs[i - 1]) * (0 - ex[i - 1]) / (ex[i] - ex[i - 1]));
  // POP: লগ-নরমাল (শূন্য ড্রিফট) ও বর্তমান IV
  const sd = iv0 * Math.sqrt(P.T_days / 365); let pop = 0, tot = 0;
  for (let i = 0; i <= N; i++) { const z = (Math.log(xs[i] / S0) + sd * sd / 2) / sd, wt = Math.exp(-z * z / 2) / xs[i]; tot += wt; if (ex[i] > 0) pop += wt; }
  const Sx = S0 * (1 + w.spot / 100); let nd = 0, nt = 0, nv = 0, ng = 0;
  P.legs.forEach(l => { const iv = Math.max(((l.iv || P.atm_iv) / 100) * (1 + w.iv / 100), 0.02), g = jG(Sx, l.strike, Math.max(l.T_days - w.day, 0) / 365, iv, l.opt, P.rf), q = l.qty * sg(l); nd += g.delta * q; nt += g.theta * q; nv += g.vega * q; });
  let now = 0; P.legs.forEach(l => { const iv = Math.max(((l.iv || P.atm_iv) / 100) * (1 + w.iv / 100), 0.02), q = l.qty * sg(l); now += q * (jP(Sx, l.strike, Math.max(l.T_days - w.day, 0) / 365, iv, l.opt, P.rf) - l.premium); });
  return {xs, ex, t0, mx, mn, unlimLoss: slopeL < -1e-6 && ex[0] < 0 || slopeR < -1e-6, unlimProfit: slopeL < -1e-6 && ex[0] > 0 || slopeR > 1e-6, be, pop: 100 * pop / tot, sd, nd, nt, nv, now, Sx};
}
function drawPayoff() {
  const box = $('#sell_pay'); if (!box) return; box.innerHTML = ''; const P = SELLP; if (!P) return;
  box.append(h('h3', {}, '📉 Payoff — ' + P.date + ' এন্ট্রি, এক্সপায়ারি ' + P.expiry + ' (' + P.T_days + ' দিন বাকি)'));
  box.append(tbl(['লেগ', 'স্ট্রাইক', 'লট/Qty', 'প্রিমিয়াম', 'IV%', 'Δ', 'Θ/দিন', 'ν'], P.legs.map(l => [l.act + ' ' + l.opt + (l.role === 'hedge' ? ' (হেজ)' : ''), l.strike, l.lots + ' / ' + l.qty, '₹' + l.premium, l.iv == null ? '–' : l.iv, l.delta, l.theta, l.vega])));
  const cv = h('canvas', {style: 'width:100%;height:260px;background:#10151d;border-radius:10px;touch-action:none'}); box.append(cv);
  const out = h('div'); const mk = (lb, key, mn, mx, st) => { const i = h('input', {type: 'range', min: mn, max: mx, step: st, value: SELLW[key], style: 'width:100%'}); const sp = h('span', {}, SELLW[key]); i.addEventListener('input', () => { SELLW[key] = parseFloat(i.value); sp.textContent = SELLW[key]; redraw(); }); return h('label', {class: 'f', style: 'flex:1 1 100%'}, h('span', {}, lb + ': ', sp), i); };
  box.append(h('div', {class: 'card'}, h('div', {class: 'body'}, mk('স্পট পরিবর্তন %', 'spot', -5, 5, 0.1), mk('IV পরিবর্তন % (আপেক্ষিক)', 'iv', -20, 20, 1), mk('দিন এগোনো (DTE ক্ষয়)', 'day', 0, Math.max(0, Math.floor(P.T_days)), 0.25))));
  box.append(out);
  function redraw() {
    const C = payoffCalc(P, SELLW), W = cv.clientWidth || 340, H = 260, dpr = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = W * dpr; cv.height = H * dpr; const g = cv.getContext('2d'); g.scale(dpr, dpr);
    const L = 8, R = 8, Tp = 10, B = 22, all = C.ex.concat(C.t0), mn = Math.min(...all, 0), mx = Math.max(...all, 0), x0 = C.xs[0], x1 = C.xs[C.xs.length - 1];
    const X = v => L + (W - L - R) * (v - x0) / (x1 - x0), Y = v => Tp + (H - Tp - B) * (1 - (v - mn) / Math.max(mx - mn, 1));
    g.fillStyle = 'rgba(86,182,194,.10)'; g.fillRect(X(P.spot - 2 * C.sd * P.spot), Tp, X(P.spot + 2 * C.sd * P.spot) - X(P.spot - 2 * C.sd * P.spot), H - Tp - B);
    g.fillStyle = 'rgba(86,182,194,.16)'; g.fillRect(X(P.spot - C.sd * P.spot), Tp, X(P.spot + C.sd * P.spot) - X(P.spot - C.sd * P.spot), H - Tp - B);
    g.strokeStyle = '#2f4a3b'; g.beginPath(); g.moveTo(L, Y(0)); g.lineTo(W - R, Y(0)); g.stroke();
    g.fillStyle = '#7d889b'; g.font = '10px sans-serif'; g.textAlign = 'center';
    for (let k = 0; k <= 4; k++) { const v = x0 + (x1 - x0) * k / 4; g.fillText(Math.round(v), X(v), H - 8); }
    g.textAlign = 'left'; g.fillText(Math.round(mx).toLocaleString('en-IN'), L + 2, Tp + 8); g.fillText(Math.round(mn).toLocaleString('en-IN'), L + 2, H - B - 3);
    const line = (arr, col, dash, w) => { g.strokeStyle = col; g.lineWidth = w; g.setLineDash(dash); g.beginPath(); arr.forEach((v, i) => i ? g.lineTo(X(C.xs[i]), Y(v)) : g.moveTo(X(C.xs[i]), Y(v))); g.stroke(); g.setLineDash([]); };
    line(C.ex, '#26c281', [], 2); line(C.t0, '#f1c40f', [5, 3], 2);
    g.strokeStyle = '#8b96a8'; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(X(P.spot), Tp); g.lineTo(X(P.spot), H - B); g.stroke(); g.setLineDash([]);
    g.strokeStyle = '#ff9f43'; g.beginPath(); g.moveTo(X(C.Sx), Tp); g.lineTo(X(C.Sx), H - B); g.stroke();
    out.innerHTML = '';
    const fm = v => inr(v), lim = (v, un) => un ? 'অসীম (হেজ নেই)' : fm(v);
    out.append(tbl(['পে-অফ সারাংশ', ''], [
      ['সর্বোচ্চ লাভ (এক্সপায়ারি)', lim(C.mx, C.unlimProfit)], ['সর্বোচ্চ লস (এক্সপায়ারি)', lim(C.mn, C.unlimLoss)],
      ['ব্রেকইভেন', C.be.length ? C.be.map(v => Math.round(v)).join(' / ') : '–'], ['লাভের সম্ভাবনা (POP, IV-ভিত্তিক)', C.pop.toFixed(0) + '%'],
      ['±1SD / ±2SD রেঞ্জ', Math.round(P.spot * (1 - C.sd)) + '–' + Math.round(P.spot * (1 + C.sd)) + ' / ' + Math.round(P.spot * (1 - 2 * C.sd)) + '–' + Math.round(P.spot * (1 + 2 * C.sd))],
      ['এখনকার (T+0) P&L এই স্লাইডারে', pn(C.now)], ['Net Δ (qty)', C.nd.toFixed(1)], ['Net Θ প্রতিদিন', pn(C.nt)], ['Net ν প্রতি ১ vol-pt', pn(C.nv)], ['আনুমানিক মার্জিন', inr(P.margin)]]));
    out.append(h('div', {class: 'note'}, 'সবুজ কঠিন = এক্সপায়ারি পে-অফ; হলুদ ড্যাশ = T+0 (Black-Scholes); নীলচে ব্যান্ড = ±1SD/±2SD; ধূসর রেখা = এন্ট্রির স্পট, কমলা = স্লাইডারের স্পট। POP/IV কেবল মডেল-অনুমান, ভবিষ্যদ্বাণী নয়।'));
  }
  setTimeout(redraw, 0);
  /* Greeks কার্ভ (স্পট বনাম Net Δ / Net Θ) ও IV স্কিউ */
  const gb = h('div'); box.append(gb);
  const gcv = h('canvas', {style: 'width:100%;height:200px;background:#10151d;border-radius:10px'});
  gb.append(h('h3', {}, '📐 Greeks কার্ভ — স্পট বনাম Net Δ (সবুজ) ও Net Θ/দিন (হলুদ)'), gcv);
  setTimeout(() => {
    const W = gcv.clientWidth || 340, H = 200, dpr = Math.min(window.devicePixelRatio || 1, 2); gcv.width = W * dpr; gcv.height = H * dpr; const g = gcv.getContext('2d'); g.scale(dpr, dpr);
    const sg = l => l.act === 'BUY' ? 1 : -1, lo = P.spot * 0.9, hi = P.spot * 1.1, N = 120, xs = [], dl = [], th = [];
    for (let i = 0; i <= N; i++) { const S = lo + (hi - lo) * i / N; let a = 0, b = 0; P.legs.forEach(l => { const iv = Math.max((l.iv || P.atm_iv) / 100, 0.02), gg = jG(S, l.strike, Math.max(l.T_days, 0.01) / 365, iv, l.opt, P.rf), q = l.qty * sg(l); a += gg.delta * q; b += gg.theta * q; }); xs.push(S); dl.push(a); th.push(b); }
    const L = 6, Rr = 6, Tp = 8, B = 20, X = v => L + (W - L - Rr) * (v - lo) / (hi - lo);
    const sc = (arr, y0, y1) => { const mn = Math.min(...arr, 0), mx = Math.max(...arr, 0), rg = Math.max(mx - mn, 1e-9); return v => y0 + (y1 - y0) * (1 - (v - mn) / rg); };
    const Yd = sc(dl, Tp, H - B), Yt = sc(th, Tp, H - B);
    g.strokeStyle = '#2f4a3b'; g.beginPath(); g.moveTo(L, Yd(0)); g.lineTo(W - Rr, Yd(0)); g.stroke();
    const ln = (arr, Y, col) => { g.strokeStyle = col; g.lineWidth = 2; g.beginPath(); arr.forEach((v, i) => i ? g.lineTo(X(xs[i]), Y(v)) : g.moveTo(X(xs[i]), Y(v))); g.stroke(); };
    ln(dl, Yd, '#26c281'); ln(th, Yt, '#f1c40f');
    g.strokeStyle = '#8b96a8'; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(X(P.spot), Tp); g.lineTo(X(P.spot), H - B); g.stroke(); g.setLineDash([]);
    g.fillStyle = '#7d889b'; g.font = '10px sans-serif'; g.textAlign = 'center'; for (let k = 0; k <= 4; k++) { const v = lo + (hi - lo) * k / 4; g.fillText(Math.round(v), X(v), H - 6); }
  }, 0);
  const sk = h('div'); box.append(sk);
  box.append(h('div', {class: 'card'}, h('div', {class: 'body'}, h('button', {class: 'btn s', on: {click: async () => {
    const r = await api('/api/sell/chain', {date: P.date, time: (SELL.entry && SELL.entry.time) || '09:20'}); sk.innerHTML = '';
    if (r.error) { sk.append(h('div', {class: 'warn'}, r.error)); return; }
    sk.append(h('h3', {}, '📈 IV স্কিউ — ' + r.time + ' · স্পট ' + Math.round(r.spot) + ' · এক্সপায়ারি ' + r.expiry));
    const c2 = h('canvas', {style: 'width:100%;height:210px;background:#10151d;border-radius:10px'}); sk.append(c2);
    const W = c2.clientWidth || 340, H = 210, dpr = Math.min(window.devicePixelRatio || 1, 2); c2.width = W * dpr; c2.height = H * dpr; const g = c2.getContext('2d'); g.scale(dpr, dpr);
    const rows = r.rows.filter(x => x.iv != null), ks = rows.map(x => x.strike), k0 = Math.min(...ks), k1 = Math.max(...ks), iv0 = Math.min(...rows.map(x => x.iv)), iv1 = Math.max(...rows.map(x => x.iv));
    const X = v => 36 + (W - 44) * (v - k0) / Math.max(k1 - k0, 1), Y = v => 10 + (H - 32) * (1 - (v - iv0) / Math.max(iv1 - iv0, 0.01));
    g.fillStyle = '#7d889b'; g.font = '10px sans-serif'; g.textAlign = 'right'; [iv0, (iv0 + iv1) / 2, iv1].forEach(v => g.fillText(v.toFixed(1) + '%', 32, Y(v) + 3));
    g.textAlign = 'center'; for (let k = 0; k <= 4; k++) { const v = k0 + (k1 - k0) * k / 4; g.fillText(Math.round(v), X(v), H - 6); }
    [['CE', '#26c281'], ['PE', '#ff5d5d']].forEach(([t, col]) => { g.strokeStyle = col; g.lineWidth = 2; g.beginPath(); let f = true; rows.filter(x => x.opt === t).forEach(x => { f ? g.moveTo(X(x.strike), Y(x.iv)) : g.lineTo(X(x.strike), Y(x.iv)); f = false; }); g.stroke(); g.fillStyle = col; g.textAlign = 'left'; g.fillText(t, t === 'CE' ? 44 : 70, 14); });
    g.strokeStyle = '#8b96a8'; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(X(r.spot), 8); g.lineTo(X(r.spot), H - 20); g.stroke(); g.setLineDash([]);
    sk.append(h('div', {class: 'note'}, 'স্ট্রাইকওয়ার্ক Black-Scholes IV (সবুজ CE, লাল PE; ড্যাশ = স্পট)। ডেটাবেসে OI নেই বলে Strike-wise OI চার্ট সম্ভব নয়।'));
  }}}, '📈 IV স্কিউ দেখুন'))));
}
function drawSellRes() {
  const box = $('#sell_res'); if (!box) return; box.innerHTML = ''; const r = SELLR, A = r.stats.all, T = r.stats.train, E = r.stats.test;
  if (!A.n) { box.append(h('div', {class: 'warn'}, 'কোনো ট্রেড হয়নি। বাদ পড়া: ' + JSON.stringify(r.meta.skipped))); return; }
  const kp = (l, v, c) => h('div', {class: 'kpi ' + (c === 'neg' ? 'bad' : c === 'pos' ? 'good' : '')}, h('small', {}, l), h('b', {class: c || ''}, v));
  box.append(h('h3', {}, 'ফল — ' + r.meta.from + ' → ' + r.meta.to));
  const M = r.margin || {};
  box.append(h('div', {class: 'kpis'}, kp('নেট P&L', inr(A.net), cls(A.net)), kp('ট্রেড-দিন', A.n), kp('Win %', A.win.toFixed(1) + '%'), kp('Profit Factor', f2(A.pf)),
    kp('Max DD', inr(A.dd) + ' (' + A.dd_pct.toFixed(1) + '%)', 'neg'), kp('Recovery', f2(A.rec)), kp('Sharpe / Sortino', f2(r.sharpe || 0) + ' / ' + f2(r.sortino || 0)), kp('মোট খরচ', inr(A.costs)),
    kp('গড় মার্জিন (আনু.)', inr(M.avg || 0)), kp('ROM মোট / বার্ষিক', (M.rom_total || 0).toFixed(1) + '% / ' + (M.rom_annual || 0).toFixed(1) + '%'), kp('Max DD ÷ মার্জিন', (M.dd_pct_margin || 0).toFixed(1) + '%', 'neg')));
  if (A.n < 60) box.append(h('div', {class: 'warn'}, 'ট্রেড মাত্র ' + A.n + 'টি — সেলিংয়ে কম নমুনায় বড় ট্রেইল-লস লুকিয়ে থাকে; ফলে অতিরিক্ত ভরসা করবেন না।'));
  box.append(h('canvas', {id: 'sell_eq', style: 'width:100%;height:170px'}));
  if (r.meta.split_date) box.append(tbl(['', 'Train', 'Test'], [['ট্রেড', T.n, E.n], ['নেট', pn(T.net), pn(E.net)], ['PF', f2(T.pf), f2(E.pf)], ['Win%', T.win.toFixed(0) + '%', E.win.toFixed(0) + '%'], ['DD', inr(T.dd), inr(E.dd)]]));
  const g2 = (t, a) => { box.append(h('h3', {}, t)); box.append(tbl(['', 'ট্রেড', 'Win%', 'নেট ₹', 'PF', 'গড়/ট্রেড'], a.map(x => [x.k, x.n, x.win.toFixed(0) + '%', pn(x.net), f2(x.pf), pn(x.exp)]))); };
  g2('এক্সিটের কারণ', r.by_reason); g2('সপ্তাহের দিন', r.by_dow); g2('DTE', r.by_dte);
  box.append(h('h3', {}, 'মাস অনুযায়ী')); box.append(tbl(['মাস', 'ট্রেড', 'Win%', 'নেট ₹', 'DD'], r.monthly.map(x => [x.k, x.n, x.win.toFixed(0) + '%', pn(x.net), inr(x.dd)])));
  box.append(h('h3', {}, 'ট্রেড তালিকা (ট্যাপ করলে লেগ-বিবরণ)'));
  const rows = r.trades.map((t, k) => '<tr data-i="' + k + '" style="cursor:pointer"><td>' + (k + 1) + '</td><td>' + t.date + '</td><td>' + t.in + '→' + t.out + '</td><td>' + t.reason + '</td><td class="' + cls(t.net) + '">' + inr(t.net) + '</td><td>' + (t.rom == null ? '' : t.rom + '%') + '</td></tr>').join('');
  const det = h('div'); const tb = h('div', {class: 'scroll', style: 'max-height:420px', html: '<table><tr><th>#</th><th>তারিখ</th><th>সময়</th><th>কারণ</th><th>নেট</th><th>ROM</th></tr>' + rows + '</table>'});
  tb.addEventListener('click', e => { const tr = e.target.closest('tr[data-i]'); if (!tr) return; const t = r.trades[+tr.dataset.i]; det.innerHTML = '';
    det.append(h('h3', {}, t.date + ' · স্পট ' + t.spot + ' · IV ' + (t.iv == null ? '–' : t.iv + '%') + (t.ivp == null ? '' : ' (IVP ' + t.ivp + ')')));
    det.append(tbl(['লেগ', 'স্ট্রাইক', 'Qty', 'ইন', 'আউট', 'এন্ট্রি', 'এক্সিট', 'P&L', 'কারণ'], t.legs.map(l => [l.act + ' ' + l.opt + (l.role === 'hedge' ? '·H' : ''), l.strike, l.qty, l.in, l.out, l.entry, l.exit, pn(l.pnl), l.why])));
    det.append(h('div', {class: 'note', html: 'পিক MTM ' + inr(t.mtm_peak) + ' · সর্বনিম্ন MTM ' + inr(t.mtm_low) + ' · মার্জিন ' + inr(t.margin) + (t.events.length ? '<br>' + t.events.join('<br>') : '')})); det.scrollIntoView({behavior: 'smooth', block: 'nearest'}); });
  box.append(tb, det);
  setTimeout(() => drawLine('sell_eq', r.equity, '#2ee27d', true), 0);
}

/* ---------- 🧠 AI Advisor ---------- */

function hbars(root, labels, vals, title) {
  const mx = Math.max(1, ...vals.map(Math.abs));
  root.append(h('div', {class: 'note', style: 'margin-bottom:2px'}, title));
  root.append(h('div', {style: 'background:#10151d;border-radius:10px;padding:8px 10px', html: labels.map((l, i) => {
    const w = Math.abs(vals[i]) / mx * 46, pos = vals[i] >= 0;
    return '<div style="font-size:11px;color:#9aa6b8;margin-top:5px">' + String(l).replace(/</g, '&lt;') + ' <b style="color:' + (pos ? '#ff5d5d' : '#26c281') + '">' + (vals[i] >= 0 ? '+' : '−') + '₹' + Math.abs(Math.round(vals[i])).toLocaleString('en-IN') + '</b></div>' +
      '<div style="position:relative;height:9px"><div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:#4d5a70"></div><div style="position:absolute;top:1px;height:7px;border-radius:3px;background:' + (pos ? '#ff5d5d' : '#26c281') + ';' + (pos ? 'left:50%;' : 'right:50%;') + 'width:' + w.toFixed(1) + '%"></div></div>';
  }).join('')}));
}
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
    hbars(root, A.ablation.map(a => a.rule), A.ablation.map(a => a.d_net), 'শর্ত বন্ধ করলে নেট বদল (বাঁয়ে = শর্তটি লাভ বাড়াচ্ছে, ডানে = ক্ষতিকর)');
    root.append(h('div', {class: 'note'}, 'Δ ঋণাত্মক = শর্তটি লাভ বাড়াচ্ছে (রাখুন); Δ ধনাত্মক = শর্ত বন্ধ করলে ভালো হতো (শর্তটি ক্ষতিকর/অকেজো হতে পারে)।'));
  }
  if (A.filters.length) {
    root.append(h('h3', {}, 'ফিল্টার মাইনিং (আসল ইঞ্জিনে যাচাই)'));
    root.append(tbl(['অতিরিক্ত শর্ত', 'ট্রেড', 'TRAIN Δ', 'TEST Δ', 'রায়'], A.filters.map(f => [f.rule, f.n, pc(f.d_train), pc(f.d_test), f.chosen ? '✔ নির্বাচিত' : (f.ok ? '✓ ভালো' : '✗')])));
  }
  if (A.curves) {
    root.append(h('h3', {}, '📊 বর্তমান বনাম সাজেশন — ইকুইটি (সময় অনুযায়ী)'));
    const c1 = h('canvas', {style: 'width:100%;height:210px;background:#10151d;border-radius:10px'}); root.append(c1);
    root.append(h('div', {class: 'note'}, 'ধূসর = বর্তমান সেটিং, সবুজ = সাজেশন (এক্সিট + ফিল্টার)। হলুদ খাড়া রেখার ডানদিক = TEST (বাছাইয়ে অদেখা অংশ) — সবুজ রেখা ডানদিকেও ওপরে থাকলেই ভরসা।'));
    setTimeout(() => {
      const C = A.curves, W = c1.clientWidth || 340, H = 210, dpr = Math.min(window.devicePixelRatio || 1, 2); c1.width = W * dpr; c1.height = H * dpr; const g = c1.getContext('2d'); g.scale(dpr, dpr);
      const n = C.dates.length, all = C.base.concat(C.final, [0]), mx = Math.max(...all), mn = Math.min(...all), L = 8, R = 8, T = 8, B = 16;
      const X = i => L + (W - L - R) * i / Math.max(n - 1, 1), Y = v => T + (H - T - B) * (1 - (v - mn) / Math.max(mx - mn, 1));
      g.strokeStyle = '#1c2431'; g.beginPath(); g.moveTo(L, Y(0)); g.lineTo(W - R, Y(0)); g.stroke();
      const ln = (arr, col, w) => { g.strokeStyle = col; g.lineWidth = w; g.beginPath(); arr.forEach((v, i) => i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))); g.stroke(); };
      ln(C.base, '#8b96a8', 1.5); ln(C.final, '#26c281', 2.2);
      if (C.split) { const k = C.dates.findIndex(d => d >= C.split); if (k > 0) { g.strokeStyle = '#f1c40f'; g.setLineDash([4, 3]); g.beginPath(); g.moveTo(X(k), T); g.lineTo(X(k), H - B); g.stroke(); g.setLineDash([]); } }
      g.fillStyle = '#7d889b'; g.font = '10px sans-serif'; g.textAlign = 'left'; g.fillText(C.dates[0], L, H - 3); g.textAlign = 'right'; g.fillText(C.dates[n - 1], W - R, H - 3);
    }, 0);
  }
  if (A.mc && A.mc.final) {
    const M = A.mc.final, Mb = A.mc.base;
    root.append(h('h3', {}, '🌫 Monte Carlo ফ্যান (সাজেশন, ' + M.n + ' ট্রেডের ক্রম ওলট-পালট × ৫০০)'));
    const c2 = h('canvas', {style: 'width:100%;height:210px;background:#10151d;border-radius:10px'}); root.append(c2);
    setTimeout(() => {
      const W = c2.clientWidth || 340, H = 210, dpr = Math.min(window.devicePixelRatio || 1, 2); c2.width = W * dpr; c2.height = H * dpr; const g = c2.getContext('2d'); g.scale(dpr, dpr);
      const n = M.p50.length, all = M.p5.concat(M.p95, [0]), mx = Math.max(...all), mn = Math.min(...all), L = 8, R = 8, T = 8, B = 8;
      const X = i => L + (W - L - R) * i / Math.max(n - 1, 1), Y = v => T + (H - T - B) * (1 - (v - mn) / Math.max(mx - mn, 1));
      const band = (a, b, col) => { g.fillStyle = col; g.beginPath(); a.forEach((v, i) => i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))); for (let i = n - 1; i >= 0; i--) g.lineTo(X(i), Y(b[i])); g.closePath(); g.fill(); };
      band(M.p95, M.p5, 'rgba(38,194,129,.16)'); band(M.p75, M.p25, 'rgba(38,194,129,.30)');
      g.strokeStyle = '#1c2431'; g.beginPath(); g.moveTo(L, Y(0)); g.lineTo(W - R, Y(0)); g.stroke();
      g.strokeStyle = '#26c281'; g.lineWidth = 2; g.beginPath(); M.p50.forEach((v, i) => i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))); g.stroke();
      g.strokeStyle = '#ff5d5d'; g.lineWidth = 1; g.setLineDash([3, 3]); g.beginPath(); M.p5.forEach((v, i) => i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))); g.stroke(); g.setLineDash([]);
    }, 0);
    root.append(tbl(['', 'বর্তমান', 'সাজেশন'], [['শেষে নেট — খারাপ ৫% (P5)', pn(Mb ? Mb.end_p5 : 0), pn(M.end_p5)], ['শেষে নেট — মধ্যম (P50)', pn(Mb ? Mb.end_p50 : 0), pn(M.end_p50)], ['শেষে নেট — ভালো ৫% (P95)', pn(Mb ? Mb.end_p95 : 0), pn(M.end_p95)],
      ['Max DD — মধ্যম', inr(Mb ? Mb.dd_p50 : 0), inr(M.dd_p50)], ['Max DD — খারাপ ৫%', inr(Mb ? Mb.dd_p95 : 0), inr(M.dd_p95)], ['লাভে শেষ হওয়ার সম্ভাবনা', (Mb ? Mb.p_profit.toFixed(0) : '–') + '%', M.p_profit.toFixed(0) + '%'], ['মূলধনের ৫০% হারানোর সম্ভাবনা', (Mb ? Mb.p_ruin.toFixed(1) : '–') + '%', M.p_ruin.toFixed(1) + '%']]));
    root.append(h('div', {class: 'note'}, 'সবুজ ছায়া: ৫০% ও ৯০% সম্ভাব্য পরিসর; লাল ড্যাশ = খারাপ ৫% পথ। এটা ঐতিহাসিক ট্রেড থেকে নমুনা নেওয়া, ভবিষ্যৎ-নিশ্চয়তা নয়।'));
  }
  if (A.ablation && A.ablation.length) {
    root.append(h('h3', {}, '📊 Rule Ablation বার চার্ট (শর্ত বন্ধ করলে নেট কত বদলায়)'));
    const mxA = Math.max(...A.ablation.map(a => Math.abs(a.d_net)), 1);
    root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, h('div', {style: 'flex:1 1 100%', html: A.ablation.map(a => '<div style="margin:5px 0"><div style="font-size:11.5px">' + a.rule + ' <b class="' + (a.d_net <= 0 ? 'pos' : 'neg') + '">' + (a.d_net <= 0 ? 'কাজের শর্ত' : 'ক্ষতিকর হতে পারে') + '</b></div><div style="height:12px;border-radius:6px;background:#1a2230;position:relative"><div style="position:absolute;top:0;height:12px;border-radius:6px;width:' + (Math.abs(a.d_net) / mxA * 48) + '%;' + (a.d_net >= 0 ? 'left:50%;background:#ff5d5d' : 'right:50%;background:#26c281') + '"></div></div></div>').join('')}))));
  }
  if (A.ind_search && A.ind_search.length) {
    root.append(h('h3', {}, 'ইন্ডিকেটর ও থ্রেশহোল্ড সার্চ'));
    root.append(tbl(['প্যারামিটার', 'এখন', 'চেষ্টা', 'TRAIN Δ', 'TEST Δ', 'রায়'], A.ind_search.map(r => [r.param.replace('cases.', 'কেস.').replace('.entry.', '.শর্ত.'), r.from, r.to, pc(r.d_train), pc(r.d_test), r.chosen ? '✔ নির্বাচিত' : (r.ok ? (r.plateau === false ? '✗ স্পাইক (প্ল্যাটো নেই)' : '✓') : '✗')])));
    root.append(h('div', {class: 'note'}, 'প্রতিটি মান আসল ইঞ্জিনে চলেছে; নির্বাচনের আগে দুই পাশের প্রতিবেশী মানও ≥৮৫% স্কোর রাখতে হয় — একক চূড়া (স্পাইক) বাদ।'));
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
  /* ---- LLM কো-পাইলট (ঐচ্ছিক) ---- */
  const lk = h('input', {type: 'password', placeholder: 'Gemini (AIza...) বা Anthropic (sk-ant-...) API Key — শুধু এই ফোনে থাকে', style: 'flex:1 1 100%'}); try { lk.value = localStorage.getItem('gb_llm_key') || ''; } catch (e) {}
  lk.addEventListener('change', () => { try { localStorage.setItem('gb_llm_key', lk.value.trim()); } catch (e) {} });
  const lm = h('input', {type: 'text', placeholder: 'মডেল (ঐচ্ছিক) — ফাঁকা = ডিফল্ট (Gemini: gemini-3.5-flash-lite)', style: 'flex:1 1 100%'}); try { lm.value = localStorage.getItem('gb_llm_model') || ''; } catch (e) {}
  lm.addEventListener('change', () => { try { localStorage.setItem('gb_llm_model', lm.value.trim()); } catch (e) {} });
  const prov = h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}); const showProv = () => { const k = lk.value.trim(); prov.textContent = !k ? '' : k.startsWith('AIza') ? '✓ Google Gemini শনাক্ত' : k.startsWith('sk-ant-') ? '✓ Anthropic শনাক্ত' : '⚠ Key-র ধরন চেনা যায়নি (AIza… বা sk-ant-…)'; }; lk.addEventListener('input', showProv); showProv();
  const li = h('textarea', {rows: 2, placeholder: 'আইডিয়া লিখুন, যেমন: "১৫ মিনিটের ORB ব্রেকআউটে ADX ২০-র ওপরে হলে CE কিনব"', style: 'width:100%;flex:1 1 100%;background:#0b130f;color:inherit;border:1px solid var(--line);border-radius:10px;padding:8px;font:inherit'});
  const lo = h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'});
  root.append(h('div', {class: 'h2'}, '🤖 LLM কো-পাইলট (ঐচ্ছিক)'));
  root.append(h('div', {class: 'card'}, h('div', {class: 'body'}, lk, prov, lm, li,
    h('button', {class: 'btn p', on: {click: async () => { lo.textContent = 'ভাবছে…'; const r = await api('/api/copilot', {mode: 'idea', text: li.value, key: lk.value.trim(), model: lm.value.trim()}); if (r.error) { lo.textContent = '⚠ ' + r.error; return; } S.cases.push(r.case); persist(); renderBuild(); lo.textContent = '✓ নতুন Case যোগ হয়েছে: ' + r.case.name + ' — Strategy ট্যাবে দেখে নিন, তারপর Run দিন।'; }}}, '💡 আইডিয়া → Case'),
    h('button', {class: 'btn s', on: {click: async () => { if (!AIR2 && !AIR) { lo.textContent = 'আগে অ্যাডভাইজার চালান।'; return; } lo.textContent = 'ব্যাখ্যা তৈরি হচ্ছে…'; const A = AIR2 || AIR; const pl = AIR2 ? {validation: A.validation, base: A.base, final: A.final, wfo: A.wfo, changes: A.changes, ablation: A.ablation} : {validation: A.validation, changes: A.changes}; const r = await api('/api/copilot', {mode: 'explain', payload: pl, key: lk.value.trim(), model: lm.value.trim()}); lo.textContent = r.error ? '⚠ ' + r.error : r.text; lo.style.whiteSpace = 'pre-wrap'; }}}, '🗣 ফলাফল ব্যাখ্যা'),
    lo, h('div', {class: 'note', style: 'flex:1 1 100%;margin:0'}, 'LLM কোনো গণিত করে না — শুধু আইডিয়াকে Case-এ রূপ দেয় (আমাদের ইঞ্জিন যাচাই করে) ও ফলাফল বোঝায়। Key ইন্টারনেটের মাধ্যমে Anthropic-এ যায়; না দিলে পুরো সিস্টেম আগের মতোই অফলাইনে চলে।'))));
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
  const kl = h('input', {type: 'number', min: 0, placeholder: '0 = বন্ধ', value: PP.kill_loss || ''}); kl.addEventListener('change', () => PP.kill_loss = parseFloat(kl.value) || 0);
  box.append(F('⛔ Kill-Switch: দৈনিক সর্বোচ্চ লস ₹', kl));
  if (PP.mode === 'live') {
    const kf = h('input', {type: 'number', min: 0, placeholder: '0 = বন্ধ', value: PP.kill_feed || ''}); kf.addEventListener('change', () => PP.kill_feed = parseInt(kf.value) || 0);
    const tt = h('input', {type: 'text', placeholder: 'Telegram Bot Token (ঐচ্ছিক)', value: PP.tg_token || ''}), tc = h('input', {type: 'text', placeholder: 'Chat ID', value: PP.tg_chat || ''});
    tt.addEventListener('change', () => PP.tg_token = tt.value); tc.addEventListener('change', () => PP.tg_chat = tc.value);
    box.append(F('ফিড টানা ব্যর্থ হলে বন্ধ (বার)', kf), F('Telegram Token', tt), F('Telegram Chat ID', tc),
      h('button', {class: 'btn s', on: {click: async () => { const r = await api('/api/alert/test', {token: PP.tg_token, chat: PP.tg_chat}); alert(r.ok ? 'টেস্ট বার্তা পাঠানো হয়েছে — Telegram দেখুন' : (r.error || 'ব্যর্থ')); }}}, '📨 অ্যালার্ট টেস্ট'));
  }
  box.append(h('button', {class: 'btn g', on: {click: async () => {
    const r = await api('/api/paper/start', {mode: PP.mode, settings: S, from: PP.from, to: PP.to, delay: PP.delay, step: PP.step, band: PP.band, feed: 'upstox', inst: META.info.inst, kill_loss: PP.kill_loss || 0, kill_feed: PP.kill_feed || 0, tg_token: PP.tg_token || '', tg_chat: PP.tg_chat || ''});
    if (r.error) { alert(r.error); return; }
    PP.last = {running: true, status: 'running', msg: 'শুরু হচ্ছে…', stats: {n: 0, win: 0, pf: 0, dd: 0}, trades: [], events: [], live_pnl: 0}; PP.run = true; PP.sig = '';
    drawPaperForm(); drawPaperStat(); paperPoll();
  }}}, '▶ পেপার শুরু'));
  f.append(h('div', {class: 'card'}, box));
}

/* সাধারণ লাইন-চার্ট (মিনিট-অক্ষ, মার্কারসহ) */
function drawSeries(cv, ys, ts, marks, zero) {
  const W = cv.clientWidth || 340, H = cv.clientHeight || 160, dpr = Math.min(window.devicePixelRatio || 1, 2);
  cv.width = W * dpr; cv.height = H * dpr; const g = cv.getContext('2d'); g.scale(dpr, dpr);
  let mn = Math.min(...ys), mx = Math.max(...ys); if (zero) { mn = Math.min(mn, 0); mx = Math.max(mx, 0); } if (mx === mn) { mx += 1; mn -= 1; }
  const L = 6, R = 48, T = 8, B = 16, X = i => L + (W - L - R) * i / Math.max(ys.length - 1, 1), Y = v => T + (H - T - B) * (1 - (v - mn) / (mx - mn));
  g.font = '10px sans-serif'; g.fillStyle = '#7d889b'; g.strokeStyle = '#1c2431';
  for (let k = 0; k <= 3; k++) { const v = mn + (mx - mn) * k / 3, y = Y(v); g.beginPath(); g.moveTo(L, y); g.lineTo(W - R, y); g.stroke(); g.textAlign = 'left'; g.fillText(Math.abs(v) >= 1000 ? Math.round(v).toLocaleString('en-IN') : v.toFixed(1), W - R + 4, y + 3); }
  if (zero) { g.strokeStyle = '#4d5a70'; g.setLineDash([4, 3]); g.beginPath(); g.moveTo(L, Y(0)); g.lineTo(W - R, Y(0)); g.stroke(); g.setLineDash([]); }
  const up = ys[ys.length - 1] >= ys[0]; g.strokeStyle = zero ? (ys[ys.length - 1] >= 0 ? '#26c281' : '#ff5d5d') : (up ? '#26c281' : '#ff5d5d'); g.lineWidth = 1.8; g.beginPath();
  ys.forEach((v, i) => i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))); g.stroke();
  const hmm = m => String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
  [0, Math.floor(ys.length / 2), ys.length - 1].forEach(i => { g.fillStyle = '#7d889b'; g.textAlign = i === 0 ? 'left' : i === ys.length - 1 ? 'right' : 'center'; g.fillText(hmm(ts[i]), X(i), H - 4); });
  (marks || []).forEach(m => { const mm = parseInt(m.t.slice(0, 2)) * 60 + parseInt(m.t.slice(3, 5)); let bi = 0, bd = 1e9; ts.forEach((t, i) => { const d = Math.abs(t - mm); if (d < bd) { bd = d; bi = i; } });
    g.fillStyle = m.k === 'ENTRY' ? '#26c281' : m.k === 'EXIT' ? '#ff5d5d' : '#f5c542'; g.beginPath(); g.arc(X(bi), Y(ys[bi]), 5, 0, 7); g.fill(); g.fillStyle = '#fff'; g.font = 'bold 8px sans-serif'; g.textAlign = 'center'; g.fillText(m.k[0], X(bi), Y(ys[bi]) + 3); });
}
function drawPaperStat() {
  const root = $('#pp-stat'); if (!root) return;
  const P = PP.last;
  const sig = JSON.stringify([P && P.status, P && P.msg, P && P.now, P && P.stats && P.stats.n, P && P.live_pnl, P && P.open && P.open.net, P && P.events && P.events.length, P && P.mtm && P.mtm.length, PP.stopping, P && P.days_done]);
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
  if (P.spot && P.spot.length > 1) {
    root.append(h('h3', {}, '📈 লাইভ স্পট (আজ) ও এন্ট্রি/এক্সিট'));
    const c1 = h('canvas', {style: 'width:100%;height:170px;background:#10151d;border-radius:10px'}); root.append(c1);
    setTimeout(() => drawSeries(c1, P.spot.map(x => x[1]), P.spot.map(x => x[0]), (P.events || []).filter(e => e.day === P.day && (e.k === 'ENTRY' || e.k === 'EXIT' || e.k === 'KILL')).map(e => ({t: e.t, k: e.k})), false), 0);
  }
  if (P.mtm && P.mtm.length > 1) {
    root.append(h('h3', {}, '💹 লাইভ MTM ইকুইটি (বন্ধ + খোলা ট্রেড)'));
    const c2 = h('canvas', {style: 'width:100%;height:150px;background:#10151d;border-radius:10px'}); root.append(c2);
    setTimeout(() => drawSeries(c2, P.mtm.map(x => x[1]), P.mtm.map(x => x[0] % 1440), [], true), 0);
  }
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
  for (const k of ['build', 'res', 'tune', 'ai', 'sell', 'paper', 'pre', 'data']) $('#t-' + k).hidden = k !== t;
  if (t === 'res') drawEq();
  if (t === 'data') renderData();
  if (t === 'ai') renderAI();
  if (t === 'sell') renderSell();
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
