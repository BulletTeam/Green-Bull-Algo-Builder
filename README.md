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
