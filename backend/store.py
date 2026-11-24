"""SQLite: সব ডেটা একটাই ফাইলে (gb.db)। প্রতিটি কাজে নতুন connection খোলা হয়, তাই থ্রেডের সমস্যা হয় না।"""
import datetime as dt
import sqlite3

from config import DB_FILE

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS bars (
        instr TEXT NOT NULL,
        t INTEGER NOT NULL,            -- মিনিট (epoch থেকে, IST সময়)
        o REAL, h REAL, l REAL, c REAL, v REAL,
        PRIMARY KEY (instr, t)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY,
        run_at TEXT, instr TEXT, action TEXT, ok INTEGER, detail TEXT)""",
]


def conn():
    c = sqlite3.connect(DB_FILE)
    for stmt in SCHEMA:
        c.execute(stmt)
    c.commit()
    return c


def last_minute(c, instr):
    row = c.execute("SELECT MAX(t) FROM bars WHERE instr=?", (instr,)).fetchone()
    return row[0]


def upsert(c, instr, rows):
    """rows: (t, o, h, l, c, v) এর তালিকা। একই মিনিট থাকলে নতুনটা জেতে।"""
    c.executemany(
        "INSERT OR REPLACE INTO bars(instr, t, o, h, l, c, v) VALUES (?,?,?,?,?,?,?)",
        [(instr,) + r for r in rows],
    )
    c.commit()


def summary(c, instr):
    n, t0, t1, days = c.execute(
        "SELECT COUNT(*), MIN(t), MAX(t), COUNT(DISTINCT t/1440) FROM bars WHERE instr=?", (instr,)
    ).fetchone()
    if not n:
        return {"bars": 0}
    bad = c.execute(
        "SELECT COUNT(*) FROM bars WHERE instr=? AND (h < MAX(o,c) - 1e-6 OR l > MIN(o,c) + 1e-6)",
        (instr,),
    ).fetchone()[0]
    short = c.execute(
        "SELECT COUNT(*) FROM (SELECT t/1440 d FROM bars WHERE instr=? GROUP BY d HAVING COUNT(*) < 360)",
        (instr,),
    ).fetchone()[0]
    epoch = dt.date(1970, 1, 1)
    return {
        "bars": n,
        "days": days,
        "first_day": (epoch + dt.timedelta(days=t0 // 1440)).isoformat(),
        "last_day": (epoch + dt.timedelta(days=t1 // 1440)).isoformat(),
        "bad_ohlc": bad,
        "short_days": short,
    }


def log(c, instr, action, ok, detail):
    c.execute(
        "INSERT INTO runs(run_at, instr, action, ok, detail) VALUES (?,?,?,?,?)",
        (dt.datetime.now().isoformat(timespec="seconds"), instr, action, int(ok), detail),
    )
    c.commit()
