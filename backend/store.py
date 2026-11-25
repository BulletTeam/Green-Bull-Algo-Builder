"""SQLite: সব ডেটা একটাই ফাইলে (gb.db)। প্রতিটি কাজে নতুন connection খোলা হয়, তাই থ্রেডের সমস্যা হয় না।"""
import datetime as dt
import sqlite3

from config import DB_FILE, OPT_DB_FILE

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


# ---------------------------------------------------------------- অপশন (gb_opt.db)
OPT_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS contracts (
        cid INTEGER PRIMARY KEY,
        ikey TEXT UNIQUE NOT NULL,
        instr TEXT NOT NULL,
        expiry TEXT NOT NULL,
        strike REAL NOT NULL,
        otype TEXT NOT NULL,           -- CE / PE
        weekly INTEGER,                -- 1 = সাপ্তাহিক, 0 = মাসিক
        lot INTEGER,
        symbol TEXT)""",
    """CREATE TABLE IF NOT EXISTS obars (
        cid INTEGER NOT NULL,
        t INTEGER NOT NULL,            -- মিনিট (epoch থেকে, IST)
        o INTEGER, h INTEGER, l INTEGER, c INTEGER,   -- দাম পয়সায় (×১০০), ছোট ফাইলের জন্য
        v INTEGER, oi INTEGER,
        PRIMARY KEY (cid, t)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS ostate (
        cid INTEGER PRIMARY KEY,
        done INTEGER, rows INTEGER, updated_at TEXT)""",
]


def opt_conn():
    c = sqlite3.connect(OPT_DB_FILE)
    for stmt in OPT_SCHEMA:
        c.execute(stmt)
    c.commit()
    typ = c.execute("SELECT type FROM pragma_table_info('obars') WHERE name='o'").fetchone()
    if typ and typ[0].upper() != "INTEGER":
        c.close()
        raise RuntimeError("gb_opt.db পুরনো ফরম্যাটের। মুছে ফেলে আবার চালান: rm %s" % OPT_DB_FILE)
    return c


def opt_contract(c, instr, ct):
    """কন্ট্র্যাক্ট না থাকলে যোগ করে; cid ফেরত দেয়।"""
    row = c.execute("SELECT cid FROM contracts WHERE ikey=?", (ct["instrument_key"],)).fetchone()
    if row:
        return row[0]
    cur = c.execute(
        "INSERT INTO contracts(ikey, instr, expiry, strike, otype, weekly, lot, symbol) VALUES (?,?,?,?,?,?,?,?)",
        (ct["instrument_key"], instr, str(ct.get("expiry"))[:10], float(ct["strike_price"]),
         ct.get("instrument_type"), int(bool(ct.get("weekly"))), ct.get("lot_size"), ct.get("trading_symbol")),
    )
    return cur.lastrowid


def opt_is_done(c, cid):
    row = c.execute("SELECT done FROM ostate WHERE cid=?", (cid,)).fetchone()
    return bool(row and row[0])


def opt_save(c, cid, rows, done):
    """rows: (t, o, h, l, c, v, oi)। একসাথে সেভ + অবস্থা লেখা।"""
    c.executemany(
        "INSERT OR REPLACE INTO obars(cid, t, o, h, l, c, v, oi) VALUES (?,?,?,?,?,?,?,?)",
        [(cid,) + r for r in rows],
    )
    c.execute(
        "INSERT OR REPLACE INTO ostate(cid, done, rows, updated_at) VALUES (?,?,?,?)",
        (cid, int(done), len(rows), dt.datetime.now().isoformat(timespec="seconds")),
    )
    c.commit()
