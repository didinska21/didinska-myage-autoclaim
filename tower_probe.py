#!/usr/bin/env python3
"""Probe celah Time Tower — SATU akun, SATU climb, risiko maks 1 stake.

Urutan uji (dari yang tanpa risiko ke yang berisiko):
  0. GET tower — dump state mentah, cari field mencurigakan.
  1. Start stake TIDAK VALID (100) — kalau server menerima, semua uji
     berikutnya jadi murah. Climb-nya langsung diselesaikan normal.
  2. Start normal (stake terkecil, mode Normal) — dump respons START
     mentah; pindai rekursif field apa pun yang berbau layout/cracked/
     next/seed. Kalau bocor: pakai untuk naik sampai atas (pasti).
  3. FLOOR-SKIP: pick pertama dikirim dengan floor=7 (lantai 8).
     - Ditolak/error  -> server memvalidasi, climb lanjut normal.
     - Diterima       -> lihat hasilnya: top/payout = CELAH KONFIRMASI.
     - Diterima+kalah -> stake hangus (risiko yang disadari: 1 stake).
  4. Sisa climb diselesaikan aman: cash begitu lantai >= 2.
  5. Opsional --race (climb BARU, +1 stake risiko): pick paralel semua
     slot di lantai yang sama + cash ganda paralel; dump semua respons.

Semua respons mentah disimpan ke inspect/probe_*.json.
Pakai HANYA di akun alt terkecil, JANGAN di akun utama.

  python3 tower_probe.py --accounts akun4
  python3 tower_probe.py --accounts akun4 --race
"""
import argparse
import asyncio
import json
import os
import random
import threading
import time

import myage_claim as mc

SUSPECT_KEYS = ("layout", "crack", "seed", "next", "board", "glass",
                "bomb", "mine", "safe", "win", "lose", "answer", "key")


def scan_suspects(obj, path="$", out=None, depth=0):
    """Kumpulkan field yang namanya berbau bocoran layout."""
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            if any(s in str(k).lower() for s in SUSPECT_KEYS):
                out.append((p, v if not isinstance(v, (dict, list))
                            else f"<{type(v).__name__} len="
                                 f"{len(v)}>"))
            scan_suspects(v, p, out, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            scan_suspects(v, f"{path}[{i}]", out, depth + 1)
    return out


def dump_probe(name, obj):
    mc.dump("probe", name, obj)
    print(f"    (dump: inspect/probe_{name}.json)", flush=True)


def unwrap(js):
    return mc._unwrap(js)


def finish_climb_safely(api, label, run, stake):
    """Cash di lantai >= 2, atau teruskan pick acak sampai selesai."""
    while isinstance(run, dict) and run.get("status") == "open":
        floor = run.get("floor") or 0
        if floor >= 2:
            st, js = api.post("tower", {"op": "cash", "id": run.get("id")})
            r = unwrap(js).get("run") or {}
            print(f"  [aman] cash lantai {r.get('floor')} "
                  f"payout {r.get('payout')} (status {st})", flush=True)
            return r
        slot = random.randrange(3 if (run.get("mode") != "risky") else 2)
        st, js = api.post("tower", {"op": "pick", "id": run.get("id"),
                                    "floor": floor, "slot": slot})
        run = unwrap(js).get("run") or {}
        print(f"  [aman] pick lantai {floor} slot {slot} -> "
              f"{run.get('status')} floor={run.get('floor')}", flush=True)
    if isinstance(run, dict) and run.get("status") == "lost":
        print(f"  [aman] climb kalah — stake {stake} hangus", flush=True)
    return run


async def get_api(label):
    """Buka sesi akun (pola sama seperti myage_claim.process_account)."""
    sess_path = os.path.join(mc.SESS_DIR, f"{label}.session")
    init_path = os.path.join(mc.INIT_DIR, f"{label}.txt")
    client = None
    if os.path.exists(sess_path):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        with open(sess_path) as f:
            sess = f.read().strip()
        client = TelegramClient(StringSession(sess), mc.API_ID,
                                mc.API_HASH, connection_retries=1,
                                retry_delay=1, timeout=15)
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("sesi mati — login ulang dulu")
        init_data = await mc.get_init_data(client)
    elif os.path.exists(init_path):
        with open(init_path) as f:
            init_data = f.read().strip()
    else:
        raise RuntimeError("tidak ada sesi/initdata untuk label ini")
    return mc.Api(init_data), client


async def main_async(args):
    label = args.accounts
    # ATURAN BOS 25 Nov 13:56 WIB: akun utama @didinska (akun1) TIDAK
    # BOLEH jadi bahan uji Tower — probe hanya untuk akun alt.
    if label in mc.TOWER_BLOCKED_LABELS:
        print("DITOLAK: akun1 adalah akun utama @didinska — probe "
              "Tower hanya untuk akun alt (aturan Bos).", flush=True)
        return
    api, client = await get_api(label)
    try:
        if client is not None:
            me = await client.get_me()
            if ("@" + (me.username or "")).lower() in \
                    mc.TOWER_BLOCKED_USERS:
                print("DITOLAK: akun ini @didinska (akun utama) — "
                      "probe Tower hanya untuk akun alt.", flush=True)
                return
        # ── Fase 0: state mentah ──────────────────────────────
        st, js = api.get("tower")
        print(f"[0] GET tower status {st}", flush=True)
        dump_probe("state", js)
        tw = unwrap(js)
        for p, v in scan_suspects(js):
            print(f"    ⚠️ field mencurigakan: {p} = {v}", flush=True)
        stakes = [s for s in (tw.get("stakes") or []) if mc._num(s)]
        stake = min(stakes) if stakes else 1000
        run = tw.get("run")
        if isinstance(run, dict) and run.get("status") == "open":
            print("[!] Ada climb terbuka dari sesi lain — selesaikan "
                  "dulu secara aman, probe berhenti.", flush=True)
            finish_climb_safely(api, label, run, run.get("stake") or stake)
            return

        # ── Fase 1: stake tidak valid ─────────────────────────
        st, js = api.post("tower", {"op": "start", "mode": "normal",
                                    "stake": 100})
        print(f"[1] start stake=100 (tidak valid) -> status {st} "
              f"{mc.resp_message(js)[:80]}", flush=True)
        dump_probe("start_invalid_stake", js)
        r = unwrap(js).get("run")
        if st == 200 and isinstance(r, dict):
            print("    🔥 CELAH: server menerima stake di luar daftar! "
                  "Climb murah ini diselesaikan aman.", flush=True)
            finish_climb_safely(api, label, r, 100)
            return

        # ── Fase 2+3: climb asli — bocoran layout + floor-skip ─
        st, js = api.post("tower", {"op": "start", "mode": "normal",
                                    "stake": stake})
        print(f"[2] start stake={stake} normal -> status {st}", flush=True)
        dump_probe("start", js)
        if st != 200:
            print(f"    start gagal: {mc.resp_message(js)[:100]}",
                  flush=True)
            return
        suspects = scan_suspects(js)
        for p, v in suspects:
            print(f"    ⚠️ field mencurigakan di START: {p} = {v}",
                  flush=True)
        run = unwrap(js).get("run") or {}
        rid = run.get("id")

        layout = None
        body = unwrap(js)
        for cand in (body, body.get("run") or {}):
            if isinstance(cand.get("layout"), list):
                layout = cand["layout"]
        if layout:
            print(f"    🔥🔥 LAYOUT BOCOR DI START: {layout} — naik "
                  f"dengan slot aman sampai atas!", flush=True)
            nslots = 3
            while run.get("status") == "open":
                f = run.get("floor") or 0
                safe = [s for s in range(nslots) if s != layout[f]]
                slot = safe[0]
                st, js = api.post("tower", {"op": "pick", "id": rid,
                                            "floor": f, "slot": slot})
                run = unwrap(js).get("run") or {}
                print(f"    pick lantai {f} slot aman {slot} -> "
                      f"{run.get('status')} payout={run.get('payout')}",
                      flush=True)
                if run.get("status") not in ("open",):
                    break
            return

        # ── Fase 3: floor-skip ────────────────────────────────
        slot = random.randrange(3)
        st, js = api.post("tower", {"op": "pick", "id": rid,
                                    "floor": 7, "slot": slot})
        print(f"[3] pick SKIP floor=7 slot={slot} -> status {st} "
              f"{mc.resp_message(js)[:80]}", flush=True)
        dump_probe("pick_skip", js)
        run2 = unwrap(js).get("run") or {}
        if st == 200 and run2:
            print(f"    hasil: status={run2.get('status')} "
                  f"floor={run2.get('floor')} "
                  f"payout={run2.get('payout')} take={run2.get('take')}",
                  flush=True)
            if run2.get("status") in ("top", "cashed") or \
                    (run2.get("payout") or 0) > stake * 3:
                print("    🔥🔥 CELAH FLOOR-SKIP KONFIRMASI — satu pick "
                      "dari lantai 1 lompat ke payout besar!", flush=True)
                return
            if run2.get("status") == "lost":
                print("    Skip DITERIMA tapi gelasnya retak — celah "
                      "tetap ada (server tidak memvalidasi floor), "
                      "kalahnya cuma faktor 1/3. Stake hangus.", flush=True)
                return
            if run2.get("status") == "open" and \
                    (run2.get("floor") or 0) <= 1:
                print("    Server MENGABAIKAN floor kiriman (memakai "
                      "state sendiri) — bukan celah. Lanjut aman.",
                      flush=True)
                finish_climb_safely(api, label, run2, stake)
                return
            finish_climb_safely(api, label, run2, stake)
            return
        print("    Skip ditolak (server memvalidasi floor). Lanjut "
              "selesaikan climb secara aman.", flush=True)
        st, js = api.get("tower")
        run = unwrap(js).get("run") or run
        finish_climb_safely(api, label, run, stake)

        # ── Fase 5: race (opsional, climb baru) ───────────────
        if args.race:
            print("[5] RACE: climb baru — pick paralel 3 slot + "
                  "cash ganda paralel", flush=True)
            time.sleep(32)
            st, js = api.post("tower", {"op": "start", "mode": "normal",
                                        "stake": stake})
            run = unwrap(js).get("run") or {}
            rid = run.get("id")
            if st != 200 or not rid:
                print(f"    start race gagal status {st}", flush=True)
                return
            results = {}

            def fire(name, body):
                s2, j2 = api.post("tower", body)
                results[name] = (s2, j2)

            ths = [threading.Thread(target=fire,
                                    args=(f"slot{s}",
                                          {"op": "pick", "id": rid,
                                           "floor": 0, "slot": s}))
                   for s in range(3)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            for name, (s2, j2) in sorted(results.items()):
                r = unwrap(j2).get("run") or {}
                print(f"    {name}: status {s2} run="
                      f"{r.get('status')} floor={r.get('floor')} "
                      f"{mc.resp_message(j2)[:50]}", flush=True)
            dump_probe("race_picks", {k: v[1] for k, v in
                                      results.items()})
            st, js = api.get("tower")
            run = unwrap(js).get("run") or {}
            finish_climb_safely(api, label, run, stake)
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description="Probe celah Time Tower")
    ap.add_argument("--accounts", required=True,
                    help="SATU label akun alt (jangan akun utama)")
    ap.add_argument("--race", action="store_true",
                    help="tambah uji race pick/cash (risiko +1 stake)")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
