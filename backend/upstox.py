"""Upstox HTTP ক্লায়েন্ট। টোকেন কখনো ছাপা হয় না।"""
import datetime as dt
import time
from urllib.parse import quote

import requests

from config import TOKEN_FILE, V2, V3


class TokenError(Exception):
    pass


_session = None


def _session_obj():
    global _session
    if _session is None:
        if not TOKEN_FILE.exists():
            raise TokenError("টোকেন ফাইল নেই: %s" % TOKEN_FILE)
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if not token:
            raise TokenError("টোকেন ফাইল খালি")
        _session = requests.Session()
        _session.headers.update({"Accept": "application/json", "Authorization": "Bearer " + token})
    return _session


def get(url):
    """(status, json). 429 বা সার্ভার এরর হলে কয়েকবার আবার চেষ্টা করে।"""
    s = _session_obj()
    for attempt in range(5):
        try:
            r = s.get(url, timeout=40)
        except requests.RequestException:
            time.sleep(3)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(3 * (attempt + 1))
            continue
        try:
            body = r.json()
        except ValueError:
            body = {}
        time.sleep(0.5)
        return r.status_code, body
    return 0, {}


def fetch_spot_1m(key, d0, d1):
    """d0..d1 (date) এর ১-মিনিট ক্যান্ডেল, Upstox-এর কাঁচা সারি। 401 হলে TokenError।"""
    q = quote(key, safe="")
    rows = []
    cur = d0
    while cur <= d1:
        nxt = min(cur + dt.timedelta(days=28 - 1), d1)
        url = "%s/%s/minutes/1/%s/%s" % (V3, q, nxt.isoformat(), cur.isoformat())
        status, body = get(url)
        if status == 401:
            raise TokenError("401: টোকেন কাজ করছে না বা মেয়াদ শেষ")
        if status != 200:
            print("  সতর্কতা: %s → %s, status %s" % (cur, nxt, status))
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict):
            rows += data.get("candles", []) or []
        cur = nxt + dt.timedelta(days=1)
    return rows


def _list(body):
    d = body.get("data") if isinstance(body, dict) else None
    return d if isinstance(d, list) else []


def get_expiries(key):
    """এক্সপায়ারি তালিকা (date-এর sorted তালিকা)। ব্যর্থ হলে None।"""
    st, body = get("%s/expiries?instrument_key=%s" % (V2, quote(key, safe="")))
    if st == 401:
        raise TokenError("401: টোকেন কাজ করছে না বা মেয়াদ শেষ")
    if st != 200:
        return None
    out = set()
    for x in _list(body):
        try:
            out.add(dt.date.fromisoformat(str(x)[:10]))
        except ValueError:
            pass
    return sorted(out)


def get_contracts(key, expiry):
    """(status, কন্ট্র্যাক্টের তালিকা) — একটি এক্সপায়ারির সব CE/PE।"""
    url = "%s/option/contract?instrument_key=%s&expiry_date=%s" % (V2, quote(key, safe=""), expiry.isoformat())
    st, body = get(url)
    if st == 401:
        raise TokenError("401: টোকেন কাজ করছে না বা মেয়াদ শেষ")
    return st, _list(body)


def fetch_option_1m(ikey, d0, d1):
    """একটি অপশন কন্ট্র্যাক্টের ১-মিনিট ক্যান্ডেল d0..d1। (সব ঠিক কি না, কাঁচা সারি)।"""
    q = quote(ikey, safe="")
    rows = []
    ok = True
    cur = d0
    while cur <= d1:
        nxt = min(cur + dt.timedelta(days=27), d1)     # একবারে সর্বোচ্চ ~১ মাস
        url = "%s/historical-candle/%s/1minute/%s/%s" % (V2, q, nxt.isoformat(), cur.isoformat())
        st, body = get(url)
        if st == 401:
            raise TokenError("401: টোকেন কাজ করছে না বা মেয়াদ শেষ")
        if st != 200:
            ok = False
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict):
            rows += data.get("candles", []) or []
        cur = nxt + dt.timedelta(days=1)
    return ok, rows
