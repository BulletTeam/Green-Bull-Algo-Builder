# =====================================================================
#  StrategyVault — আপডেট-নিরাপদ স্থায়ী স্ট্র্যাটেজি-ভল্ট (কোড-ফোল্ডারের বাইরে)
#  অবস্থান: $GB_VAULT_DIR → <db_dir>/vault → ~/.greenbull_vault
#  গঠন: folders.json, strategies/st_<uuid>.json, trash/st_<uuid>.json
# =====================================================================
VAULT_SCHEMA = 2
GENERAL_FOLDER = "সাধারণ"
LEGACY_FOLDER = "পুরনো প্রিসেট"
DEFAULT_FOLDERS = [GENERAL_FOLDER, "Option Buying", "Option Selling", "Live Ready"]


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, path)


class StrategyVault:
    def __init__(self, root=None):
        self.lock = threading.RLock()
        self._root = root

    # ---- অবস্থান ----
    def root(self):
        if self._root:
            r = self._root
        elif os.environ.get("GB_VAULT_DIR"):
            r = os.environ["GB_VAULT_DIR"]
        else:
            d = (DATA.get("src") or {}).get("db_dir")
            r = os.path.join(d, "vault") if d else os.path.join(os.path.expanduser("~"), ".greenbull_vault")
        try:
            os.makedirs(os.path.join(r, "strategies"), exist_ok=True)
            os.makedirs(os.path.join(r, "trash"), exist_ok=True)
        except Exception:
            r = os.path.join(os.path.expanduser("~"), ".greenbull_vault")
            os.makedirs(os.path.join(r, "strategies"), exist_ok=True)
            os.makedirs(os.path.join(r, "trash"), exist_ok=True)
        return r

    def _p(self, *a):
        return os.path.join(self.root(), *a)

    # ---- ফোল্ডার ----
    def _folders_raw(self):
        fp = self._p("folders.json")
        seeded = False
        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
            fl = [str(x) for x in d.get("folders", [])]
            seeded = True
        except Exception:
            fl = []
        if not seeded:                     # ডিফল্ট ফোল্ডার শুধু প্রথমবার বসে (পরে নাম বদলালে/মুছলে ফিরে আসে না)
            for n in DEFAULT_FOLDERS:
                if n not in fl:
                    fl.append(n)
        if GENERAL_FOLDER in fl:
            fl.remove(GENERAL_FOLDER)
        fl.insert(0, GENERAL_FOLDER)
        return fl

    def _save_folders(self, fl):
        _atomic_write(self._p("folders.json"), {"schema_ver": VAULT_SCHEMA, "folders": fl})

    def folders(self):
        with self.lock:
            self._legacy_import()
            return self._folders_raw()

    def folder_create(self, name):
        name = str(name or "").strip()
        if not name:
            raise ValueError("ফোল্ডারের নাম দিন")
        with self.lock:
            fl = self._folders_raw()
            if name in fl:
                raise ValueError("এই নামে ফোল্ডার আছে")
            fl.append(name)
            self._save_folders(fl)
        return fl

    def folder_rename(self, old, new):
        new = str(new or "").strip()
        with self.lock:
            fl = self._folders_raw()
            if old == GENERAL_FOLDER:
                raise ValueError("'সাধারণ' ফোল্ডারের নাম বদলানো যায় না")
            if old not in fl or not new or (new in fl and new != old):
                raise ValueError("নাম বদলানো গেল না")
            fl[fl.index(old)] = new
            self._save_folders(fl)
            for sub in ("strategies", "trash"):
                for fn in os.listdir(self._p(sub)):
                    if fn.endswith(".json"):
                        rec = self._read(os.path.join(self._p(sub), fn))
                        if rec and rec.get("folder") == old:
                            rec["folder"] = new
                            _atomic_write(os.path.join(self._p(sub), fn), rec)
        return fl

    def folder_delete(self, name):
        with self.lock:
            fl = self._folders_raw()
            if name == GENERAL_FOLDER:
                raise ValueError("'সাধারণ' ফোল্ডার মোছা যায় না")
            if name not in fl:
                raise ValueError("ফোল্ডার নেই")
            moved = 0
            for sub in ("strategies", "trash"):
                for fn in os.listdir(self._p(sub)):
                    if fn.endswith(".json"):
                        rec = self._read(os.path.join(self._p(sub), fn))
                        if rec and rec.get("folder") == name:
                            rec["folder"] = GENERAL_FOLDER          # কখনো স্ট্র্যাটেজি মোছা হয় না
                            _atomic_write(os.path.join(self._p(sub), fn), rec)
                            moved += 1
            fl.remove(name)
            self._save_folders(fl)
        return {"folders": fl, "moved": moved}

    # ---- স্ট্র্যাটেজি ----
    def _read(self, fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            return None
        return self._migrate(rec)

    def _migrate(self, rec):
        if not isinstance(rec, dict) or "settings" not in rec:
            return None
        if int(rec.get("schema_ver", 1)) < VAULT_SCHEMA:
            try:
                rec["settings"] = norm_settings(rec["settings"]) if rec["settings"].get("cases") else rec["settings"]
            except Exception:
                pass
            rec["schema_ver"] = VAULT_SCHEMA
        rec.setdefault("folder", GENERAL_FOLDER)
        rec.setdefault("pinned", False)
        rec.setdefault("note", "")
        return rec

    def _path(self, sid, trash=False):
        sid = os.path.basename(str(sid))
        if not sid.startswith("st_"):
            sid = "st_" + sid
        return os.path.join(self._p("trash" if trash else "strategies"), sid + ".json")

    def _new_rec(self, name, settings, folder, summary, note):
        now = dt.datetime.now().isoformat(timespec="seconds")
        fl = self._folders_raw()
        return {"schema_ver": VAULT_SCHEMA, "id": uuid.uuid4().hex[:12], "name": str(name).strip(), "folder": folder if folder in fl else GENERAL_FOLDER,
                "pinned": False, "note": str(note or ""), "created": now, "updated": now, "settings": settings, "summary": summary or None}

    def save_new(self, name, settings, folder=None, summary=None, note=""):
        if not str(name or "").strip():
            raise ValueError("স্ট্র্যাটেজির নাম দিন")
        with self.lock:
            self._legacy_import()
            rec = self._new_rec(name, settings, folder or GENERAL_FOLDER, summary, note)
            _atomic_write(self._path(rec["id"]), rec)
        return rec["id"]

    def get(self, sid, trash=False):
        with self.lock:
            rec = self._read(self._path(sid, trash))
        if not rec:
            raise ValueError("স্ট্র্যাটেজি পাওয়া যায়নি")
        return rec

    def update(self, sid, settings=None, summary=None, name=None, note=None):
        with self.lock:
            rec = self.get(sid)
            if settings is not None:
                rec["settings"] = settings
            if summary is not None:
                rec["summary"] = summary
            if name:
                rec["name"] = str(name).strip()
            if note is not None:
                rec["note"] = str(note)
            rec["updated"] = dt.datetime.now().isoformat(timespec="seconds")
            _atomic_write(self._path(sid), rec)
        return rec["id"]

    def duplicate(self, sid):
        with self.lock:
            rec = self.get(sid)
            new = self._new_rec(rec["name"] + " (কপি)", copy.deepcopy(rec["settings"]), rec["folder"], copy.deepcopy(rec.get("summary")), rec.get("note"))
            _atomic_write(self._path(new["id"]), new)
        return new["id"]

    def move(self, sid, folder):
        with self.lock:
            if folder not in self._folders_raw():
                raise ValueError("ফোল্ডার নেই")
            rec = self.get(sid)
            rec["folder"] = folder
            _atomic_write(self._path(sid), rec)

    def pin(self, sid, on):
        with self.lock:
            rec = self.get(sid)
            rec["pinned"] = bool(on)
            _atomic_write(self._path(sid), rec)

    def delete(self, sid):
        """সফট ডিলিট: trash/-এ সরানো"""
        with self.lock:
            rec = self.get(sid)
            rec["deleted"] = dt.datetime.now().isoformat(timespec="seconds")
            _atomic_write(self._path(sid, True), rec)
            os.remove(self._path(sid))

    def restore(self, sid):
        with self.lock:
            rec = self.get(sid, True)
            rec.pop("deleted", None)
            if rec.get("folder") not in self._folders_raw():
                rec["folder"] = GENERAL_FOLDER
            _atomic_write(self._path(sid), rec)
            os.remove(self._path(sid, True))

    def purge(self, sid):
        """স্থায়ী মোছা — শুধু ট্র্যাশ থেকে"""
        with self.lock:
            fp = self._path(sid, True)
            if not os.path.exists(fp):
                raise ValueError("শুধু ট্র্যাশের স্ট্র্যাটেজি স্থায়ীভাবে মোছা যায়")
            os.remove(fp)

    def list(self, trash=False, with_settings=False):
        out = []
        with self.lock:
            self._legacy_import()
            d = self._p("trash" if trash else "strategies")
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".json"):
                    continue
                rec = self._read(os.path.join(d, fn))
                if rec:
                    if not with_settings:
                        rec = {k: v for k, v in rec.items() if k != "settings"}
                        rec["legs"] = None
                    out.append(rec)
        return out

    # ---- পুরনো presets.json আমদানি (একবার, মূল ফাইল না মুছে) ----
    def _legacy_import(self):
        mk = self._p(".presets_migrated")
        if os.path.exists(mk) or getattr(self, "_importing", False):
            return
        self._importing = True
        try:
            legacy = load_user_presets()
            n = 0
            if legacy:
                fl = self._folders_raw()
                if LEGACY_FOLDER not in fl:
                    fl.append(LEGACY_FOLDER)
                    self._save_folders(fl)
                for name, st in legacy.items():
                    meta = (st or {}).get("_meta") or {}
                    rec = self._new_rec(name, st, LEGACY_FOLDER, meta.get("stats"), meta.get("note", ""))
                    _atomic_write(self._path(rec["id"]), rec)
                    n += 1
            _atomic_write(mk, {"migrated": n, "at": dt.datetime.now().isoformat(timespec="seconds")})
        except Exception:
            traceback.print_exc()
        finally:
            self._importing = False


VAULT = StrategyVault()


def vault_api(op, b):
    V = VAULT
    if op == "list":
        return {"folders": V.folders(), "items": V.list(), "trash": len(V.list(trash=True)), "dir": V.root()}
    if op == "trash_list":
        return {"items": V.list(trash=True)}
    if op == "get":
        return V.get(b["id"], bool(b.get("trash")))
    if op == "save":
        sid = V.save_new(b.get("name"), b["settings"], b.get("folder"), b.get("summary"), b.get("note", ""))
        return {"id": sid, **vault_api("list", {})}
    if op == "update":
        V.update(b["id"], b.get("settings"), b.get("summary"), b.get("name"), b.get("note"))
    elif op == "duplicate":
        return {"id": V.duplicate(b["id"]), **vault_api("list", {})}
    elif op == "move":
        V.move(b["id"], b.get("folder"))
    elif op == "pin":
        V.pin(b["id"], b.get("on", True))
    elif op == "delete":
        V.delete(b["id"])
    elif op == "restore":
        V.restore(b["id"])
    elif op == "purge":
        V.purge(b["id"])
    elif op == "folder_create":
        V.folder_create(b.get("name"))
    elif op == "folder_rename":
        V.folder_rename(b.get("old"), b.get("new"))
    elif op == "folder_delete":
        V.folder_delete(b.get("name"))
    else:
        raise ValueError("অজানা ভল্ট-অপারেশন")
    return vault_api("list", {})
