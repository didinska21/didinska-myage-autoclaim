#!/usr/bin/env python3
"""Ambil initData SEGAR satu akun alt dan tulis ke initdata/<label>.txt.

Jalan di VPS (di sana sesi Telethon-nya hidup). File hasilnya adalah
kredensial berumur pendek — kirim ke Potato lewat chat, JANGAN sebar,
hapus dari VPS sesudah terkirim (rm initdata/<label>.txt di VPS tidak
mengganggu sesi/loop: file itu hanya dipakai mode tanpa-Telethon).

  python3 dump_initdata.py akun2

DITOLAK untuk akun1/@didinska (aturan Bos: akun utama tidak disentuh
untuk urusan Tower).
"""
import asyncio
import os
import sys

import myage_claim as mc


async def amain(label):
    if label in mc.TOWER_BLOCKED_LABELS:
        print("DITOLAK: akun1 = akun utama @didinska (aturan Bos).")
        return 1
    sess_path = os.path.join(mc.SESS_DIR, f"{label}.session")
    if not os.path.exists(sess_path):
        print(f"tidak ada sesi untuk {label}")
        return 1
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    with open(sess_path) as f:
        sess = f.read().strip()
    client = TelegramClient(StringSession(sess), mc.API_ID, mc.API_HASH,
                            connection_retries=1, retry_delay=1,
                            timeout=15)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("sesi mati — login ulang dulu")
            return 1
        me = await client.get_me()
        uname = ("@" + (me.username or "")).lower()
        if uname in mc.TOWER_BLOCKED_USERS:
            print(f"DITOLAK: {label} ternyata {uname} (akun utama).")
            return 1
        init_data = await mc.get_init_data(client)
    finally:
        await client.disconnect()
    os.makedirs(mc.INIT_DIR, exist_ok=True)
    path = os.path.join(mc.INIT_DIR, f"{label}.txt")
    with open(path, "w") as f:
        f.write(init_data)
    os.chmod(path, 0o600)
    print(f"OK {label} ({uname}) -> initdata/{label}.txt "
          f"({len(init_data)} byte). Kirim file itu ke Potato, "
          f"lalu hapus: rm initdata/{label}.txt")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("pakai: python3 dump_initdata.py <label-akun-alt>")
        sys.exit(2)
    sys.exit(asyncio.run(amain(sys.argv[1])))
