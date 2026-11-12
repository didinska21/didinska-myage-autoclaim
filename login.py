#!/usr/bin/env python3
"""Login satu akun Telegram -> StringSession tersimpan per label.

Pemakaian: python3 login.py <label>     (contoh: python3 login.py akun1)

- Nomor HP + kode OTP diketik LANGSUNG di terminal VPS ini.
- Password 2FA (bila aktif) diketik lewat getpass (tidak tampil di layar).
- Hasil login disimpan ke sessions/<label>.session (chmod 600).
  Nilai sesi TIDAK pernah dicetak. JANGAN bagikan folder sessions/ —
  isinya setara kunci login Telegram akun-akun itu.
- Cukup dijalankan SEKALI per akun; sesudah itu myage_claim.py jalan
  sendiri memakai sesi yang tersimpan.
"""
import asyncio
import getpass
import os
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

API_ID = int(os.environ.get("MYAGE_API_ID", "2040"))
API_HASH = os.environ.get("MYAGE_API_HASH", "b18441a1ff607e10a989891a5462e627")
HERE = os.path.dirname(os.path.abspath(__file__))
SESS_DIR = os.path.join(HERE, "sessions")


async def main():
    if len(sys.argv) < 2:
        print("Pemakaian: python3 login.py <label>")
        return
    label = sys.argv[1]
    os.makedirs(SESS_DIR, exist_ok=True)
    try:
        os.chmod(SESS_DIR, 0o700)
    except Exception:
        pass
    path = os.path.join(SESS_DIR, f"{label}.session")
    existing = ""
    if os.path.exists(path):
        with open(path) as f:
            existing = f.read().strip()
    client = TelegramClient(StringSession(existing), API_ID, API_HASH,
                            connection_retries=2, retry_delay=1, timeout=20)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"[{label}] SUDAH LOGIN sebagai @{me.username} — "
              "tidak perlu login ulang.")
        await client.disconnect()
        return
    phone = input(f"[{label}] Nomor HP (format +62...): ").strip()
    sent = await client.send_code_request(phone)
    code = input(f"[{label}] Kode OTP dari Telegram: ").strip()
    try:
        await client.sign_in(phone=phone, code=code,
                             phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        pw = getpass.getpass(f"[{label}] Password 2FA: ")
        await client.sign_in(password=pw)
    me = await client.get_me()
    with open(path, "w") as f:
        f.write(client.session.save())
    os.chmod(path, 0o600)
    print(f"[{label}] LOGIN BERHASIL @{me.username} — sesi tersimpan.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
