# TJ Live

A live-stream studio for Facebook Live: plan agendas, push promo posts before going live, then auto-process the VOD into short clips and schedule them across TikTok / Instagram / YouTube / FB Reels — all from one dashboard.

## What it does

- **Live planning** — projects with topics + bullets you want to cover
- **Promo posting** — generate FB + IG image posts from project content; optionally email-blast via Brevo
- **VOD ingest** — fetch the recorded live from FB Graph (or pull AI-cut clips from Restream)
- **Auto clip cutter** — Whisper transcribes, an LLM picks highlights, ffmpeg cuts the clips
- **Multi-platform scheduling** — push the cut clips to TikTok / IG Reels / YouTube Shorts / FB Reels via [upload-post.com](https://upload-post.com) on a schedule
- **Recap posts** — optional Documentor integration to publish a recap article ~24h after the live

## Quick Start

```bash
git clone https://github.com/Income-in-Click/tj-live.git
cd tj-live

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8800
```

Open http://localhost:8800 — on first load the setup wizard asks for an email + password and writes the `.env` file for you.

## Configuration

All config lives in `.env`. The setup wizard auto-creates the auth fields; everything else is optional and can be added as you start using each integration. See `.env.example` for the full list.

| Group | Variables | When you need them |
|---|---|---|
| Auth | `USER_EMAIL`, `USER_PASS_HASH`, `SECRET_KEY` | Auto-set by setup wizard |
| Telegram | `TG_BOT_TOKEN`, `TG_CHAT_ID` | Optional notifications when promos post / clips finish |
| upload-post | `UPLOAD_POST_API_KEY`, `UPLOAD_POST_USER` | Scheduling clips to TikTok/IG/YT/FB |
| Facebook | `FB_PAGE_ID` + `FB_ACCESS_TOKEN` (or Documentor) | Posting promos + downloading VODs |
| Restream | `RESTREAM_CLIENT_ID/SECRET/REDIRECT_URI` | Pulling AI-cut clips from Restream |
| Documentor | `DOCUMENTOR_DB`, `DOCUMENTOR_API`, `DOCUMENTOR_AUTH`, `DOCUMENTOR_TH_PROJECT`, `DOCUMENTOR_URL` | Auto recap posts |
| Brevo | `BREVO_API_KEY`, `BREVO_LIST_IDS`, `BREVO_SENDER_NAME`, `BREVO_SENDER_EMAIL` | Email blast when announcing a live |
| Tools | `CLAUDE_CLI`, `AUTO_EDITOR`, `YTDLP_BIN`, `PROMO_SKILL_PATH` | Override paths to local binaries |

## Production deployment

- Reverse proxy with nginx + certbot in front of `127.0.0.1:8800`
- Systemd unit example:

```
[Service]
Type=simple
ExecStart=/path/to/tj-live/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8800
WorkingDirectory=/path/to/tj-live
Restart=always
```

## Tech stack

- **Backend:** FastAPI + APScheduler + SQLAlchemy/aiosqlite
- **Frontend:** Single HTML, Alpine.js, vanilla JS
- **External:** Whisper (CPU `faster-whisper` recommended), `ffmpeg`, `auto-editor`, `yt-dlp`
- **Storage:** SQLite

## License

MIT
