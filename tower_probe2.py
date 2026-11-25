#!/usr/bin/env python3
"""Probe Tower fase 2 (live, akun alt saja) — validasi server lanjutan.

Lanjutan temuan fase 1 (25 Nov): stake invalid ditolak, layout null
selama run terbuka (baru terisi SETELAH run selesai), floor-skip
diabaikan diam-diam (server otoritatif), cash floor 1 = +450 bersih.

Fase 2 menguji:
  T1 double-start   : start kedua selagi run masih terbuka
  T2 slot invalid   : pick slot 99 / -1 pada run terbuka
  T3 race picks     : 3 pick paralel (slot 0,1,2) di lantai yang sama
  T4 double-cash    : cash kedua untuk run yang sudah cashed
Semua respons jaringan status 0 (transien dari VM) di-retry; sesudah
POST yang ambigu, state dibaca ulang via GET tower (rekonsiliasi).

Pakai: python3 tower_probe2.py akun2
Risiko: 1 climb (stake 1.000) untuk T1–T3 + penutup aman.
"""
import json
import sys
import threading
import time

import myage_claim as mc

LABEL = sys.argv[1] if len(sys.argv) > 1 else "akun2"
if LABEL in mc.TOWER_BLOCKED_LABELS:
    print("DITOLAK: akun utama tidak boleh jadi bahan probe.")
    sys.exit(2)


def load_api():
    import os
    p = os.path.join(mc.INIT_DIR, f"{LABEL}.txt")
    with open(p) as f:
        return mc.Api(f.read().strip())


API = load_api()


def req(method, *a, tries=4):
    """Retry khusus status 0 (error jaringan transien)."""
    st, js = 0, None
    for i in range(tries):
        st, js = getattr(API, method)(*a)
        if st != 0:
            return st, js
        print(f"    (jaringan status 0, retry {i + 1})", flush=True)
        time.sleep(8)
    return st, js


def state():
    st, tw = req("get", "tower")
    return mc._unwrap(tw) if st == 200 else {}


def wait_pause():
    b = state()
    left = ((b.get("pause") or {}).get("leftMs") or 0)
    if left:
        print(f"    pause server {left / 1000:.0f} dtk — menunggu…",
              flush=True)
        time.sleep(left / 1000 + 3)
    return b


def main():
    b = wait_pause()
    print(f"awal: points={b.get('points')} run={b.get('run')}", flush=True)
    if b.get("run"):
        print("ada run terbuka dari sebelumnya — hentikan, periksa dulu.")
        return

    # ---- start climb uji ----
    st, js = req("post", "tower", {"op": "start", "mode": "normal",
                                   "stake": 1000})
    run = (mc._unwrap(js) or {}).get("run") or {}
    rid = run.get("id")
    print(f"[start] {st} run={rid} points="
          f"{(mc._unwrap(js) or {}).get('points')}", flush=True)
    mc.dump("probe", "p2_start", js)
    if not rid:
        print("start gagal — berhenti.")
        return

    # ---- T1 double-start ----
    st, js = req("post", "tower", {"op": "start", "mode": "normal",
                                   "stake": 1000})
    u = mc._unwrap(js) or {}
    print(f"[T1 double-start] {st} -> run_baru="
          f"{(u.get('run') or {}).get('id')} points={u.get('points')} "
          f"err={u.get('error')}", flush=True)
    mc.dump("probe", "p2_double_start", js)

    # ---- T2 slot invalid ----
    for slot in (99, -1):
        st, js = req("post", "tower", {"op": "pick", "id": rid,
                                       "floor": 0, "slot": slot})
        u = mc._unwrap(js) or {}
        r = u.get("run") or {}
        print(f"[T2 slot={slot}] {st} -> status={r.get('status')} "
              f"floor={r.get('floor')} picks={r.get('picks')} "
              f"err={u.get('error')}", flush=True)
        mc.dump("probe", f"p2_slot_{slot}", js)

    b = state()
    r = b.get("run") or {}
    print(f"    state rekonsiliasi: floor={r.get('floor')} "
          f"picks={r.get('picks')} status={r.get('status')} "
          f"points={b.get('points')}", flush=True)
    if r.get("status") != "open":
        print("run tidak open lagi sesudah T1/T2 — loncat ke T4.")
    else:
        # ---- T3 race picks: 3 slot paralel di lantai berjalan ----
        floor = r.get("floor") or 0
        results = {}

        def fire(slot):
            api = load_api()
            s, j = api.post("tower", {"op": "pick", "id": rid,
                                      "floor": floor, "slot": slot})
            results[slot] = (s, mc._unwrap(j) or {})

        ths = [threading.Thread(target=fire, args=(s,))
               for s in (0, 1, 2)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        for s in sorted(results):
            stt, u = results[s]
            rr = u.get("run") or {}
            print(f"[T3 race slot={s}] {stt} -> status={rr.get('status')} "
                  f"floor={rr.get('floor')} picks={rr.get('picks')} "
                  f"err={u.get('error')}", flush=True)
        mc.dump("probe", "p2_race", {str(k): [v[0], v[1]]
                                     for k, v in results.items()})
        time.sleep(2)
        b = state()
        r = b.get("run") or {}
        print(f"    state akhir race: status={r.get('status')} "
              f"floor={r.get('floor')} picks={r.get('picks')} "
              f"take={r.get('take')} points={b.get('points')} "
              f"layout={r.get('layout')}", flush=True)

        # ---- penutup aman: cash bila floor>=1 & open ----
        if r.get("status") == "open" and (r.get("floor") or 0) >= 1:
            st, js = req("post", "tower", {"op": "cash", "id": rid})
            u = mc._unwrap(js) or {}
            rr = u.get("run") or {}
            print(f"[tutup cash] {st} -> status={rr.get('status')} "
                  f"payout={rr.get('payout')} points={u.get('points')} "
                  f"layout={rr.get('layout')}", flush=True)
            mc.dump("probe", "p2_cash", js)
        elif r.get("status") == "open":
            print("    run masih open di floor 0 — dibiarkan; "
                  "putaran loop v9 akan melanjutkannya (resume).")

    # ---- T4 double-cash pada run yang sudah selesai ----
    st, js = req("post", "tower", {"op": "cash", "id": rid})
    u = mc._unwrap(js) or {}
    print(f"[T4 double-cash] {st} -> points={u.get('points')} "
          f"err={u.get('error')} body={json.dumps(u)[:200]}", flush=True)
    mc.dump("probe", "p2_double_cash", js)
    b = state()
    print(f"akhir: points={b.get('points')} run={b.get('run')}",
          flush=True)


if __name__ == "__main__":
    main()
