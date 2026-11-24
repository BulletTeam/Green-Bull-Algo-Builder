"""সেটিংস। ডেটা ও টোকেন GB_HOME ফোল্ডারে থাকে (GitHub-এ যায় না)।"""
import os
from pathlib import Path

HOME = Path(os.environ.get("GB_HOME", str(Path.home() / "greenbull-data")))
HOME.mkdir(parents=True, exist_ok=True)

TOKEN_FILE = HOME / "upstox_token.txt"
DB_FILE = HOME / "gb.db"            # স্পট ডেটা
OPT_DB_FILE = HOME / "gb_opt.db"    # অপশন ডেটা (আলাদা ফাইল)

V2 = "https://api.upstox.com/v2/expired-instruments"
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
SESSION_END = 15 * 60 + 39    # 15:39 IST (মিনিট); ২০২৬-০৮-০৩ থেকে F&O বাজার ১৫:৪০ পর্যন্ত খোলা
CLOSE_AFTER = "15:50"         # এর আগে আজকের দিন অসম্পূর্ণ ধরা হয়

# ---- অপশন ----
OPT_START = "2024-10-01"        # অপশন ইতিহাস এখান থেকে
OPT_BAND = 5                    # ATM-এর ওপরে-নিচে কয়টি স্ট্রাইক (ITM5–OTM5)
WEEKLY_WINDOW = 14              # সাপ্তাহিক এক্সপায়ারির কত দিন আগে থেকে রাখব (নিকটতম ২টা সাপ্তাহিক)
MONTHLY_WINDOW = 35             # মাসিক এক্সপায়ারির কত দিন আগে থেকে রাখব
OPT_SESSION_END = 15 * 60 + 39  # অপশনে ১৫:৩৯ পর্যন্ত বার রাখা হয়
