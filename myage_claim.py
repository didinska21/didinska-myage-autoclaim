#!/usr/bin/env python3
"""MyAge (@MyAgeApp) — auto-claim multi-akun untuk VPS.

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


def _loads(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw[:500]}


def find_number(obj, names, depth=0):
    """Cari angka pertama untuk salah satu nama kunci (rekursif, dangkal)."""
    if depth > 4:
        return None
    if isinstance(obj, dict):
        for n in names:
            v = obj.get(n)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
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


def balances(state):
    age = find_number(state, ("age", "AGE", "age_balance", "balance_age"))
    gram = find_number(state, ("gram", "GRAM", "gram_balance", "balance_gram"))
    return age, gram


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


def claim_simple(api, label, path, note):
    st, js = api.post(path, {})
    amt = find_number(js, ("amount", "claimed", "reward", "earned", "bonus",
                           "value"))
    if st == 200:
        log(label, f"{note}: OK" + (f" +{amt}" if amt is not None else ""))
    else:
        err = ""
        if isinstance(js, dict):
            err = str(js.get("error") or js.get("message") or
                      js.get("reason") or "")[:120]
        log(label, f"{note}: status {st} {err}".rstrip())
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
                log(label, f"task: join @{name}")
            elif "bot" in t["type"] or "start" in t["type"] or \
                    "register" in t["type"] or "quest" in t["type"]:
                await client.send_message(name, "/start")
                log(label, f"task: START @{name}")
            await asyncio.sleep(random.uniform(1, 2.5))
        except Exception as e:
            log(label, f"task @{name}: gagal ({type(e).__name__})")


async def run_claims(label, api, client, inspect):
    st, state = api.post("session", {})
    if st != 200:
        log(label, f"session gagal: status {st} — akun dilewati putaran ini")
        return
    age0, gram0 = balances(state)
    log(label, f"saldo awal: AGE={age0} GRAM={gram0}")
    if inspect:
        dump(label, "session", state)

    claim_simple(api, label, "vault", "Vault")
    time.sleep(random.uniform(1, 2))
    claim_simple(api, label, "daily", "Daily reward")
    time.sleep(random.uniform(1, 2))
    claim_simple(api, label, "spin", "Spin reguler")
    time.sleep(random.uniform(1, 2))
    claim_simple(api, label, "tag", "Name tag harian")
    time.sleep(random.uniform(1, 2))
    claim_simple(api, label, "bio", "Link bio harian")

    st, ach = api.get("achievements")
    if inspect:
        dump(label, "achievements", ach)
    if st == 200:
        keys = set()
        collect_achievement_keys(ach, keys)
        for k in sorted(keys):
            time.sleep(random.uniform(0.7, 1.5))
            st2, js2 = api.post("achievements", {"key": k})
            amt = find_number(js2, ("amount", "claimed", "reward", "earned"))
            log(label, f"Achievement {k}: status {st2}" +
                (f" +{amt}" if amt is not None else ""))

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
                log(label, f"Task {t['key']}: claim status {st2}")

    st, state2 = api.post("session", {})
    if st == 200:
        age1, gram1 = balances(state2)
        delta = None
        if isinstance(age0, (int, float)) and isinstance(age1, (int, float)):
            delta = age1 - age0
        log(label, f"saldo akhir: AGE={age1} GRAM={gram1}" +
            (f" (total putaran +{delta} AGE)" if delta is not None else ""))


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
                await client.disconnect()
                return
            me = await client.get_me()
            log(label, f"login OK @{me.username} — ambil initData segar")
            init_data = await get_init_data(client)
        elif os.path.exists(init_path):
            with open(init_path) as f:
                init_data = f.read().strip()
            log(label, "mode initData dari file (tanpa Telethon)")
        else:
            log(label, "tidak ada sesi/initdata — dilewati "
                       f"(login dulu: python3 login.py {label})")
            return
        api = Api(init_data)
        await run_claims(label, api, client, args.inspect)
    except Exception as e:
        log(label, f"ERROR {type(e).__name__}: {str(e)[:160]}")
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


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
    for i, label in enumerate(labels):
        if i:
            await asyncio.sleep(random.uniform(5, 12))
        await process_account(label, args)


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
