# MyAge Auto-Claim — Multi-Akun (VPS)

Script Python untuk auto-claim My Age (@MyAgeApp) banyak akun sekaligus,
jalan langsung di VPS — tanpa browser, tanpa klik UI.

> **v7 (18 Nov 2026, malam):** AKAR MASALAH "No task." ditemukan dari
> bundle app live — klaim task yang benar adalah `POST tasks` dengan
> body **`{taskId: <id>}`** (fungsi `pressQuest` di app), bukan `{key}`
> / `{id}` seperti yang script kirim sejak v1; kedua bentuk lama itu
> ditolak server 400 "No task." selamanya, jadi spam v4–v6 tidak akan
> pernah tembus. Alur kini persis app + logika Bos: aksi Telegram
> (join/buka post + reaksi) dikerjakan **sekali saja**, sesudah itu
> yang di-spam hanya Claim (`{taskId}` 30x rapat) — notif "Open the
> link, then tap Claim again" **bukan** perintah mengulang aksinya.
> Hadiah dibaca dari field respons `paid` / `gramPaid`.
>
> **v6 (18 Nov 2026):** dari video Bos soal task "Like and share for new
> IMPORTANT post" (+2.000) yang sering gagal ke-claim. Di app, Claim
> ditekan **terus-terusan secara rapat** sampai popup "Task done" muncul
> (percobaan awal cuma dibalas "Open the link, then tap Claim again"),
> lalu task yang sama muncul lagi di **EXTRA TASKS** — di-Open lagi,
> di-spam Claim lagi, tuntas untuk +2.000 kedua. Script kini meniru
> persis itu: spam-claim **30x jeda rapat 0,4–0,9 dtk** (aksi Open
> diulang di percobaan ke-5/13/21) dan daftar task **di-GET ulang
> multi-pass (maks 4)** supaya instans Extra Tasks / task yang baru
> terbuka ikut terproses.
>
> **v5 (15 Nov 2026):** tiga lubang ditutup, dibedah dari bundle app versi
> live + video pemakaian Bos. (1) **Mystery spin kini dimainkan** — stok
> spin menunggu (`mystery.left` di state sesi) diputar via `POST mystery`;
> hadiahnya GRAM atau Telegram Premium. (2) **Achievement tidak lolos
> lagi** — deteksi siap-klaim kini persis logika app (`done && !claimed`);
> sebelumnya script mencari flag yang tidak pernah dikirim server, jadi
> achievement tuntas seperti Hoarder +2.500 tidak pernah terklaim.
> (3) **Saldo terbaca lagi** — field AGE di state sesi adalah `points`;
> spin kini dibaca dari slice pemenang sehingga hadiah GRAM tidak lagi
> tercatat sebagai AGE.
>
> **v4 (13 Nov 2026):** klaim task pakai spam-claim sampai 10x sampai
> tembus (bug klaim app), sesuai aturan Bos.
>
> **v3 (13 Nov 2026):** mesin task mengikuti alur app persis — klik task →
> aksi di Telegram (join channel / START bot / buka postingan + reaksi 👍) →
> balik → claim, dengan satu kali percobaan ulang bila server menjawab
> "belum selesai". Parser task toleran bentuk API (key/id, status string),
> hasil tiap task tercatat, dan task ber-link luar Telegram dilewati.
>
> **v2 (13 Nov 2026):** total putaran kini dihitung dari hadiah klaim yang
> benar-benar dibayar server (v1 salah membaca field `age` di respons
> session — itu statistik lain, bukan saldo), hadiah dibedakan AGE vs GRAM
> (spin bisa bayar GRAM), status "sudah diambil hari ini" tampil sebagai
> info (bukan error), dan setiap putaran ditutup **tabel ringkasan
> multi-akun**. Terverifikasi jalan end-to-end di VPS (5 akun, 13 Nov 2026).

Per putaran, per akun, script mengerjakan persis jalur API resmi app-nya:

1. Ambil **initData segar** dari WebView bot @MyAgeApp (lewat sesi Telegram akun itu sendiri)
2. `POST session` — buka sesi, baca saldo awal
3. Klaim: **Vault → Daily reward → Spin reguler → Name tag harian → Link bio harian**
4. **Achievements** yang sudah selesai (`done`) & belum diklaim
5. **Tasks**: baca daftar task; aksi Telegram tiap task dikerjakan SEKALI (join channel / START bot / buka postingan + reaksi), lalu klaim `POST tasks {taskId}` di-spam rapat sampai 30x sampai tembus (aksi tidak diulang di tengah spam); daftar di-GET ulang multi-pass untuk instans EXTRA TASKS
6. **Mystery spin**: stok spin menunggu dari state sesi diputar semua (hadiah GRAM / Telegram Premium)
7. `POST session` lagi — baca saldo akhir + total AGE yang masuk putaran itu

## Pagar (sudah keras di dalam kode)

Endpoint di luar allowlist **tidak bisa** dipanggil script ini. Yang tidak
pernah disentuh: reserve/bayar Pass (`pass/invoice`, `pass/mint`), kode promo,
link X, withdraw, kasino, share/posting, connect wallet. Script tidak invite
siapa pun dan tidak mengubah nama/bio — item tag/bio hanya *klaim hadiah*
yang server verifikasi sendiri dari nama/bio yang sudah ada.

## Pasang di VPS

```bash
cd myage-autoclaim
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Login akun (sekali per akun)

```bash
python3 login.py akun1   # ketik nomor HP + OTP di terminal VPS
python3 login.py akun2
python3 login.py akun3
python3 login.py akun4
python3 login.py akun5
```

Sesi tersimpan di `sessions/<label>.session` (chmod 600).
**Folder `sessions/` = kunci login Telegram. Jangan dibagikan, jangan di-commit.**

## Jalan

```bash
python3 myage_claim.py --once --inspect   # putaran pertama + dump struktur API ke inspect/
python3 myage_claim.py --once             # satu putaran biasa
python3 myage_claim.py --loop --interval 9000   # jalan terus, putaran tiap ±2,5 jam
python3 myage_claim.py --accounts akun1,akun3   # batasi akun tertentu
```

Log tertulis di layar dan di `runs.log`. Mode alternatif: bila tidak mau
pakai Telethon, taruh initData mentah per akun di `initdata/<label>.txt`
(script otomatis memakainya bila file sesi tidak ada) — tapi initData bisa
kadaluwarsa, mode sesi Telegram di atas yang awet.

Jalan 24 jam via systemd (contoh):

```ini
[Unit]
Description=MyAge auto-claim
After=network-online.target

[Service]
WorkingDirectory=/root/myage-autoclaim
ExecStart=/root/myage-autoclaim/venv/bin/python myage_claim.py --loop --interval 9000
Restart=always

[Install]
WantedBy=multi-user.target
```

## Catatan penting

- Script ini butuh VPS yang bisa konek **MTProto Telegram** secara normal.
  VPS Potato **tidak bisa** (egress-nya me-reset handshake Telegram — sudah
  diuji 12 Nov 2026), jadi login + jalan script dilakukan di VPS Bos sendiri.
- API Telegram yang dipakai default adalah kredensial publik klien Telegram
  Desktop (sama seperti pemakaian Telethon umumnya). Bisa diganti pakai
  api_id/api_hash sendiri dari my.telegram.org lewat env
  `MYAGE_API_ID` / `MYAGE_API_HASH`.
- Otomasi multi-akun selalu ada risiko ditandai proyek sebagai farming saat
  distribusi. Itu keputusan yang sudah diambil — script ini hanya alatnya.
- Distribusi AGE mulai 20 Nov 2026 19:00 WIB; sesudah itu script boleh
  dimatikan (`--loop` berhenti dengan Ctrl+C / stop service).
