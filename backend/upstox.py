"""Upstox HTTP ক্লায়েন্ট। টোকেন কখনো ছাপা হয় না।"""
import datetime as dt
import time
from urllib.parse import quote

import requests

from config import TOKEN_FILE, V3


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
