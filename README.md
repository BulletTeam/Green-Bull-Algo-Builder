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

### 🧠 AI Advisor (সাজেশন ইঞ্জিন)

বিল্ডারের **AI** ট্যাব: বর্তমান স্ট্র্যাটেজির SL, টার্গেট, ট্রেইলিং, ব্রেকইভেন, T1/T2, স্ট্রাইক, টাইম-স্টপ, এন্ট্রি-সময়, দিনে ট্রেড-সংখ্যার প্রতিটি মান **আসল ব্যাকটেস্ট ইঞ্জিনে** চালিয়ে মাপে।
- বাছাই শুধু TRAIN অংশে (পাশের মানের সাথে মসৃণ করে, যাতে একক ভাগ্যবান মান না জেতে); TEST অংশ আলাদা রেখে যাচাই; প্রতিটি পরিবর্তনের পাশে TEST ✓/✗।
- বুটস্ট্র্যাপে গড়-নেটের আস্থা-সীমা, সম্ভাব্য সর্বোচ্চ DD ও প্রয়োজনীয় মূলধন।
- Results ট্যাবের MAE/MFE এখন পথ-সচেতন (SL/টার্গেটে বেরোনোর পর দাম ধরা হয় না); আনুমানিক what-if সরানো হয়েছে।

*এটি শিক্ষামূলক তথ্য, আর্থিক পরামর্শ নয়।*

**অনুরোধ বাক্স:** AI ট্যাবে লিখুন, যেমন "ড্রডাউন ৩০% কমাও", "লাভ বাড়াও", "win rate বাড়াও, SL ২৫% এর বেশি না", "ট্রেইল চাই না", "স্ট্রাইক বদলিও না", "ITM চাই না", "ড্রডাউন ১০% এর মধ্যে রাখো"। বাংলা/English কীওয়ার্ড-নিয়মে বোঝা হয় (LLM নয়); লক্ষ্য অনুযায়ী স্কোর বদলে আসল ব্যাকটেস্ট আবার চলে। অনুরোধ জমা হয়; কী বোঝা গেল/গেল না তা দেখায়।

### 📄 Paper Trading (Paper ট্যাব)

- **রিপ্লে মোড:** পুরনো দিনগুলো লাইভের মতো টিক-টিক করে চালায় (ফিড ছাড়াই)। শেষে "ব্যাকটেস্টের সাথে মিলিয়ে দেখুন" বোতামে হুবহু মিল যাচাই।
- **লাইভ মোড:** Upstox থেকে আজকের ১-মিনিট স্পট + ATM-এর আশপাশের অপশন টেনে প্রতি মিনিটে হিসাব; আসল অর্ডার যায় না। আগে "🔌 লাইভ কানেকশন টেস্ট" চালান (টোকেন `~/greenbull-data/upstox_token.txt`)।
- ইঞ্জিন আলাদা নয়: ব্যাকটেস্টের কোডই আজকের দিনের জন্য "এখন পর্যন্ত জানা ডেটা" (সম্পূর্ণ চার্ট বার + চলতি মিনিটের শুধু open) দিয়ে চলে। সেশন `~/greenbull-data/paper/`-এ সেভ হয়।
- মাল্টি-ব্রোকার: ফিড কাঠামো তৈরি (`LIVE_FEEDS`), এখন শুধু Upstox।

### AI Advisor v2 (গভীর বিশ্লেষণ)
Rule Ablation (কোন এন্ট্রি-শর্ত কাজ করছে), Filter Mining (আসল ইঞ্জিনে যাচাই), স্মার্ট সার্চ (random + hill-climb) ও রোলিং Walk-Forward। AI ট্যাবে "🔬 গভীর বিশ্লেষণ (v2)"। সাথে: হোল্ড-টাইম সংশোধন, লং-SL ফ্লোর ০.০৫।

### উন্নত অ্যানালিটিক্স (Results ট্যাব)
Sharpe/Sortino/Calmar/SQN, E-ratio, আন্ডারওয়াটার দিন, বিনিয়োগ-ROI, ADX/ATR/RSI রেজিম ব্রেকডাউন, MAE-MFE স্ক্যাটার, সময়×বার ও মাসিক হিটম্যাপ, এবং স্লিপেজ/খরচ স্ট্রেস টেস্ট।

### 🧩 Option Selling Lab (নতুন ট্যাব "Selling")
Black-Scholes IV/Greeks, ৯টি টেমপ্লেট (Short Straddle/Strangle, Iron Condor/Butterfly, Bull Put/Bear Call, Ratio, Jade Lizard, Calendar), লেগ-SL, Basket SL/TP/Lock/Trail, Move-SL-to-Cost, Re-entry, Auto-Adjust, Auto-Hedge, IVP ফিল্টার, আনুমানিক মার্জিন/ROM, ইন্টারেক্টিভ Payoff (Expiry + T+0, ±1SD/2SD, স্লাইডার, Net Greeks)। এখনকার সংস্করণ ইন্ট্রাডে।

### Phase 1/3 বাকি অংশ
ডাইনামিক সাইজিং (ফিক্সড/কম্পাউন্ড/%-ঝুঁকি), Equity+Underwater+Spot চার্ট, টপ-৫ DD, E-ratio টাইম-ডিকে, গ্যাপ-রেজিম; SMC সোর্স (Swing, BOS/CHoCH, FVG, Inside Bar, NR7, RVOL — রিপেইন্ট-মুক্ত); Optimize-এ 2D Heatmap ও 1D স্টেবিলিটি কার্ভ; AI v2-তে ইন্ডিকেটর/থ্রেশহোল্ড সার্চ (প্ল্যাটো-যাচাই), Base-vs-Suggested ইকুইটি, Monte Carlo ফ্যান, Ablation বার; ঐচ্ছিক LLM কো-পাইলট (`~/greenbull-data/llm_key.txt` বা UI-তে Key)।
