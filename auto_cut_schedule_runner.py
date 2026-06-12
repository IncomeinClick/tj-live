#!/usr/bin/env python3
"""auto_cut_schedule_runner.py — end-to-end pipeline:
VOD → transcript → 7 clips (command+result for demos) → upload-post schedule → Telegram.

Triggered by POST /api/projects/{project_id}/auto-cut-schedule → subprocess.Popen.
See /root/skills/auto-cut-schedule-short-vdo/SKILL.md for spec.
"""
import asyncio
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from sqlalchemy import select

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
from backend.database import async_session  # noqa: E402
from backend.models import Project, ShortVideo  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("auto-cut")


def _load_env():
    env = {}
    env_path = APP_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = _load_env()

# ── Constants ──
MEDIA_DIR = Path(ENV.get("MEDIA_DIR") or (APP_DIR / "media"))
CLIPS_DIR = MEDIA_DIR / "clips"
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

# Documentor integration (optional — used only if you also self-host documentor)
DOCUMENTOR_DB = ENV.get("DOCUMENTOR_DB", "")
DOCUMENTOR_API = ENV.get("DOCUMENTOR_API", "")
DOCUMENTOR_AUTH = ENV.get("DOCUMENTOR_AUTH", "")
DOCUMENTOR_TH_PROJECT = ENV.get("DOCUMENTOR_TH_PROJECT", "")
FB_CRED_ID = ENV.get("FB_CRED_ID", "")
FB_PAGE_ID_FOR_UPLOAD = ENV.get("FB_PAGE_ID_FOR_UPLOAD", "")
RECAP_HOUR_BANGKOK = int(ENV.get("RECAP_HOUR_BANGKOK", "9"))

# Telegram (optional — leave blank to disable)
TG_TOKEN = ENV.get("TG_BOT_TOKEN", "")
TG_CHAT = ENV.get("TG_CHAT_ID", "")

# Tool paths (override via .env if your install differs)
AUTO_EDITOR = ENV.get("AUTO_EDITOR", "auto-editor")

# codex (subscription auth, NOT API key) replaces claude -p — gpt-5.4 low effort.
# read-only sandbox: the transcript is in the prompt, no file/network access needed.
_CODEX_NODE_BIN = "/root/.nvm/versions/node/v22.22.3/bin"
CODEX_ENV = {**os.environ, "PATH": _CODEX_NODE_BIN + ":" + os.environ.get("PATH", ""), "HOME": "/root"}
CODEX_CMD = ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
             "-m", "gpt-5.4", "-c", "model_reasoning_effort=low", "-c", "approval_policy=never"]
YTDLP_BIN_DEFAULT = ENV.get("YTDLP_BIN", "yt-dlp")

UPLOAD_POST_BASE = "https://api.upload-post.com/api"
UPLOAD_POST_BACKOFFS = [10, 30, 60]

VOD_POLL_MAX_ATTEMPTS = 36  # 10 min × 36 = 6 hours — FB can take ages to finalize VOD
VOD_POLL_INTERVAL = 600  # 10 minutes
VOD_POLL_HEARTBEAT_EVERY = 3  # Telegram heartbeat every 30 min (3 × 10 min)

TARGET_CLIP_COUNT = 7

UP_KEY = ENV.get("UPLOAD_POST_API_KEY", "")
UP_USER = ENV.get("UPLOAD_POST_USER", "")
PUBLIC_URL = ENV.get("PUBLIC_URL", "")


# ── Utils ──
def tg_send(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data
        )
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        log.warning(f"telegram failed: {e}")


def get_fb_credential():
    conn = sqlite3.connect(DOCUMENTOR_DB)
    row = conn.execute(
        "SELECT page_id, access_token FROM credentials WHERE id=?",
        (FB_CRED_ID,),
    ).fetchone()
    conn.close()
    return row


# ── Phase 1: Find VOD ──
def find_vod_once(page_id: str, token: str, live_date):
    url = (
        f"https://graph.facebook.com/v20.0/{page_id}/videos"
        f"?fields=id,title,source,created_time,live_status,permalink_url&limit=10&access_token={token}"
    )
    resp = urllib.request.urlopen(url, timeout=30)
    data = json.loads(resp.read())
    live_utc = live_date.replace(tzinfo=timezone.utc) if live_date.tzinfo is None else live_date
    for v in data.get("data", []):
        if v.get("live_status") != "VOD":
            continue
        ct_str = v.get("created_time", "").replace("Z", "+00:00")
        try:
            ct = datetime.fromisoformat(ct_str)
        except Exception:
            continue
        if abs((ct - live_utc).total_seconds()) < 86400:  # within 24h
            return v
    return None


def find_vod_with_retry(page_id: str, token: str, live_date, project_title: str = ""):
    """Poll FB every VOD_POLL_INTERVAL until VOD appears or VOD_POLL_MAX_ATTEMPTS exhausted.
    Send a Telegram heartbeat periodically so Pond knows it's still alive."""
    for attempt in range(1, VOD_POLL_MAX_ATTEMPTS + 1):
        try:
            v = find_vod_once(page_id, token, live_date)
        except urllib.error.HTTPError as e:
            log.warning(f"VOD poll {attempt}: HTTP {e.code} {e.read().decode()[:200]}")
            v = None
        except Exception as e:
            log.warning(f"VOD poll {attempt}: {e}")
            v = None
        if v:
            log.info(f"VOD found after {attempt} attempts: id={v['id']} title={v.get('title','')[:60]}")
            return v
        log.info(f"VOD not ready yet ({attempt}/{VOD_POLL_MAX_ATTEMPTS}), sleep {VOD_POLL_INTERVAL}s")
        # Heartbeat every N attempts (not on first) so Pond knows runner is alive
        if attempt > 1 and attempt % VOD_POLL_HEARTBEAT_EVERY == 0:
            elapsed_min = attempt * VOD_POLL_INTERVAL // 60
            tg_send(
                f"⏳ ยังรอ VOD อยู่ ({elapsed_min} นาทีแล้ว)\n"
                f"📌 {project_title}\n\n"
                f"FB ยังไม่ publish live video เป็น VOD — จะเช็คต่อทุก 10 นาที"
            )
        if attempt < VOD_POLL_MAX_ATTEMPTS:
            time.sleep(VOD_POLL_INTERVAL)
    return None


# ── Phase 2: Download ──
YTDLP_BIN = YTDLP_BIN_DEFAULT


def download_vod(video: dict, out_path: Path) -> None:
    """Download FB VOD at the highest available resolution.

    The `source` field from FB Graph API only points at the SD variant (~360x640).
    yt-dlp parses the public watch page and selects the best video+audio streams
    (typically 1080x1920 native), so we always get HD.
    """
    permalink = video.get("permalink_url") or ""
    watch_url = (
        f"https://www.facebook.com{permalink}" if permalink.startswith("/") else permalink
    )
    if watch_url:
        try:
            subprocess.run(
                [
                    YTDLP_BIN,
                    # Sort by resolution (descending) so 1080×1920 wins over FB's
                    # internal "quality" tag which can rank 360p higher otherwise.
                    "-S", "res:1080,br",
                    "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
                    "--merge-output-format", "mp4",
                    "-o", str(out_path),
                    "--no-progress", "--quiet",
                    watch_url,
                ],
                check=True, capture_output=True, timeout=1800,
            )
            if out_path.exists() and out_path.stat().st_size > 1024:
                return
        except subprocess.CalledProcessError as e:
            log.warning(f"yt-dlp failed ({(e.stderr or b'')[-300:]!r}), falling back to source URL")
        except Exception as e:
            log.warning(f"yt-dlp error: {e}, falling back to source URL")
    # Fallback: direct CDN download (SD)
    req = urllib.request.Request(video["source"], headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=900) as r:
        with open(out_path, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)


# ── Phase 3: Transcribe ──
def transcribe(video_path: Path, out_json: Path):
    from faster_whisper import WhisperModel

    model = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=8)
    segments_iter, _ = model.transcribe(
        str(video_path),
        language="th",
        beam_size=1,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    segs = [{"start": s.start, "end": s.end, "text": s.text} for s in segments_iter]
    out_json.write_text(json.dumps(segs, ensure_ascii=False, indent=2))
    return segs


def retranscribe_gap(video_path: Path, start: float, end: float):
    from faster_whisper import WhisperModel

    model = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=8)
    segments_iter, _ = model.transcribe(
        str(video_path),
        language="th",
        beam_size=1,
        vad_filter=False,
        clip_timestamps=f"{start},{end}",
    )
    return [{"start": s.start, "end": s.end, "text": s.text} for s in segments_iter]


# ── Phase 4: Pick clips via Claude CLI ──
CLIP_PICK_PROMPT = """You are editing a Thai live stream into {n} short viral clips for TikTok/IG/YT/FB-Page.

LIVE TITLE: {title}

TRANSCRIPT WITH TIMESTAMPS (seconds):
{transcript}

Task: pick the {n} most interesting moments to turn into 30-90s clips.

Hard rules:
- Each clip MUST have a strong hook in its first 2-3s (question, shocking claim, pain point, concrete number) and a payoff that closes the thought.
- Clips must NOT duplicate the same content. Different angle on similar theme is fine; the same line/demo re-clipped is not.
- End BEFORE the speaker transitions to a new topic — even if the end is mid-sentence. Watch for markers: "อีกเรื่องนึง", "OK ต่อ...", "แล้วก็..." + new noun, "ก็เป็นงาน..." false starts. Trim 1-3s past the last syllable of the payoff, never 5-10s.
- If a clip is a live demo of the speaker giving AI a command and the AI producing a result, mark is_demo=true and provide BOTH the command range and a plausible result range (usually minutes apart in the live — look for visible-result cues like "เห็นไหม", "สวยงาม", "เสร็จ", "หมดแล้ว" and numbers announced after the command).

Output ONLY a JSON array, nothing else:
[
  {{"start": 853, "end": 942, "title": "Thai title with 1 emoji at end", "is_demo": false}},
  {{"start": 3593, "end": 3649, "title": "...", "is_demo": true, "result_start": 3958, "result_end": 4012}}
]
"""


def pick_clips(segments, title: str, n: int = TARGET_CLIP_COUNT):
    transcript_text = "\n".join(
        f"[{int(s['start'])}-{int(s['end'])}] {s['text'].strip()}" for s in segments
    )
    prompt = CLIP_PICK_PROMPT.format(n=n, title=title, transcript=transcript_text)
    for attempt in range(1, 3):
        try:
            r = subprocess.run(
                CODEX_CMD + [prompt],
                capture_output=True, text=True, timeout=600, env=CODEX_ENV,
            )
            out = r.stdout.strip()
            if "[" in out and "]" in out:
                out = out[out.index("["):out.rindex("]") + 1]
            picks = json.loads(out)
            if isinstance(picks, list) and picks:
                return picks[:n]
        except Exception as e:
            log.warning(f"pick_clips attempt {attempt}: {e}")
    return []


# ── Phase 5: Cut ──
def ff_cut(live_file: Path, start, end, out: Path):
    subprocess.run(
        [
            "ffmpeg", "-y", "-ss", str(start), "-i", str(live_file),
            "-t", str(end - start),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            str(out),
        ],
        check=True, capture_output=True, timeout=600,
    )


def ff_concat(files, out: Path):
    lst = out.parent / f"concat-{out.stem}.txt"
    lst.write_text("\n".join(f"file '{p}'" for p in files))
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(lst), "-c", "copy", str(out),
            ],
            check=True, capture_output=True, timeout=300,
        )
    finally:
        lst.unlink(missing_ok=True)


def auto_edit(src: Path, out: Path, threshold: int = 6):
    subprocess.run(
        [
            AUTO_EDITOR, str(src),
            "--edit", f"audio:threshold={threshold}%",
            "--margin", "0.12sec",
            "--output", str(out), "--no-open",
        ],
        check=True, capture_output=True, timeout=900,
    )


def extract_frame(video: Path, ts: float, out: Path):
    subprocess.run(
        [
            "ffmpeg", "-y", "-ss", str(ts), "-i", str(video),
            "-frames:v", "1", "-vf", "scale=640:-1", "-q:v", "3", str(out),
        ],
        check=True, capture_output=True, timeout=60,
    )


def frame_is_mostly_black(path: Path, yavg_threshold: float = 15.0) -> bool:
    """Use ffmpeg signalstats to measure mean luminance. Lower = darker."""
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-i", str(path),
                "-vf", "signalstats",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=30,
        )
        # signalstats prints to stderr; look for YAVG or mean
        m = re.search(r"YAVG:([\d.]+)", r.stderr)
        if m:
            return float(m.group(1)) < yavg_threshold
        # Fallback: file size heuristic — dark JPG compresses much smaller
        return path.stat().st_size < 12000
    except Exception:
        return False


def cut_clip(live_file: Path, project_id: str, pick: dict, idx: int):
    """Return Path to final clip or None."""
    final = CLIPS_DIR / f"{project_id}-tim-{idx}.mp4"
    combined = CLIPS_DIR / f"tmp-combined-{project_id}-{idx}.mp4"
    tmp_files = []

    is_demo = (
        pick.get("is_demo")
        and pick.get("result_start") is not None
        and pick.get("result_end") is not None
    )

    try:
        if is_demo:
            rs, re_ = float(pick["result_start"]), float(pick["result_end"])
            # Visual verify result window
            sample = 6
            step = max(2.0, (re_ - rs) / sample)
            ts_samples = [rs + i * step for i in range(sample) if rs + i * step < re_]
            non_black = 0
            for t in ts_samples:
                fp = Path(f"/tmp/framecheck-{project_id}-{idx}-{int(t)}.jpg")
                try:
                    extract_frame(live_file, t, fp)
                    if not frame_is_mostly_black(fp):
                        non_black += 1
                except Exception:
                    pass
                finally:
                    fp.unlink(missing_ok=True)
            if non_black < 2:
                log.warning(
                    f"clip {idx}: result window [{rs:.0f}-{re_:.0f}] mostly black — falling back to single segment"
                )
                is_demo = False
            else:
                log.info(f"clip {idx}: result window verified ({non_black}/{sample} non-black frames)")

        if is_demo:
            tmp1 = CLIPS_DIR / f"tmp-{project_id}-{idx}-1.mp4"
            tmp2 = CLIPS_DIR / f"tmp-{project_id}-{idx}-2.mp4"
            tmp_files = [tmp1, tmp2]
            ff_cut(live_file, pick["start"], pick["end"], tmp1)
            ff_cut(live_file, pick["result_start"], pick["result_end"], tmp2)
            ff_concat([tmp1, tmp2], combined)
        else:
            ff_cut(live_file, pick["start"], pick["end"], combined)

        auto_edit(combined, final)
        return final
    except subprocess.CalledProcessError as e:
        log.error(f"clip {idx} ffmpeg failed: {(e.stderr or b'')[-400:]!r}")
        return None
    except Exception as e:
        log.error(f"clip {idx} cut failed: {e}")
        return None
    finally:
        combined.unlink(missing_ok=True)
        for f in tmp_files:
            Path(f).unlink(missing_ok=True)


# ── Phase 7: Caption ──
CAPTION_PROMPT = """Write a Thai social-media caption for this short clip.

CLIP TRANSCRIPT:
{transcript}

PRIOR TITLES IN THIS BATCH (for emoji rotation — do NOT reuse any emoji already listed):
{prior_titles}

Return ONLY JSON (no other text):
{{
  "title": "short hook, max 100 chars, end with 1 emoji not seen in prior titles",
  "description": "3-5 sentences casual Thai (ผม voice). Under-promise. End with 3-5 hashtags including #AI #Newton. NO LINKS ANYWHERE."
}}

Style rules:
- Hook = question, shocking claim, or concrete number
- Short sentences, line breaks between thoughts
- Under-promise timelines (if live says 10 นาที, say 10 นาที — never 'instant')
- ABSOLUTELY NO URLs in the caption — not your domain, not any other site. Social platforms downrank posts with links.
"""


# ── Phase 9: Documentor recap ──
RECAP_PROMPT = """The host just finished a Facebook live. Write a recap post that goes out ~24h after the live to pull people back to watch the replay.

LIVE TITLE: {title}
LIVE URL: {permalink}

TRANSCRIPT HIGHLIGHTS (picked moments from the live):
{picks}

Write casual Thai, no corporate jargon, under-promise.

Return ONLY JSON (no other text):
{{
  "title": "hook headline (Thai, 1 short sentence, grab attention, ends with 👇)",
  "caption": "main post caption — 3-5 short paragraphs teasing the content, make missing the live feel like FOMO. NO URLs in caption.",
  "blocks": [
    {{"text": "comment 1 — a highlight or tease from the live, Thai"}},
    {{"text": "comment 2 — another highlight"}},
    {{"text": "comment 3 — another tease"}},
    {{"text": "ดูย้อนหลังได้ที่ลิงก์นี้ครับ {permalink}"}}
  ]
}}

Rules:
- 3-5 total blocks (excluding the headline which you put in the "title" field).
- Last block MUST contain the VOD permalink exactly as given.
- Caption has NO links. Only the last block has the link.
- Use specific details from the transcript — no generic "มี insight ดีๆ เยอะ".

FACTUAL GUARDRAIL (very important):
- Describe ONLY what actually happened in the live, based on the transcript highlights above. Do NOT invent or exaggerate.
- Under-claim, never over-claim. If you're not sure a demo happened, leave it out.
- Don't promise something the live didn't deliver. Viewers will rewatch and feel cheated.
"""


def gen_recap(title: str, permalink: str, picks: list, segments: list):
    """Build a picks-summary block the model can reason over."""
    summaries = []
    for p in picks:
        body_txt = "\n".join(
            s["text"].strip() for s in segments
            if s["start"] >= p["start"] and s["end"] <= p["end"]
        )[:400]
        summaries.append(f"- {p.get('title', '')}\n  [{int(p['start'])}-{int(p['end'])}s] {body_txt}")
    prompt = RECAP_PROMPT.format(
        title=title, permalink=permalink,
        picks="\n".join(summaries)[:4000],
    )
    try:
        r = subprocess.run(
            CODEX_CMD + [prompt],
            capture_output=True, text=True, timeout=300, env=CODEX_ENV,
        )
        out = r.stdout.strip()
        if "{" in out and "}" in out:
            out = out[out.index("{"):out.rindex("}") + 1]
        return json.loads(out)
    except Exception as e:
        log.warning(f"gen_recap failed: {e}")
        return None


def documentor_create_and_schedule(title: str, caption: str, blocks: list, scheduled_at_utc: datetime) -> str | None:
    """Create Documentor content with inline blocks + schedule. Returns content_id or None.
    Block format (per openapi): {sort_order: int, text: str, image_url?: str}.
    """
    try:
        # sort_order=0 is the required "headline" block (= the main post body on FB).
        # sort_order=1..N are comment replies.
        content_blocks = [{"sort_order": 0, "text": title}]
        for i, blk in enumerate(blocks, 1):
            text = blk.get("text") or blk.get("body") or ""
            if not text:
                continue
            content_blocks.append({"sort_order": i, "text": text})

        body = json.dumps({
            "project_id": DOCUMENTOR_TH_PROJECT,
            "title": title,
            "caption": caption or "",
            "source": "manual",
            "blocks": content_blocks,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{DOCUMENTOR_API}/api/contents",
            data=body, method="POST",
            headers={"Authorization": DOCUMENTOR_AUTH, "Content-Type": "application/json"},
        )
        r = urllib.request.urlopen(req, timeout=60)
        content_id = json.loads(r.read())["id"]
        log.info(f"  Documentor content created: {content_id}")

        # Schedule
        sched_iso = scheduled_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        sbody = json.dumps({"scheduled_at": sched_iso}).encode()
        sq = urllib.request.Request(
            f"{DOCUMENTOR_API}/api/contents/{content_id}/schedule",
            data=sbody, method="POST",
            headers={"Authorization": DOCUMENTOR_AUTH, "Content-Type": "application/json"},
        )
        urllib.request.urlopen(sq, timeout=60).read()
        log.info(f"  Documentor scheduled for {sched_iso}")
        return content_id
    except urllib.error.HTTPError as e:
        log.error(f"documentor API {e.code}: {e.read().decode()[:400]}")
        return None
    except Exception as e:
        log.error(f"documentor failed: {e}")
        return None


def gen_caption(clip_transcript: str, prior_titles: list):
    prompt = CAPTION_PROMPT.format(
        transcript=clip_transcript or "(no transcript)",
        prior_titles="\n".join(f"- {t}" for t in prior_titles) or "(none)",
    )
    try:
        r = subprocess.run(
            CODEX_CMD + [prompt],
            capture_output=True, text=True, timeout=180, env=CODEX_ENV,
        )
        out = r.stdout.strip()
        if "{" in out and "}" in out:
            out = out[out.index("{"):out.rindex("}") + 1]
        data = json.loads(out)
        return data.get("title", "")[:100], data.get("description", "")
    except Exception as e:
        log.warning(f"caption failed: {e}")
        return None, None


# ── Phase 8: Upload-post schedule ──
def upload_post_schedule(clip_path: Path, title: str, description: str, scheduled_iso_z: str, fb_first_comment: str | None = None):
    # Enum values from upload-post openapi.json (components/schemas):
    #   TikTokPrivacyLevel: PUBLIC_TO_EVERYONE | MUTUAL_FOLLOW_FRIENDS | FOLLOWER_OF_CREATOR | SELF_ONLY
    #   TikTokPostMode: DIRECT_POST | MEDIA_UPLOAD
    #   YouTubePrivacyStatus: public | unlisted | private  (LOWERCASE)
    data = [
        ("user", UP_USER),
        ("platform[]", "tiktok"),
        ("platform[]", "instagram"),
        ("platform[]", "youtube"),
        ("platform[]", "facebook"),
        ("scheduled_date", scheduled_iso_z),
        ("title", title),
        ("description", description),
        # TikTok
        ("tiktok_title", title),
        ("privacy_level", "PUBLIC_TO_EVERYONE"),
        ("post_mode", "DIRECT_POST"),
        ("disable_duet", "false"),
        ("disable_comment", "false"),
        ("disable_stitch", "false"),
        ("brand_content_toggle", "false"),
        ("is_aigc", "false"),
        # Instagram
        ("media_type", "REELS"),
        # YouTube
        ("youtube_title", title[:100]),
        ("youtube_description", description),
        ("privacyStatus", "public"),
        ("categoryId", "22"),
        # Facebook
        ("facebook_page_id", FB_PAGE_ID_FOR_UPLOAD),
    ]
    if fb_first_comment:
        data.append(("facebook_first_comment", fb_first_comment))
    last_err = None
    for attempt, backoff in enumerate([0] + UPLOAD_POST_BACKOFFS):
        if backoff:
            time.sleep(backoff)
        try:
            with open(clip_path, "rb") as f:
                files = {"video": (clip_path.name, f, "video/mp4")}
                r = requests.post(
                    f"{UPLOAD_POST_BASE}/upload",
                    headers={"Authorization": f"Apikey {UP_KEY}"},
                    data=data, files=files, timeout=900,
                )
            if r.status_code == 202:
                return r.json().get("job_id")
            if 400 <= r.status_code < 500:
                log.error(f"upload {r.status_code} (no retry): {r.text[:400]}")
                return None
            last_err = f"HTTP {r.status_code}: {r.text[:300]}"
            log.warning(f"upload attempt {attempt+1}: {last_err}")
        except Exception as e:
            last_err = str(e)
            log.warning(f"upload attempt {attempt+1} exc: {e}")
    log.error(f"upload exhausted retries: {last_err}")
    return None


# ── Orchestration ──
async def _set_status(project_id: str, status: str):
    async with async_session() as db:
        p = await db.get(Project, project_id)
        if p:
            p.status = status
            p.updated_at = datetime.now(timezone.utc)
            await db.commit()


async def _clear_old_clips(project_id: str):
    async with async_session() as db:
        rows = (
            await db.execute(
                select(ShortVideo).where(
                    ShortVideo.project_id == project_id,
                    ShortVideo.source == "tim",
                )
            )
        ).scalars().all()
        for r in rows:
            if r.file_path and os.path.exists(r.file_path):
                try:
                    os.remove(r.file_path)
                except Exception:
                    pass
            await db.delete(r)
        await db.commit()


async def run(project_id: str) -> int:
    async with async_session() as db:
        p = await db.get(Project, project_id)
        if not p:
            tg_send(f"❌ Auto-cut: project {project_id} not found")
            return 1
        if p.status != "processing":
            log.info(f"setting status=processing (was {p.status})")
            p.status = "processing"
            p.updated_at = datetime.now(timezone.utc)
            await db.commit()
        project_title = p.title
        live_date = p.live_date

    try:
        # Phase 1: find VOD
        cred = get_fb_credential()
        if not cred:
            tg_send(f"❌ Auto-cut: no FB credential '{FB_CRED_ID}'\n📌 {project_title}")
            await _set_status(project_id, "upcoming")
            return 2
        page_id, token = cred
        log.info(f"Phase 1: polling FB VOD for page {page_id} (up to {VOD_POLL_MAX_ATTEMPTS * VOD_POLL_INTERVAL // 3600}h)")
        video = find_vod_with_retry(page_id, token, live_date, project_title)
        if not video:
            tg_send(
                f"⚠️ VOD ยังไม่พร้อมหลัง {VOD_POLL_MAX_ATTEMPTS * VOD_POLL_INTERVAL // 3600} ชั่วโมง\n"
                f"📌 {project_title}\n\nกด Mark Done + Auto-cut อีกครั้งถ้ายังอยากให้ลอง"
            )
            await _set_status(project_id, "upcoming")
            return 3

        # Build VOD permalink once for reuse (FB first-comment + Phase 9 recap).
        permalink_path = video.get("permalink_url", "")
        vod_permalink = (
            f"https://www.facebook.com{permalink_path}"
            if permalink_path.startswith("/") else permalink_path
        ) if permalink_path else ""
        fb_first_comment = f"ดูไลฟ์เต็ม 👇\n{vod_permalink}" if vod_permalink else None

        # Phase 2: download
        live_file = MEDIA_DIR / f"live-{project_id}.mp4"
        if not (live_file.exists() and live_file.stat().st_size > 1024):
            log.info("Phase 2: downloading VOD")
            download_vod(video, live_file)
            log.info(f"  downloaded {live_file.stat().st_size / 1024 / 1024:.1f} MB")

        # Phase 3: transcribe
        transcript_file = MEDIA_DIR / f"transcript-{project_id}.json"
        if transcript_file.exists():
            segments = json.loads(transcript_file.read_text())
            log.info(f"Phase 3: reusing transcript {len(segments)} segments")
        else:
            log.info("Phase 3: transcribing (may take ~90 min)")
            segments = transcribe(live_file, transcript_file)
            log.info(f"  {len(segments)} segments")

        # Phase 4: pick clips
        log.info(f"Phase 4: picking {TARGET_CLIP_COUNT} clips via Claude CLI")
        picks = pick_clips(segments, project_title, n=TARGET_CLIP_COUNT)
        if not picks:
            tg_send(f"❌ Claude failed to pick clips\n📌 {project_title}")
            await _set_status(project_id, "upcoming")
            return 4
        log.info(f"  Claude picked {len(picks)} clips")
        for i, p in enumerate(picks, 1):
            tag = "demo" if p.get("is_demo") else "single"
            log.info(f"    [{i}] {tag} {p.get('start')}-{p.get('end')}: {p.get('title','')[:60]}")

        # Clear old clips for this project
        await _clear_old_clips(project_id)

        # Phase 5: cut
        log.info("Phase 5: cutting clips")
        cut_results = []
        for idx, pick in enumerate(picks, 1):
            out = cut_clip(live_file, project_id, pick, idx)
            if out:
                cut_results.append((idx, pick, out))

        if not cut_results:
            tg_send(f"❌ ตัดคลิปไม่ผ่านสักอัน\n📌 {project_title}")
            await _set_status(project_id, "upcoming")
            return 5

        # Phases 6–8: captions, save, schedule
        log.info("Phase 6-8: captions + DB save + schedule")
        live_day = live_date.date() if hasattr(live_date, "date") else live_date
        prior_titles: list = []
        scheduled = 0
        failed_uploads = []

        for i, (idx, pick, out_path) in enumerate(cut_results):
            clip_transcript = "\n".join(
                f"- {s['text'].strip()}"
                for s in segments
                if s["start"] >= pick["start"] and s["end"] <= pick["end"]
            )
            title, desc = gen_caption(clip_transcript, prior_titles)
            if not title:
                title = (pick.get("title") or project_title)[:100]
                desc = clip_transcript[:300] + "\n\n#AI #Newton"
            prior_titles.append(title)

            sched_dt = datetime(
                year=live_day.year, month=live_day.month, day=live_day.day,
                hour=12, minute=0, second=0, tzinfo=timezone.utc,
            ) + timedelta(days=i)
            sched_iso = sched_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            job_id = upload_post_schedule(out_path, title, desc, sched_iso, fb_first_comment)

            async with async_session() as db:
                sv = ShortVideo(
                    project_id=project_id, source="tim",
                    title=title, description=desc,
                    file_path=str(out_path),
                    status="scheduled" if job_id else "draft",
                    upload_post_job_id=job_id,
                    scheduled_at=sched_dt if job_id else None,
                )
                db.add(sv)
                await db.commit()

            if job_id:
                scheduled += 1
                log.info(f"  ✓ clip {idx} scheduled {sched_iso} (job {job_id[:8]})")
            else:
                failed_uploads.append(idx)
                log.warning(f"  ✗ clip {idx} schedule failed")

        # Phase 9: Documentor recap (pull viewers back to the VOD ~24h after live)
        recap_status = "skip"
        recap_schedule_dt = None
        try:
            if vod_permalink:
                log.info(f"Phase 9: generating Documentor recap (VOD {vod_permalink})")
                recap = gen_recap(project_title, vod_permalink, picks, segments)
                recap_title = (recap or {}).get("title") or (recap or {}).get("headline")
                if recap and recap_title and recap.get("blocks"):
                    # schedule at live_day + 1 at 09:00 Bangkok = 02:00 UTC
                    recap_schedule_dt = datetime(
                        live_day.year, live_day.month, live_day.day,
                        tzinfo=timezone.utc,
                    ) + timedelta(days=1, hours=RECAP_HOUR_BANGKOK - 7)
                    cid = documentor_create_and_schedule(
                        recap_title, recap.get("caption", ""),
                        recap["blocks"], recap_schedule_dt,
                    )
                    recap_status = "scheduled" if cid else "failed"
                    if cid:
                        # TJ Live stores datetimes as Bangkok-local naive (like live_date).
                        # recap_schedule_dt is UTC (for Documentor API); convert to Bangkok-naive for DB.
                        bkk_naive = (recap_schedule_dt + timedelta(hours=7)).replace(tzinfo=None)
                        async with async_session() as db:
                            pp = await db.get(Project, project_id)
                            if pp:
                                pp.recap_documentor_id = cid
                                pp.recap_scheduled_at = bkk_naive
                                await db.commit()
                else:
                    recap_status = "gen_failed"
            else:
                log.warning("no permalink_url on VOD — skip recap")
        except Exception as e:
            log.exception(f"Phase 9 recap error: {e}")
            recap_status = "exception"

        # Phase 10: notify + mark done
        start_day = live_day
        end_day = live_day + timedelta(days=len(cut_results) - 1)
        lines = [
            "🎬 Auto-cut + schedule เสร็จ!",
            "",
            f"📌 {project_title}",
            f"✂️ ตัด {len(cut_results)}/{TARGET_CLIP_COUNT} คลิป",
            f"📅 โพสต์ {start_day} – {end_day} เวลา 19:00 น.",
            f"🌐 TikTok + IG + YT + FB Page",
            f"✅ schedule สำเร็จ {scheduled} / {len(cut_results)}",
        ]
        if failed_uploads:
            lines.append(f"⚠️ upload fail: clip {failed_uploads}")
        if recap_status == "scheduled" and recap_schedule_dt:
            local_dt = recap_schedule_dt + timedelta(hours=7)
            lines.append(f"📣 Recap Documentor: {local_dt.strftime('%Y-%m-%d %H:%M')} (Bangkok)")
        elif recap_status != "skip":
            lines.append(f"⚠️ Recap: {recap_status}")
        if PUBLIC_URL:
            lines += ["", f"👉 {PUBLIC_URL}"]
        tg_send("\n".join(lines))

        await _set_status(project_id, "done")
        log.info("=== pipeline complete ===")
        return 0
    except Exception as e:
        log.exception("pipeline crashed")
        tg_send(f"❌ Auto-cut crashed\n📌 {project_title}\n\n{e!r}")
        await _set_status(project_id, "upcoming")
        return 10


def main():
    if len(sys.argv) < 2:
        print("usage: auto_cut_schedule_runner.py <project_id>", file=sys.stderr)
        sys.exit(1)
    project_id = sys.argv[1]
    log.info(f"=== auto-cut-schedule-short-vdo START project={project_id} ===")
    code = asyncio.run(run(project_id))
    sys.exit(code)


if __name__ == "__main__":
    main()
