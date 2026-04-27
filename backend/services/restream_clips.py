"""Fetch AI Clips from Restream and save to TJ Live."""
import json
import logging
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger("tj-live.restream-clips")

RESTREAM_API_BASE = "https://api.restream.io/v2"
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


CLIPS_DIR = Path(_load_env().get("MEDIA_DIR") or (APP_DIR / "media")) / "clips"
CLIPS_DIR.mkdir(parents=True, exist_ok=True)


async def get_restream_token(db) -> str | None:
    from backend.models import Setting
    s = await db.get(Setting, "restream_access_token")
    return s.value if s else None


def api_request(endpoint: str, token: str, method: str = "GET"):
    """Make authenticated Restream API request."""
    url = f"{RESTREAM_API_BASE}{endpoint}"
    req = urllib.request.Request(url, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    resp = urllib.request.urlopen(req, timeout=60)
    return json.loads(resp.read())


def download_file(url: str, save_path: Path, token: str = None) -> bool:
    """Download file from URL (Restream clip download URL)."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=300)
        with open(save_path, "wb") as f:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                f.write(chunk)
        return save_path.exists() and save_path.stat().st_size > 0
    except Exception as e:
        log.error(f"Download failed: {e}")
        return False


async def fetch_restream_clips_for_project(project_id: str, db):
    """Fetch most recent Restream clips and save them to a project."""
    from backend.models import ShortVideo, Project

    project = await db.get(Project, project_id)
    if not project:
        log.error(f"Project {project_id} not found")
        return

    token = await get_restream_token(db)
    if not token:
        log.error("No Restream token — user must connect first")
        return

    # 1. Get clip projects
    try:
        projects_resp = api_request("/user/clips/projects", token)
    except Exception as e:
        log.error(f"Failed to get clip projects: {e}")
        return

    clip_projects = projects_resp.get("projects", []) if isinstance(projects_resp, dict) else projects_resp
    if not clip_projects:
        log.info("No Restream clip projects found")
        return

    # Get the most recent clip project (should correspond to most recent live)
    latest = clip_projects[0]
    cp_id = latest.get("projectId") or latest.get("id")
    log.info(f"Latest clip project: {cp_id} — {latest.get('title')}")

    # 2. Get clips in that project
    details = api_request(f"/user/clips/projects/{cp_id}", token)
    clips = details.get("clips", [])

    # 3. Download each clip
    from backend.services.scheduler import send_telegram
    saved = 0
    for idx, clip in enumerate(clips, 1):
        clip_id = clip.get("clipId") or clip.get("id")
        title = clip.get("name") or clip.get("title") or f"Restream clip {idx}"

        # Fetch download URL
        try:
            dl = api_request(f"/user/clips/{clip_id}/download", token)
            download_url = dl.get("downloadUrl") or dl.get("url")
        except Exception as e:
            log.warning(f"Couldn't get download URL for {clip_id}: {e}")
            continue

        if not download_url:
            continue

        save_path = CLIPS_DIR / f"{project_id}-restream-{idx}.mp4"
        # Download URL is pre-signed (user-id + clip-id params) — no auth header needed
        if download_file(download_url, save_path, token=None):
            # Check if already in DB
            from sqlalchemy import select
            existing = await db.execute(
                select(ShortVideo).where(
                    ShortVideo.project_id == project_id,
                    ShortVideo.restream_clip_id == clip_id,
                )
            )
            if existing.scalar_one_or_none():
                continue

            sv = ShortVideo(
                project_id=project_id,
                source="restream",
                restream_clip_id=clip_id,
                title=title,
                file_path=str(save_path),
                status="draft",
            )
            db.add(sv)
            saved += 1
            log.info(f"✅ Restream clip {idx}: {title}")

    await db.commit()
    public_url = _load_env().get("PUBLIC_URL", "")
    msg = f"🎬 Pulled {saved} clips from Restream\n\n📌 {project.title}"
    if public_url:
        msg += f"\n\n👉 {public_url}"
    send_telegram(msg)
