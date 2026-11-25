#!/usr/bin/env python3
"""Probe Tower fase 3 (live, akun alt) — tes murah + kumpul sampel layout.

Tes tanpa risiko stake:
  U1 start mode invalid ("turbo")
  U2 pick pada run LAMA yang sudah cashed (replay)
Sesudah itu N climb sampel (cash di lantai 1) untuk:
  U3 cash dengan field payout ditamper (server harus mengabaikan)
  U4 kumpulkan layout tiap climb -> cek duplikat/pola antar-climb

Pakai: python3 tower_probe3.py akun2 [jumlah_sampel=4]
"""
import json
import random
import sys
import time

import myage_claim as mc

LABEL = sys.argv[1] if len(sys.argv) > 1 else "akun2"
SAMPLES = int(sys.argv[2]) if len(sys.argv) > 2 else 4
if LABEL in mc.TOWER_BLOCKED_LABELS:
    print("DITOLAK: akun utama tidak boleh jadi bahan probe.")
    sys.exit(2)

import os
with open(os.path.join(mc.INIT_DIR, f"{LABEL}.txt")) as f:
    API = mc.Api(f.read().strip())

OLD_CASHED_ID = "cmwvs6j9127b80wnzi5rl7a9t"  # run akun2 yg sudah cashed


def req(method, *a, tries=4):
    st, js = 0, None
    for i in range(tries):
        st, js = getattr(API, method)(*a)
        if st != 0:
            return st, js
        time.sleep(8)
    return st, js


def state():
    st, tw = req("get", "tower")
    return mc._unwrap(tw) if st == 200 else {}


def wait_pause():
    b = state()
    left = ((b.get("pause") or {}).get("leftMs") or 0)
    if left:
        print(f"    pause {left / 1000:.0f} dtk…", flush=True)
        time.sleep(left / 1000 + 3)
    return b


def main():
    b = wait_pause()
    p0 = b.get("points")
    print(f"awal: points={p0} run={b.get('run')}", flush=True)
    if b.get("run"):
        print("run terbuka — berhenti, periksa dulu.")
        return

    # U1: mode invalid
    st, js = req("post", "tower", {"op": "start", "mode": "turbo",
                                   "stake": 1000})
    u = mc._unwrap(js) or {}
    print(f"[U1 mode=turbo] {st} err={u.get('error')} run="
          f"{(u.get('run') or {}).get('id')}", flush=True)
    mc.dump("probe", "p3_mode_turbo", js)

    # U2: pick pada run lama yang sudah cashed
    st, js = req("post", "tower", {"op": "pick", "id": OLD_CASHED_ID,
                                   "floor": 1, "slot": 0})
    u = mc._unwrap(js) or {}
    print(f"[U2 pick run cashed] {st} err={u.get('error')} "
          f"body={json.dumps(u)[:160]}", flush=True)
    mc.dump("probe", "p3_pick_old", js)
    b = state()
    print(f"    points sesudah U1/U2: {b.get('points')} "
          f"(awal {p0}) run={b.get('run')}", flush=True)

    # U3+U4: climb sampel, cash di lantai 1 (payout ditamper di sampel 1)
    layouts = [[0, 0, 0, 1, 0, 1, 0, 2], [0, 2, 1, 1, 0, 0, 0, 1]]
    net = 0
    for n in range(SAMPLES):
        b = wait_pause()
        st, js = req("post", "tower", {"op": "start", "mode": "normal",
                                       "stake": 1000})
        u = mc._unwrap(js) or {}
        run = u.get("run") or {}
        rid = run.get("id")
        if not rid:
            print(f"[sampel {n + 1}] start gagal {st} {u.get('error')}")
            break
        slot = random.randint(0, 2)
        st, js = req("post", "tower", {"op": "pick", "id": rid,
                                       "floor": 0, "slot": slot})
        u = mc._unwrap(js) or {}
        run = u.get("run") or {}
        if run.get("status") != "open":
            net -= 1000
            print(f"[sampel {n + 1}] PECAH di lantai 1 (slot {slot}) "
                  f"layout={run.get('layout')}", flush=True)
            if run.get("layout"):
                layouts.append(run["layout"])
            mc.dump("probe", f"p3_sample{n + 1}", js)
            continue
        body = {"op": "cash", "id": rid}
        if n == 0:
            body["payout"] = 99999999  # U3: tamper
            body["floor"] = 8
        st, js = req("post", "tower", body)
        u = mc._unwrap(js) or {}
        run = u.get("run") or {}
        pay = run.get("payout") or 0
        net += pay - 1000
        print(f"[sampel {n + 1}] cash payout={pay} "
              f"{'(TAMPER diabaikan)' if n == 0 else ''} "
              f"layout={run.get('layout')} points={u.get('points')}",
              flush=True)
        if run.get("layout"):
            layouts.append(run["layout"])
        mc.dump("probe", f"p3_sample{n + 1}", js)

    print(f"\nneto sampel: {net:+} AGE")
    print("layout terkumpul:")
    seen = {}
    for i, lay in enumerate(layouts):
        key = tuple(lay)
        dup = "  <-- DUPLIKAT dari sampel " + str(seen[key] + 1) \
            if key in seen else ""
        seen.setdefault(key, i)
        print(f"  {i + 1}. {lay}{dup}")
    b = state()
    print(f"akhir: points={b.get('points')} run={b.get('run')}")


if __name__ == "__main__":
    main()
