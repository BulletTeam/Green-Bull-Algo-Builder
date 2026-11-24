"""সেটিংস। ডেটা ও টোকেন GB_HOME ফোল্ডারে থাকে (GitHub-এ যায় না)।"""
import os
from pathlib import Path

HOME = Path(os.environ.get("GB_HOME", str(Path.home() / "greenbull-data")))
HOME.mkdir(parents=True, exist_ok=True)

TOKEN_FILE = HOME / "upstox_token.txt"
DB_FILE = HOME / "gb.db"

V3 = "https://api.upstox.com/v3/historical-candle"

INSTR = {
    "BN": {"name": "BankNifty", "key": "NSE_INDEX|Nifty Bank"},
    "N50": {"name": "Nifty 50", "key": "NSE_INDEX|Nifty 50"},
    "FIN": {"name": "FinNifty", "key": "NSE_INDEX|Nifty Fin Service"},
    "SEN": {"name": "Sensex", "key": "BSE_INDEX|SENSEX"},
}

START_DAY = "2022-01-03"   # স্পট ১-মিনিট এখান থেকে পাওয়া যায়
CHUNK_DAYS = 28            # একবারে কত দিন নামানো হবে
SESSION_START = 9 * 60 + 15   # 09:15 IST (মিনিট)
SESSION_END = 15 * 60 + 29    # 15:29 IST (মিনিট)
CLOSE_AFTER = "15:40"         # এর আগে আজকের দিন অসম্পূর্ণ ধরা হয়
