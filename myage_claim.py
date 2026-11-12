#!/usr/bin/env python3
"""MyAge (@MyAgeApp) — auto-claim multi-akun untuk VPS. (v2)

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


def collect_tasks(obj, out, depth=0):
    if depth > 5:
        return
    if isinstance(obj, dict):
        link = obj.get("link") or obj.get("url") or obj.get("href") or ""
        typ = str(obj.get("type") or obj.get("kind") or "").lower()
        done = obj.get("done", obj.get("completed", obj.get("is_done")))
        key = obj.get("key") or obj.get("id")
        ready = obj.get("claimable", obj.get("ready"))
        claimed = obj.get("claimed", obj.get("is_claimed"))
        if isinstance(link, str) and "t.me/" in link:
            out.append({"link": link, "type": typ, "done": done,
                        "key": key, "ready": ready, "claimed": claimed})
        elif key and (ready is True or done is True) and claimed is not True:
            out.append({"link": "", "type": typ, "done": done,
                        "key": key, "ready": ready, "claimed": claimed})
        for v in obj.values():
            collect_tasks(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            collect_tasks(v, out, depth + 1)


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


async def tg_task_actions(client, label, tasks):
    """Aksi Telegram gratis untuk task: join channel publik / START bot."""
    from telethon.tl.functions.channels import JoinChannelRequest
    for t in tasks:
        if t["done"] is True or not t["link"]:
            continue
        name = t["link"].rstrip("/").split("t.me/")[-1].split("/")[0]
        if not name or name.startswith("+"):
            continue
        try:
            if "join" in t["type"] or "subscri" in t["type"] or \
               "channel" in t["type"]:
                await client(JoinChannelRequest(
                    await client.get_input_entity(name)))
                log(label, f"  · task: join @{name}")
            elif "bot" in t["type"] or "start" in t["type"] or \
                    "register" in t["type"] or "quest" in t["type"]:
                await client.send_message(name, "/start")
                log(label, f"  · task: START @{name}")
            await asyncio.sleep(random.uniform(1, 2.5))
        except Exception as e:
            log(label, f"  · task @{name}: gagal ({type(e).__name__})")


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

    st, tasks = api.get("tasks")
    if inspect:
        dump(label, "tasks", tasks)
    if st == 200:
        found = []
        collect_tasks(tasks, found)
        if client is not None and found:
            await tg_task_actions(client, label, found)
            st, tasks = api.get("tasks")
            found = []
            collect_tasks(tasks, found)
        for t in found:
            if t["key"] and t["ready"] is True and t["claimed"] is not True:
                st2, js2 = api.post("tasks", {"key": t["key"]})
                status, msg = classify(st2, js2)
                if status == "ok":
                    a, g = parse_reward(js2)
                    if a:
                        extra_age += a
                    if g:
                        extra_gram += g
                    log(label, f"  ✅ Task {t['key']}" +
                        (f" +{fmt(a)} AGE" if a else "") +
                        (f" +{fmt(g)} GRAM" if g else ""))
                else:
                    log(label, f"  {ICON[status]} Task {t['key']}: "
                               f"{msg[:60]}")
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
