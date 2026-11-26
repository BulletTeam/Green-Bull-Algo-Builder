# Green Bull Algo Builder

- `backend/` — FastAPI + SQLite (ডেটা নামানো, পরে ব্যাকটেস্ট)
- `flutter_app/` — Flutter অ্যাপ (পরে)

ডেটা (`gb.db`) এবং Upstox টোকেন GitHub-এ যায় না। সেগুলো `~/greenbull-data/` ফোল্ডারে থাকে।

## Termux-এ চালানো (প্রথমবার)

```
pkg install -y python git
git clone https://github.com/BulletTeam/green-bull-algo-builder
cd green-bull-algo-builder/backend
pip install -r requirements.txt   # starlette + uvicorn + requests (Rust লাগে না)
mkdir -p ~/greenbull-data
cp ~/storage/downloads/Algo/upstox_token.txt ~/greenbull-data/
python downloader.py
```

সার্ভার চালু করতে:

```
uvicorn app:app --host 127.0.0.1 --port 8000
```

ব্রাউজারে `http://127.0.0.1:8000/status` খুললে ডেটার অবস্থা দেখাবে।

রোজ আপডেট: `python downloader.py` (শুধু নতুন দিনগুলো যোগ হয়)।

*এটি শিক্ষামূলক তথ্য, আর্থিক পরামর্শ নয়।*

## বিল্ডার (ব্যাকটেস্ট UI)

`builder/algo_builder.py` — ব্রাউজারে চলা ব্যাকটেস্টার। ডেটা `~/greenbull-data/` থেকে সরাসরি পড়ে (gb.db স্পট + gb_opt.db অপশন)।

```
pip install numpy pandas
cd builder
python algo_builder.py --db ~/greenbull-data --inst N50
```

ব্রাউজারে `http://127.0.0.1:8000` খুলুন। Data ট্যাবে ইনডেক্স (BN/N50/FIN/SEN) ও তারিখ বেছে "লোড করুন" চাপলে ডেটাসেট বদলায়।
তারিখ না দিলে সব ডেটা লোড হয় (বেশি সময় ও মেমোরি লাগে; দরকারে `--from 2026-01-01` বা Data ট্যাবে তারিখ সীমা দিন)।

সোর্স `builder/src/`-তে (`python make.py` দিলে `algo_builder.py` আবার তৈরি হয়)।

### ডেটা আপডেট ও ফিউচার ভলিউম

- Data ট্যাবের **"🔄 ডেটা আপডেট"** বোতাম: স্পট → ফিউচার ভলিউম → অপশন, পালা করে নামায় (যা নেমেছে তা বাদ যায়)।
- নিজে চালাতে (backend ফোল্ডার থেকে): `python downloader.py`, `python futures.py`, `python options.py`।
- স্পট ইনডেক্সে ভলিউম থাকে না। `futures.py` নিকটতম মাসিক ফিউচারের ১-মিনিট ভলিউম `gb.db`-র `fvol` টেবিলে রাখে, বিল্ডার লোডের সময় সেটা ভলিউম হিসেবে বসায়। হেডারে "ভলিউম ✓ N দিন" দেখায়।
- Optimize ট্যাবে **"সব ইনডেক্সে চালান"**: একই স্ট্র্যাটেজি BN/N50/FIN/SEN-এ পালা করে চালিয়ে ফল পাশাপাশি দেখায়।
