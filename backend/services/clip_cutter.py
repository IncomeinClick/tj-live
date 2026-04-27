"""Clip Cutter — download live video, transcribe, select highlights, cut clips."""
import json
import sqlite3
import subprocess
import urllib.request
import os
from pathlib import Path
from datetime import datetime, timezone

import logging
log = logging.getLogger("tj-live.clip-cutter")

GRAPH_API = "https://graph.facebook.com/v20.0"
APP_DIR = Path(__file__).resolve().parent.parent.parent


def _load_env() -> dict:
    env: dict[str, str] = {}
    env_path = APP_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


_ENV = _load_env()
MEDIA_DIR = Path(_ENV.get("MEDIA_DIR") or (APP_DIR / "media"))
CLIPS_DIR = MEDIA_DIR / "clips"
MEDIA_DIR.mkdir(exist_ok=True)
CLIPS_DIR.mkdir(exist_ok=True)

WHISPER_MODEL_SIZE = "small"  # tiny/base/small/medium/large — small = good balance


def get_th_credential():
    env = _load_env()
    doc_db = env.get("DOCUMENTOR_DB", "")
    fb_cred_id = env.get("FB_CRED_ID", "")
    if doc_db and fb_cred_id and Path(doc_db).exists():
        conn = sqlite3.connect(doc_db)
        c = conn.cursor()
        c.execute("SELECT page_id, access_token FROM credentials WHERE id=?", (fb_cred_id,))
        row = c.fetchone()
        conn.close()
        if row:
            return {"page_id": row[0], "access_token": row[1]}

    page_id = env.get("FB_PAGE_ID", "")
    access_token = env.get("FB_ACCESS_TOKEN", "")
    if page_id and access_token:
        return {"page_id": page_id, "access_token": access_token}
    return None


def get_latest_live_video(page_id: str, token: str):
    """Get the most recent ended live video from FB page."""
    url = (
        f"{GRAPH_API}/{page_id}/live_videos"
        f"?fields=id,title,description,permalink_url,status,creation_time,video{{id,source}}"
        f"&limit=5&access_token={token}"
    )
    try:
        resp = urllib.request.urlopen(url, timeout=30)
        data = json.loads(resp.read())
        for lv in data.get("data", []):
            if lv.get("status") == "VOD":
                return lv
    except Exception as e:
        log.error(f"Failed to fetch live videos: {e}")
    return None


def download_video(fb_url: str, output_path: Path) -> bool:
    """Download video using yt-dlp."""
    log.info(f"Downloading {fb_url}...")
    ytdlp = _load_env().get("YTDLP_BIN", "yt-dlp")
    try:
        subprocess.run(
            [ytdlp, "-f", "best", "-o", str(output_path), fb_url],
            check=True, capture_output=True, timeout=1800
        )
        return output_path.exists()
    except subprocess.CalledProcessError as e:
        log.error(f"yt-dlp failed: {e.stderr.decode() if e.stderr else e}")
        return False


def transcribe_video(video_path: Path) -> list:
    """Use Whisper to transcribe video. Returns list of segments with timestamps."""
    log.info(f"Transcribing {video_path} (may take a while)...")
    import whisper
    model = whisper.load_model(WHISPER_MODEL_SIZE)
    result = model.transcribe(str(video_path), language="th", verbose=False)
    return result.get("segments", [])


def select_highlights(segments: list, video_title: str, num_clips: int = 5) -> list:
    """Use Claude CLI to pick best highlight segments."""
    # Build transcript with timestamps
    transcript_text = ""
    for seg in segments:
        start = int(seg["start"])
        end = int(seg["end"])
        text = seg["text"].strip()
        transcript_text += f"[{start}s-{end}s] {text}\n"

    prompt = f"""You are selecting highlight clips from a Thai live stream titled: "{video_title}"

TRANSCRIPT WITH TIMESTAMPS:
{transcript_text}

TASK: Select {num_clips} best moments to turn into short viral clips for social media.
Criteria:
- Each clip should be 30-90 seconds long
- Must have a strong hook or interesting point
- Should stand alone without context
- Pick diverse topics from across the stream
- Direct response marketing style — hooks that make people stop scrolling

Output ONLY valid JSON array like this:
[
  {{"start": 120, "end": 180, "title": "Thai title for clip"}},
  {{"start": 450, "end": 520, "title": "Another Thai title"}}
]

Only output the JSON, nothing else."""

    try:
        result = subprocess.run(
            ["/root/.local/bin/claude", "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=180
        )
        output = result.stdout.strip()
        # Extract JSON
        if "[" in output:
            output = output[output.index("["):output.rindex("]")+1]
        clips = json.loads(output)
        return clips
    except Exception as e:
        log.error(f"Claude selection failed: {e}")
        return []


def cut_clip(video_path: Path, start: int, end: int, output_path: Path) -> bool:
    """Cut a clip from video using ffmpeg."""
    duration = end - start
    try:
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(start), "-i", str(video_path),
            "-t", str(duration), "-c", "copy", str(output_path)
        ], check=True, capture_output=True, timeout=120)
        return output_path.exists()
    except subprocess.CalledProcessError as e:
        log.error(f"ffmpeg cut failed: {e.stderr.decode() if e.stderr else e}")
        return False


async def process_live_for_project(project_id: str, db):
    """Main pipeline: download live → transcribe → select → cut → save to DB."""
    from backend.models import ShortVideo, Project

    project = await db.get(Project, project_id)
    if not project:
        log.error(f"Project {project_id} not found")
        return

    cred = get_th_credential()
    if not cred:
        log.error("No TH credential")
        return

    # 1. Find latest live video
    live = get_latest_live_video(cred["page_id"], cred["access_token"])
    if not live:
        log.error("No live video found")
        return

    live_id = live["id"]
    video_title = live.get("title") or project.title
    log.info(f"Found live video: {live_id} — {video_title}")

    # 2. Download
    video_file = MEDIA_DIR / f"live-{project_id}.mp4"
    if not video_file.exists():
        source = live.get("video", {}).get("source") or live.get("permalink_url")
        fb_url = f"https://www.facebook.com/{source}" if source and source.startswith("/") else source
        if not fb_url:
            fb_url = f"https://www.facebook.com{live['permalink_url']}"
        if not download_video(fb_url, video_file):
            return

    # 3. Transcribe
    segments = transcribe_video(video_file)
    if not segments:
        log.error("Transcription failed")
        return
    log.info(f"Transcribed {len(segments)} segments")

    # Save transcript for debugging
    transcript_file = MEDIA_DIR / f"transcript-{project_id}.json"
    with open(transcript_file, "w") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    # 4. Select highlights
    highlights = select_highlights(segments, video_title, num_clips=5)
    if not highlights:
        log.error("No highlights selected")
        return
    log.info(f"Selected {len(highlights)} highlights")

    # 5. Cut clips + save to DB
    for idx, h in enumerate(highlights, 1):
        clip_file = CLIPS_DIR / f"{project_id}-tim-{idx}.mp4"
        if cut_clip(video_file, h["start"], h["end"], clip_file):
            sv = ShortVideo(
                project_id=project_id,
                source="tim",
                title=h.get("title", f"Clip {idx}"),
                file_path=str(clip_file),
                status="draft",
            )
            db.add(sv)
            log.info(f"✅ Clip {idx}: {h.get('title')}")
    await db.commit()

    # Telegram
    from backend.services.scheduler import send_telegram
    public_url = _load_env().get("PUBLIC_URL", "")
    msg = f"🎬 Clips cut!\n\n📌 {project.title}\n✂️ {len(highlights)} clips"
    if public_url:
        msg += f"\n\n👉 {public_url}"
    send_telegram(msg)
