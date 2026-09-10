# TJ Live

Write an outline, present it. Type your topics and bullets in the dashboard, then open the
same content as full-screen slides or as an animated mind map — one branch at a time, in
front of a live audience or a camera.

Built for teaching and course-building: the outline *is* the presentation, so there is no
slide deck to maintain on the side.

## What it does

- **Outline editor** — presentations → topics → bullets, nested up to 3 levels, drag to reorder
- **Slide mode** — full-screen dark slides, one topic per slide, bullets revealed on click
- **Mind map mode** — the same outline drawn as a radial map that grows as you click, with
  drag-to-pan and zoom controls
- Both modes share one position, so you can switch between them mid-sentence (`M`)

## Quick Start

```bash
git clone https://github.com/IncomeinClick/tj-live.git
cd tj-live

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8800
```

Open http://localhost:8800 — the setup wizard asks for an email + password on first load and
writes `.env` for you. That is the whole configuration; there are no API keys to collect.

## Presenting

| Key | |
|---|---|
| `→` / click / space | reveal the next bullet, then the next topic |
| `←` | step back |
| `M` | switch slides ↔ mind map |
| `F` | full screen |
| `0` | reset the map view |

In mind map mode, drag the background to move the map and use the − / + buttons in the
bottom-right to zoom. The view stays where you put it — revealing a node never moves it.

Each presentation has two links, so an OBS scene can point straight at either one:

```
/live/<project-id>              slides
/live/<project-id>?view=map     mind map
```

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

- **Backend:** FastAPI + SQLAlchemy/aiosqlite
- **Frontend:** two HTML files, Alpine.js, vanilla JS — no build step
- **Storage:** SQLite

## Built by Newton

This tool was built by [Newton](https://newton.incomeinclick.in.th) — an AI teammate that
works on your business the way a hire would: it builds and runs the software, writes the
content, runs the campaigns, and reports back.

TJ Live is free and MIT licensed. If it is useful to you, leave the Newton mark on the
presentation screen — that is the only thing we ask in return.

## License

MIT
