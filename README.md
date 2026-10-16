# Nontonin extraction API

Backend ekstraksi URL-only untuk aplikasi Nontonin Dulu. Python stdlib + yt-dlp, tanpa framework web, database, browser, FFmpeg, atau penyimpanan video. Server hanya membaca metadata/manifest dan menjawab deskriptor CDN; **tidak pernah** mengambil byte media (tanpa preflight/HEAD/Range ke CDN). Ponsel yang mentransfer video.

## Deploy produksi (Docker + reverse proxy milik Anda)

Prasyarat VPS: Docker Engine + Compose, reverse proxy HTTPS (Caddy/nginx/...) dengan batas body ~16 KiB, network external `proxy-tier` yang sama dengan proxy (`docker network create proxy-tier` sekali saja), dan DNS A/AAAA domain Anda ke server.

```sh
umask 077
python3 -c 'import secrets; print("NONTONIN_API_TOKEN=" + secrets.token_urlsafe(32))' > .env
chmod 600 .env
docker compose pull
docker compose up -d --wait --wait-timeout 90 --remove-orphans
```

Contoh `Caddyfile` tidak ikut repo (gitignored, berisi domain operator); tulis blok reverse proxy sendiri ke backend `backend-nonton-yuks:8080`, lalu validate/reload proxy. Origin aplikasi = domain Anda; token dari `.env` server dimasukkan sekali ke build aplikasi (env `NONTON_YUKS_API_TOKEN`), tidak di source code. Jangan commit token atau menampilkannya di log/terminal.

Image `ghcr.io/ridhoarmand/backend-nonton-yuks:latest` diterbitkan workflow GitHub tiap push `main` (tes + smoke + build amd64/arm64). Catatan: package GHCR baru default **private**; sekali saja buka Package settings → Change visibility → Public agar pull anonim bisa jalan.

## Kontrak

Endpoint: `POST /v1/extract`, `Content-Type: application/json`, `Authorization: Bearer <token>`, body `{"text":"caption atau satu URL"}` atau `{"text":"...","height":720}` (batas tinggi YouTube opsional, integer positif). Tanpa field platform: server mendeteksi YouTube vs Facebook dari host URL, jadi aplikasi cukup mengirim tautan. Tanpa field platform: server mendeteksi YouTube vs Facebook dari host URL, jadi aplikasi cukup mengirim tautan. `GET /healthz` tanpa token menjawab `{"status":"ok"}` (tanda proses siap, bukan jaminan ekstraksi live bisa). Platform aktif: Facebook dan YouTube saja; tidak ada extractor generik.

Respons 200 (tanpa alias lama):

```json
{"platform":"youtube","videoId":"jNQXAC9IVRw","sourceUrl":"https://www.youtube.com/watch?v=jNQXAC9IVRw","title":"...","uploader":null,"durationMs":19000,"thumbnailUrl":null,"height":240,"heights":[1080,720,480,360,240],"approxBytes":null,"streams":[{"kind":"video","parts":["https://...googlevideo.com/..."],"headers":{"User-Agent":"..."},"approxBytes":null},{"kind":"audio","parts":["https://...googlevideo.com/..."],"headers":{"User-Agent":"..."},"approxBytes":null}]}
```

`streams` tepat satu `muxed`, atau satu `video` + satu `audio` (H264 MP4 + AAC; muxed diutamakan saat seri). `parts` berurutan lengkap: satu URL file utuh, atau init+fragmen dari yt-dlp (maks 128/stream, tiap URL <= 16 KiB, respons serial <= 256 KiB). Fragmen live/unbounded, multi-period ganti track, byte-range-only, DRM, dan HLS ditolak dengan `unsupportedMedia`.

Kegagalan: `{"error":{"code":"...","message":"..."}}` berbahasa Indonesia. Kode: `invalidUrl` (400), `unauthorized` (401), `unavailable`/`blocked`/`unsupportedMedia` (422), `network` (502), `busy` (503). Satu ekstraksi dalam satu waktu; selebihnya `busy`.

## Batas akses dan operasional

- Host sumber persis registri aplikasi (facebook.com + www/m/web, fb.watch + www, youtube.com + www/m/music, youtu.be + www). YouTube dinormalisasi ke `watch?v=`; `v` ganda ditolak. Source HTTP dinormalisasi ke HTTPS.
- Metadata boleh dibaca dari subdomain facebook.com/fb.watch/fbcdn.net, subdomain youtube.com, `youtubei.googleapis.com`, `consent.google.com`, dan `googlevideo.com` (untuk manifest). Hasil CDN: suffix facebook.com/fbcdn.net (FB) atau googlevideo.com (YT); thumbnail suffix ytimg.com/fbcdn.net/facebook.com. Boundary suffix selalu `host == suffix || host.endswith('.'+suffix)`.
- Semua egress memakai transport HTTPS khusus: seluruh jawaban DNS harus publik, koneksi ke alamat tervalidasi dengan SNI hostname asli, port 443, tanpa userinfo/kontrol/backslash/IP literal, redirect maksimal 5 dengan validasi ulang tiap hop. Header credential/hop-by-hop tidak diteruskan; Content-Type JSON/form extractor dipertahankan. Respons metadata bertipe audio/video/image/octet-stream ditolak sebelum body dibaca (maks 8 MiB).
- yt-dlp metadata-only (`download=False`, tanpa pemeriksaan/pengunduhan format), hanya extractor terdaftar, tanpa playlist/login/cookie/proxy env, tanpa retry, dengan QuickJS `/usr/local/bin/qjs` + `yt-dlp-ejs` untuk challenge. Worker process-group sendiri; timeout selalu terminate/kill/reap grup termasuk solver. Deadline 45 detik, socket 20 detik, 16 koneksi.
- **URL CDN dapat terikat IP/wilayah/UA/masa berlaku.** Sukses dari IP server tidak membuktikan ponsel beda IP bisa mengunduh. Aplikasi resolve ulang saat job mulai dan menampilkan kegagalan CDN apa adanya; tanpa proxy media, cookie, atau login.
- Image: `python:3.14-slim` + qjs resmi terverifikasi checksum, UID/GID 10001, read-only, tmpfs 16m, `no-new-privileges`, pids 32, mem 80m, cpu 1. Terukur: image 150 MB, idle RSS ~35 MB di dalam cap 80 MB. Pengukuran puncak lama ~87 MB **melewati** cap ini, jadi ekstraksi live pertama adalah uji sesungguhnya; bila OOM, naikkan `mem_limit`, jangan mematikan pengaman lain. Tidak ada file video di container.

## Regression tests lokal

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest -v
```

Fixture sintetis, tanpa akses jaringan. Mencakup: single/duplicate URL, ID YouTube 11 char + tolak `v` ganda, host spoof, IP privat v4/v6, rebinding, redirect identity, ranking muxed vs pasangan H264/AAC, urutan/batas fragmen, DRM/live/codec tak didukung, filesize unknown, deskriptor jahat, tanpa request media saat extract, batas request/respons, auth/busy sebelum ekstraksi, health tanpa auth saat ekstraksi sibuk, dan worker group-kill (POSIX).

Smoke yang terbukti: YouTube `jNQXAC9IVRw` → 200 video+audio dalam ~3 detik (judul/uploader/durasi/thumbnail nyata); Facebook reel `1195289147628387` → 200 muxed; dua sampel FB lain `unavailable` (butuh login). Jangan melabeli fixture sebagai lulus live.
