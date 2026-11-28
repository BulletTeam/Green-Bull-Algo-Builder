# =====================================================================
#  হাইব্রিড LLM কো-পাইলট (ঐচ্ছিক)। LLM কোনো গণিত করে না — শুধু (ক) আইডিয়া → Case JSON, (খ) ফলাফলের ভাষ্য।
#  সব সংখ্যা ও যাচাই আমাদের ইঞ্জিনেই হয়; কী না থাকলে এই অংশ নিষ্ক্রিয়।
# =====================================================================
LLM_MODEL = "claude-sonnet-5-5"


def llm_key(given=None):
    if given:
        return str(given).strip()
    for pth in (os.environ.get("ANTHROPIC_API_KEY"), None):
        if pth:
            return pth.strip()
    try:
        d = (DATA.get("src") or {}).get("db_dir")
        if d and os.path.exists(os.path.join(d, "llm_key.txt")):
            return open(os.path.join(d, "llm_key.txt")).read().strip()
    except Exception:
        pass
    return ""


def llm_call(system, user, key, max_tokens=1600):
    import requests
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": LLM_MODEL, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}, timeout=60)
    if r.status_code != 200:
        raise ValueError("LLM ত্রুটি %d: %s" % (r.status_code, r.text[:160]))
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
    out = _extract_json(caller(system, text, key))
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



