#!/usr/bin/env python3
"""MyAge (@MyAgeApp) — auto-claim multi-akun untuk VPS. (v4)

Yang baru di v4:
- KLAIM TASK pakai SPAM-CLAIM (aturan Bos 13 Nov, meneruskan aturan
  spam-claim 12 Nov): setelah aksi task selesai, klaim ditekan berulang
  sampai 10x sampai tembus — bug app membuat klaim sering tidak
  merespons / error sesaat di percobaan awal padahal task sudah selesai.
  Berhenti dini hanya bila sukses atau server menyatakan sudah diklaim.
  Body {key} diganti {id} bila server balas 404 / 'No task'; aksi
  Telegram diulang sekali di tengah spam (server kadang belum melihat
  aksi kita — pola app: 'Open the link, then tap Claim again').

Yang baru di v3:
- Mesin TASK mengikuti alur app persis: klik task -> aksi di Telegram
  (join channel / START bot / buka postingan + reaksi 👍) -> balik ->
  claim. Parser task toleran (key/id, title/name, status string) dan
  setiap task yang belum selesai dicoba + hasilnya dicatat per task.

Cara kerja:
- Per akun: buka sesi Telegram (Telethon, StringSession per label di
  sessions/<label>.session), minta URL WebView resmi bot @MyAgeApp,
  ambil initData segar dari URL itu, lalu panggil API myageapp.xyz
  persis seperti app-nya: POST session -> vault -> daily -> spin ->
  tag -> bio -> achievements -> tasks.
- Mode initdata: bila sessions/<label>.session tidak ada tapi
  initdata/<label>.txt ada, initData dibaca dari file itu (tanpa Telethon).
- Pagar keras (allowlist): script TIDAK PERNAH menyentuh endpoint
  pass/invoice, pass/mint, code (promo), x/link, withdraw, casino,
  share/*, usdt, vip, bingx. Tidak invite, tidak ubah nama/bio
  (tag/bio di sini hanya klaim hadiah yang server verifikasi sendiri),
  tidak connect wallet, tidak bayar/sign apa pun.

Yang baru di v2:
- Total putaran dihitung dari HADIAH KLAIM yang benar-benar dibayar server
  (bukan selisih saldo — field "age" di respons session ternyata statistik
  lain, bukan saldo AGE; v1 salah membaca field itu).
- Hadiah dibedakan AGE vs GRAM (spin bisa membayar GRAM).
- Status 429 "Already taken/paid today" ditampilkan sebagai "sudah
  diambil", bukan seperti error.
- Tampilan multi-akun: header per akun + TABEL RINGKASAN di akhir putaran.

Pemakaian:
  python3 myage_claim.py --once                 # satu putaran semua akun
  python3 myage_claim.py --once --inspect       # + dump JSON mentah ke inspect/
  python3 myage_claim.py --loop --interval 9000 # jalan terus tiap 9000 detik
  python3 myage_claim.py --accounts akun1,akun3 # batasi label akun

Login per akun (sekali saja, di VPS ini): python3 login.py <label>
Nilai sesi / initData TIDAK pernah dicetak ke log.
"""
import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import parse_qs, urlparse

BASE = "https://myageapp.xyz"
BOT_USERNAME = "MyAgeApp"
API_ID = int(os.environ.get("MYAGE_API_ID", "2040"))
API_HASH = os.environ.get("MYAGE_API_HASH", "b18441a1ff607e10a989891a5462e627")

HERE = os.path.dirname(os.path.abspath(__file__))
SESS_DIR = os.path.join(HERE, "sessions")
INIT_DIR = os.path.join(HERE, "initdata")
INSPECT_DIR = os.path.join(HERE, "inspect")
LOG_PATH = os.path.join(HERE, "runs.log")

ALLOWED_POST = {"session", "vault", "daily", "spin", "tag", "bio",
                "achievements", "tasks"}
ALLOWED_GET = {"achievements", "tasks", "pass", "friends", "wallet",
               "board", "league", "channels", "feed", "vault-info"}

CLAIM_TRIES = 10           # spam-claim task: maks percobaan klaim (aturan Bos)
CLAIM_DELAY = (1.0, 2.0)   # jeda antar percobaan klaim (detik, acak)


def log(label, msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{label}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def banner(msg):
    log("=", msg)


def _loads(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw[:500]}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def find_number(obj, names, depth=0):
    """Cari angka pertama untuk salah satu nama kunci (rekursif, dangkal)."""
    if depth > 4:
        return None
    if isinstance(obj, dict):
        for n in names:
            v = obj.get(n)
            if _num(v):
                return v
        for v in obj.values():
            r = find_number(v, names, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_number(v, names, depth + 1)
            if r is not None:
                return r
    return None


def find_string(obj, names, depth=0):
    if depth > 4:
        return None
    if isinstance(obj, dict):
        for n in names:
            v = obj.get(n)
            if isinstance(v, str) and v:
                return v
        for v in obj.values():
            r = find_string(v, names, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_string(v, names, depth + 1)
            if r is not None:
                return r
    return None


def resp_message(js):
    if isinstance(js, dict):
        return str(js.get("error") or js.get("message") or
                   js.get("reason") or js.get("detail") or "")
    return ""


def parse_reward(js):
    """Ekstrak hadiah dari respons klaim -> (age, gram).

    Respons klaim terbukti memuat angka hadiah (amount/claimed/reward/...);
    mata uangnya AGE kecuali respons menyebut GRAM (spin bisa bayar GRAM).
    """
    amt = find_number(js, ("amount", "claimed", "reward", "earned",
                           "bonus", "value", "prize"))
    if amt is not None:
        cur = find_string(js, ("currency", "unit", "token", "denom",
                               "asset"))
        if cur and "gram" in cur.lower():
            return None, amt
        if cur and "age" in cur.lower():
            return amt, None
        # Tanpa label mata uang: tebak dari kunci telanjang yang ikut ada.
        g = find_number(js, ("gram",))
        a = find_number(js, ("age",))
        if g is not None and a is None and amt == g:
            return None, amt
        return amt, None
    # Tidak ada amount generik: mungkin pasangan age/gram langsung.
    return find_number(js, ("age",)), find_number(js, ("gram",))


def classify(st, js):
    """-> (status, pesan_singkat). status: ok|already|warn|error"""
    msg = resp_message(js)
    low = msg.lower()
    if st == 200:
        return "ok", msg
    if "already" in low or "taken today" in low or "paid today" in low:
        return "already", msg
    if st == 429:
        return "already", msg or "sudah diambil hari ini"
    if st == 400:
        return "warn", msg
    if st == 0:
        return "error", msg or "jaringan/API tidak menjawab"
    return "error", msg


AGE_KEYS = ("age", "age_balance", "balance_age", "age_amount")
GRAM_KEYS = ("gram", "gram_balance", "balance_gram", "gram_amount")


def find_balances(state):
    """Cari pasangan saldo (age, gram) di respons session.

    v1 salah ambil: kunci "age" pertama di JSON ternyata statistik lain
    (50 / 37,5 / 25 — bukan saldo). v2 mengumpulkan semua dict yang memuat
    kunci age DAN gram sekaligus, lalu memilih yang paling mungkin saldo:
    induk bernama balance/wallet diutamakan, lalu nilai age terbesar.
    Mengembalikan (age|None, gram|None).
    """
    cands = []

    def walk(obj, depth, parent):
        if depth > 5:
            return
        if isinstance(obj, dict):
            age = gram = None
            for k, v in obj.items():
                if not _num(v):
                    continue
                lk = str(k).lower()
                if lk in AGE_KEYS:
                    age = v
                elif lk in GRAM_KEYS:
                    gram = v
            if age is not None and gram is not None:
                score = 0
                if "balance" in parent or "wallet" in parent:
                    score += 10
                cands.append((score, age, gram))
            for k, v in obj.items():
                walk(v, depth + 1, str(k).lower())
        elif isinstance(obj, list):
            for v in obj:
                walk(v, depth, parent)

    walk(state, 0, "")
    if cands:
        cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
        return cands[0][1], cands[0][2]
    return None, find_number(state, GRAM_KEYS)


def fmt(v):
    """Format angka gaya ID: 113.116 / 2.500 / 0,065 / — untuk None."""
    if v is None:
        return "—"
    if isinstance(v, float) and v != int(v):
        s = f"{v:.3f}".rstrip("0").rstrip(".")
        return s.replace(".", ",")
    return f"{int(round(v)):,}".replace(",", ".")


class Api:
    def __init__(self, init_data):
        self.init = init_data

    def _req(self, method, path, body=None):
        url = BASE + "/api/" + path
        headers = {"authorization": "tma " + self.init,
                   "accept": "application/json",
                   "user-agent": "Mozilla/5.0 (Linux; Android 13) "
                                 "AppleWebKit/537.36 Telegram-Android"}
        data = None
        if method == "POST":
            data = json.dumps(body or {}).encode()
            headers["content-type"] = "application/json"
            headers["idempotency-key"] = str(uuid.uuid4())
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, _loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return e.code, _loads(e.read().decode("utf-8", "replace"))
        except Exception as e:  # jaringan putus dll -> status 0
            return 0, {"error": str(e)}

    def post(self, path, body=None, _retry=True):
        if path not in ALLOWED_POST:
            raise ValueError(f"endpoint POST di luar allowlist: {path}")
        st, js = self._req("POST", path, body)
        if st == 409 and _retry:
            d = js.get("data") if isinstance(js, dict) else None
            if (isinstance(js, dict) and js.get("reopen")) or \
               (isinstance(d, dict) and d.get("reopen")):
                self.post("session", {}, _retry=False)
                return self.post(path, body, _retry=False)
        return st, js

    def get(self, path):
        if path not in ALLOWED_GET:
            raise ValueError(f"endpoint GET di luar allowlist: {path}")
        return self._req("GET", path)


ICON = {"ok": "✅", "already": "⏭", "warn": "⚠️", "error": "❌"}


def do_claim(api, label, path, note, res):
    """Satu klaim sederhana; catat hasil ke res[note] dan ke total akun."""
    st, js = api.post(path, {})
    status, msg = classify(st, js)
    age = gram = None
    if status == "ok":
        age, gram = parse_reward(js)
    res["items"][note] = (status, age, gram)
    if age:
        res["age"] += age
    if gram:
        res["gram"] += gram
    if status == "ok":
        parts = []
        if age:
            parts.append(f"+{fmt(age)} AGE")
        if gram:
            parts.append(f"+{fmt(gram)} GRAM")
        log(label, f"  ✅ {note:<14} {' · '.join(parts) or 'OK'}")
    elif status == "already":
        log(label, f"  ⏭ {note:<14} sudah diambil hari ini")
    elif status == "warn":
        log(label, f"  ⚠️ {note:<14} {msg[:90]}")
    else:
        log(label, f"  ❌ {note:<14} status {st} {msg[:90]}".rstrip())
    return st, js


def collect_achievement_keys(obj, out, depth=0):
    if depth > 5:
        return
    if isinstance(obj, dict):
        key = obj.get("key") or obj.get("id")
        claimed = obj.get("claimed", obj.get("done", obj.get("is_claimed")))
        ready = obj.get("claimable", obj.get("ready",
                        obj.get("unlocked", obj.get("achieved",
                        obj.get("completed")))))
        if isinstance(key, str) and claimed is not True and ready is True:
            out.add(key)
        for v in obj.values():
            collect_achievement_keys(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            collect_achievement_keys(v, out, depth + 1)


def _status_flags(d):
    s = str(d.get("status") or d.get("state") or "").lower()
    claimed = d.get("claimed", d.get("is_claimed"))
    done = d.get("done", d.get("completed", d.get("is_done")))
    ready = d.get("claimable", d.get("ready"))
    if s in ("claimed", "done", "completed", "finished", "paid"):
        claimed = True
    if s in ("done", "completed"):
        done = True
    if s in ("ready", "claimable", "check", "claim"):
        ready = True
    return done, claimed, ready


def parse_tasks(obj, out, seen, depth=0):
    """Kumpulkan task dari JSON tasks (toleran bentuk: key/id, title/name,
    link/url, status string atau boolean, bersarang di steps/group)."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        link = obj.get("link") or obj.get("url") or obj.get("href") or ""
        key = obj.get("key") or obj.get("id") or obj.get("task_id") or \
            obj.get("slug")
        title = obj.get("title") or obj.get("name") or obj.get("text") or \
            obj.get("label") or ""
        if (isinstance(link, str) and link) or (key and title):
            sig = (str(key), str(link), str(title))
            if sig not in seen:
                seen.add(sig)
                done, claimed, ready = _status_flags(obj)
                out.append({
                    "key": key, "title": str(title)[:60],
                    "link": link if isinstance(link, str) else "",
                    "type": str(obj.get("type") or obj.get("kind") or
                                obj.get("action") or "").lower(),
                    "done": done, "claimed": claimed, "ready": ready})
        for v in obj.values():
            parse_tasks(v, out, seen, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            parse_tasks(v, out, seen, depth + 1)


def task_kind(t):
    if re.match(r"https?://t\.me/[^/]+/\d+", t["link"] or ""):
        return "post"
    typ, title = t["type"], t["title"].lower()
    if "like" in typ or "react" in typ or "like" in title or \
            "share" in title:
        return "post"
    if "bot" in typ or "register" in typ or "quest" in typ or \
            "register" in title or "start" in title:
        return "bot"
    return "join"


async def get_init_data(client):
    bot = await client.get_input_entity(BOT_USERNAME)
    url = None
    try:
        from telethon.tl.functions.messages import RequestMainWebViewRequest
        res = await client(RequestMainWebViewRequest(
            peer=bot, bot=bot, platform="android"))
        url = res.url
    except Exception:
        from telethon.tl.functions.messages import RequestWebViewRequest
        res = await client(RequestWebViewRequest(
            peer=bot, bot=bot, platform="android", from_bot_menu=True))
        url = res.url
    frag = urlparse(url).fragment
    data = parse_qs(frag).get("tgWebAppData", [None])[0]
    if not data:
        raise RuntimeError("tgWebAppData tidak ada di URL WebView")
    return data


async def tg_action(client, label, t):
    """Kerjakan aksi Telegram satu task (langkah 'klik task' di app):
    join channel / START bot / buka postingan + reaksi. True bila dicoba."""
    from telethon.tl.functions.channels import JoinChannelRequest
    link = t["link"]
    kind = task_kind(t)
    title = t["title"] or str(t["key"])
    try:
        if "+join" in link or "joinchat" in link or \
                re.search(r"t\.me/\+", link):
            from telethon.tl.functions.messages import \
                ImportChatInviteRequest
            h = link.rstrip("/").split("/")[-1].lstrip("+")
            try:
                await client(ImportChatInviteRequest(h))
            except Exception as e:
                if "already" not in type(e).__name__.lower():
                    raise
            log(label, f"  · task '{title}': join via invite")
            return True
        m = re.match(r"https?://t\.me/([^/?#]+)(?:/(\d+))?", link)
        if not m:
            return False
        name, post_id = m.group(1), m.group(2)
        entity = await client.get_input_entity(name)
        if kind == "bot" and not post_id:
            q = parse_qs(urlparse(link).query)
            start = q.get("start", [None])[0] or \
                q.get("startapp", [None])[0]
            await client.send_message(
                entity, f"/start {start}" if start else "/start")
            log(label, f"  · task '{title}': START @{name}")
            return True
        try:
            await client(JoinChannelRequest(entity))
            log(label, f"  · task '{title}': join @{name}")
        except Exception as e:
            if "already" not in type(e).__name__.lower():
                log(label, f"  · task '{title}': join @{name} gagal "
                           f"({type(e).__name__})")
        if post_id:
            try:
                await client.get_messages(entity, ids=int(post_id))
                try:
                    await client.send_reaction(entity, int(post_id), "👍")
                except Exception:
                    from telethon.tl.functions.messages import \
                        SendReactionRequest
                    from telethon.tl.types import ReactionEmoji
                    await client(SendReactionRequest(
                        peer=entity, msg_id=int(post_id),
                        reaction=[ReactionEmoji(emoticon="👍")]))
                log(label, f"  · task '{title}': buka post + reaksi "
                           f"👍 @{name}/{post_id}")
            except Exception as e:
                log(label, f"  · task '{title}': buka post gagal "
                           f"({type(e).__name__})")
        return True
    except Exception as e:
        log(label, f"  · task '{title}': aksi gagal ({type(e).__name__})")
        return False


async def claim_task(api, client, label, t):
    """Klaim hadiah SATU task dengan SPAM-CLAIM (aturan Bos): tekan klaim
    berulang sampai CLAIM_TRIES (10) kali sampai tembus — bug app membuat
    klaim sering tidak merespons / error sesaat di percobaan awal padahal
    aksi task sudah selesai. Berhenti dini hanya bila sukses atau server
    menyatakan SUDAH diklaim. Bila server balas 404 / 'No task', bentuk
    body diganti {key} -> {id} untuk sisa percobaan. Aksi Telegram
    diulang sekali di tengah spam (percobaan ke-4) untuk kasus server
    belum melihat aksi kita. -> (age, gram) yang berhasil diklaim."""
    name = t["title"] or str(t["key"]) or t["link"]
    body = {"key": t["key"]}
    last_st, last_status, last_msg = 0, "error", ""
    for attempt in range(1, CLAIM_TRIES + 1):
        st2, js2 = api.post("tasks", body)
        status, msg = classify(st2, js2)
        if status == "ok":
            a, g = parse_reward(js2)
            log(label, f"  ✅ Task '{name}'" +
                (f" +{fmt(a)} AGE" if a else "") +
                (f" +{fmt(g)} GRAM" if g else "") +
                (f" (klaim tembus percobaan ke-{attempt})"
                 if attempt > 1 else ""))
            return a or 0.0, g or 0.0
        if status == "already":
            log(label, f"  ⏭ Task '{name}': sudah diklaim")
            return 0.0, 0.0
        last_st, last_status, last_msg = st2, status, msg
        if (st2 == 404 or "no task" in msg.lower()) and "id" not in body:
            body = {"id": t["key"]}
        if attempt == 4 and client is not None and t["link"]:
            await tg_action(client, label, t)
        if attempt < CLAIM_TRIES:
            await asyncio.sleep(random.uniform(*CLAIM_DELAY))
    log(label, f"  {ICON[last_status]} Task '{name}': klaim gagal "
               f"{CLAIM_TRIES}x (terakhir status {last_st} "
               f"{last_msg[:70]})".rstrip())
    return 0.0, 0.0


async def handle_tasks(api, client, label, inspect):
    """Alur task persis app: klik task -> aksi di Telegram -> balik ->
    claim. Mengembalikan (age, gram) dari task yang berhasil diklaim."""
    tot_age = tot_gram = 0.0
    st, js = api.get("tasks")
    if inspect:
        dump(label, "tasks", js)
    if st != 200:
        log(label, f"  · tasks: GET status {st} — dilewati")
        return tot_age, tot_gram
    found, seen = [], set()
    parse_tasks(js, found, seen)
    open_tasks = [t for t in found
                  if t["claimed"] is not True and t["done"] is not True]
    log(label, f"  · tasks: {len(found)} ditemukan, "
               f"{len(open_tasks)} belum selesai")
    for t in open_tasks:
        name = t["title"] or str(t["key"]) or t["link"]
        if t["link"] and "t.me/" not in t["link"]:
            log(label, f"  ⚠️ Task '{name}': link luar Telegram — dilewati")
            continue
        if client is not None and t["link"]:
            if await tg_action(client, label, t):
                await asyncio.sleep(random.uniform(2, 4))
        if not t["key"]:
            log(label, f"  · Task '{name}': tanpa key — tidak bisa "
                       f"claim via API")
            continue
        a, g = await claim_task(api, client, label, t)
        tot_age += a
        tot_gram += g
    return tot_age, tot_gram


async def run_claims(label, api, client, inspect):
    res = {"label": label, "user": "", "items": {}, "age": 0.0,
           "gram": 0.0, "saldo_age": None, "saldo_gram": None,
           "skipped": ""}
    st, state = api.post("session", {})
    if st != 200:
        log(label, f"  ❌ session gagal: status {st} — akun dilewati")
        res["skipped"] = f"session status {st}"
        return res
    age0, gram0 = find_balances(state)
    res["saldo_age"], res["saldo_gram"] = age0, gram0
    if inspect:
        dump(label, "session", state)
    if age0 is not None or gram0 is not None:
        log(label, f"  saldo awal: AGE={fmt(age0)} GRAM={fmt(gram0)}")

    for path, note in (("vault", "Vault"), ("daily", "Daily"),
                       ("spin", "Spin"), ("tag", "Name tag"),
                       ("bio", "Link bio")):
        do_claim(api, label, path, note, res)
        time.sleep(random.uniform(1, 2))

    extra_age = extra_gram = 0.0
    st, ach = api.get("achievements")
    if inspect:
        dump(label, "achievements", ach)
    if st == 200:
        keys = set()
        collect_achievement_keys(ach, keys)
        for k in sorted(keys):
            time.sleep(random.uniform(0.7, 1.5))
            st2, js2 = api.post("achievements", {"key": k})
            status, msg = classify(st2, js2)
            if status == "ok":
                a, g = parse_reward(js2)
                if a:
                    extra_age += a
                if g:
                    extra_gram += g
                log(label, f"  ✅ Achievement {k}" +
                    (f" +{fmt(a)} AGE" if a else "") +
                    (f" +{fmt(g)} GRAM" if g else ""))
            else:
                log(label, f"  {ICON[status]} Achievement {k}: "
                           f"{msg[:60]}")

    t_age, t_gram = await handle_tasks(api, client, label, inspect)
    extra_age += t_age
    extra_gram += t_gram
    if extra_age or extra_gram:
        res["items"]["Lain"] = ("ok", extra_age or None,
                                extra_gram or None)
        res["age"] += extra_age
        res["gram"] += extra_gram

    st, state2 = api.post("session", {})
    if st == 200:
        age1, gram1 = find_balances(state2)
        log(label, f"  saldo akhir: AGE={fmt(age1)} GRAM={fmt(gram1)}")
    log(label, f"  Subtotal: +{fmt(res['age'])} AGE · "
               f"+{fmt(res['gram'])} GRAM")
    return res


def dump(label, name, obj):
    try:
        os.makedirs(INSPECT_DIR, exist_ok=True)
        with open(os.path.join(INSPECT_DIR, f"{label}_{name}.json"),
                  "w") as f:
            json.dump(obj, f, indent=1, default=str)
    except Exception:
        pass


async def process_account(label, args):
    sess_path = os.path.join(SESS_DIR, f"{label}.session")
    init_path = os.path.join(INIT_DIR, f"{label}.txt")
    client = None
    res = {"label": label, "user": "", "items": {}, "age": 0.0,
           "gram": 0.0, "saldo_age": None, "saldo_gram": None,
           "skipped": ""}
    try:
        if os.path.exists(sess_path):
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            with open(sess_path) as f:
                sess = f.read().strip()
            client = TelegramClient(StringSession(sess), API_ID, API_HASH,
                                    connection_retries=1, retry_delay=1,
                                    timeout=15)
            await client.connect()
            if not await client.is_user_authorized():
                log(label, "SESI MATI — login ulang: "
                           f"python3 login.py {label}")
                res["skipped"] = "sesi mati"
                await client.disconnect()
                return res
            me = await client.get_me()
            res["user"] = "@" + (me.username or "?")
            log(label, f"── {res['user']} ── login OK, ambil initData segar")
            init_data = await get_init_data(client)
        elif os.path.exists(init_path):
            with open(init_path) as f:
                init_data = f.read().strip()
            log(label, "mode initData dari file (tanpa Telethon)")
        else:
            log(label, "tidak ada sesi/initdata — dilewati "
                       f"(login dulu: python3 login.py {label})")
            res["skipped"] = "belum login"
            return res
        api = Api(init_data)
        r = await run_claims(label, api, client, args.inspect)
        r["user"] = res["user"]
        return r
    except Exception as e:
        log(label, f"ERROR {type(e).__name__}: {str(e)[:160]}")
        res["skipped"] = f"error {type(e).__name__}"
        return res
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


def cell(item):
    if item is None:
        return "—"
    status, age, gram = item
    if status == "ok":
        parts = []
        if age:
            parts.append(f"+{fmt(age)}")
        if gram:
            parts.append(f"+{fmt(gram)}g")
        return " ".join(parts) or "OK"
    return {"already": "⏭", "warn": "⚠️", "error": "❌"}.get(status, "?")


def print_summary(results):
    if not results:
        return
    cols = ["Vault", "Daily", "Spin", "Name tag", "Link bio", "Lain"]
    hdr = (f"{'Akun':<8}{'User':<16}" +
           "".join(f"{c:>9}" for c in cols) +
           f"{'AGE':>10}{'GRAM':>9}")
    banner("═" * len(hdr))
    banner("RINGKASAN PUTARAN")
    banner(hdr)
    banner("─" * len(hdr))
    tot_age = tot_gram = 0.0
    for r in results:
        if r["skipped"]:
            banner(f"{r['label']:<8}{r['user'] or '—':<16}"
                   f" dilewati ({r['skipped']})")
            continue
        row = (f"{r['label']:<8}{(r['user'] or '—'):<16}" +
               "".join(f"{cell(r['items'].get(c)):>9}" for c in cols) +
               f"{'+' + fmt(r['age']):>10}{'+' + fmt(r['gram']):>9}")
        banner(row)
        tot_age += r["age"]
        tot_gram += r["gram"]
    banner("─" * len(hdr))
    banner(f"{'TOTAL':<24}{'':>{9 * len(cols)}}"
           f"{'+' + fmt(tot_age):>10}{'+' + fmt(tot_gram):>9}")
    banner("═" * len(hdr))


def list_labels(only=None):
    labels = set()
    for d in (SESS_DIR, INIT_DIR):
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if fn.endswith(".session"):
                    labels.add(fn[:-len(".session")])
                elif fn.endswith(".txt"):
                    labels.add(fn[:-len(".txt")])
    if only:
        want = [x.strip() for x in only.split(",") if x.strip()]
        return want
    return sorted(labels)


async def one_round(args):
    labels = list_labels(args.accounts)
    if not labels:
        print("Tidak ada akun (folder sessions/ dan initdata/ kosong).")
        return
    banner("🥔 MyAge auto-claim — putaran " +
           time.strftime("%Y-%m-%d %H:%M:%S") +
           f" — {len(labels)} akun")
    results = []
    for i, label in enumerate(labels):
        if i:
            await asyncio.sleep(random.uniform(5, 12))
        results.append(await process_account(label, args))
    print_summary(results)


def main():
    ap = argparse.ArgumentParser(description="MyAge auto-claim multi-akun")
    ap.add_argument("--once", action="store_true",
                    help="satu putaran lalu selesai (default)")
    ap.add_argument("--loop", action="store_true",
                    help="jalan terus, ulangi tiap --interval detik")
    ap.add_argument("--interval", type=int, default=9000,
                    help="jeda antar putaran dalam detik (default 9000)")
    ap.add_argument("--accounts", default=None,
                    help="label akun dipisah koma (default: semua)")
    ap.add_argument("--inspect", action="store_true",
                    help="dump JSON mentah session/tasks/achievements "
                         "ke folder inspect/")
    args = ap.parse_args()
    while True:
        asyncio.run(one_round(args))
        if not args.loop:
            break
        nxt = time.strftime("%H:%M:%S", time.localtime(
            time.time() + args.interval))
        print(f"Putaran berikut ±{nxt} (interval {args.interval} dtk)",
              flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
