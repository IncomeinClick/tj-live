import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import get_db
from backend.models import Project, Topic, Bullet, Promo, ShortVideo
from backend.auth import require_auth
from pydantic import BaseModel
from typing import Optional

APP_DIR = Path(__file__).resolve().parent.parent.parent
PYTHON_BIN = sys.executable
LOG_DIR = Path(os.environ.get("TJ_LOG_DIR", "/var/log"))


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


router = APIRouter(prefix="/api/projects", dependencies=[Depends(require_auth)])


class ProjectCreate(BaseModel):
    title: str
    live_date: Optional[str] = None  # ISO datetime (optional — unused for slide-mode lives)


class ProjectUpdate(BaseModel):
    title: Optional[str] = None
    live_date: Optional[str] = None
    status: Optional[str] = None


class TopicCreate(BaseModel):
    title: str
    sort_order: int


class TopicUpdate(BaseModel):
    title: Optional[str] = None
    sort_order: Optional[int] = None


class BulletCreate(BaseModel):
    text: str
    sort_order: int
    is_sub: bool = False


class BulletUpdate(BaseModel):
    text: Optional[str] = None
    sort_order: Optional[int] = None
    is_sub: Optional[bool] = None


def compute_cron_date(live_date: datetime) -> datetime:
    return live_date + timedelta(hours=6)


def serialize_project(p, topics=None, promos=None, videos=None):
    d = {
        "id": p.id,
        "title": p.title,
        "live_date": p.live_date.isoformat() if p.live_date else None,
        "cron_date": p.cron_date.isoformat() if p.cron_date else None,
        "status": p.status,
        "approved_at": p.approved_at.isoformat() if p.approved_at else None,
        "recap_documentor_id": p.recap_documentor_id,
        "recap_scheduled_at": p.recap_scheduled_at.isoformat() if p.recap_scheduled_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }
    if topics is not None:
        d["topics"] = topics
    if promos is not None:
        d["promos"] = promos
    if videos is not None:
        d["videos"] = videos
    return d


def serialize_topic(t, bullets=None):
    d = {"id": t.id, "project_id": t.project_id, "sort_order": t.sort_order, "title": t.title}
    if bullets is not None:
        d["bullets"] = bullets
    return d


def serialize_bullet(b):
    return {"id": b.id, "topic_id": b.topic_id, "sort_order": b.sort_order, "text": b.text, "is_sub": b.is_sub}


@router.get("")
async def list_projects(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).order_by(Project.created_at.desc()))
    projects = result.scalars().all()
    return [serialize_project(p) for p in projects]


@router.post("")
async def create_project(body: ProjectCreate, db: AsyncSession = Depends(get_db)):
    live_dt = datetime.fromisoformat(body.live_date) if body.live_date else None
    cron_dt = compute_cron_date(live_dt) if live_dt else None
    p = Project(title=body.title, live_date=live_dt, cron_date=cron_dt)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return serialize_project(p)


@router.get("/{project_id}")
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "Project not found")

    # Get topics with bullets
    topics_result = await db.execute(
        select(Topic).where(Topic.project_id == project_id).order_by(Topic.sort_order)
    )
    topics = topics_result.scalars().all()

    topic_list = []
    for t in topics:
        bullets_result = await db.execute(
            select(Bullet).where(Bullet.topic_id == t.id).order_by(Bullet.sort_order)
        )
        bullets = [serialize_bullet(b) for b in bullets_result.scalars().all()]
        topic_list.append(serialize_topic(t, bullets))

    # Get promos
    promos_result = await db.execute(
        select(Promo).where(Promo.project_id == project_id).order_by(Promo.scheduled_at)
    )
    promos = [
        {
            "id": pr.id, "label": pr.label, "caption_th": pr.caption_th,
            "caption_en": pr.caption_en, "image_path": pr.image_path,
            "scheduled_at": pr.scheduled_at.isoformat() if pr.scheduled_at else None,
            "status": pr.status, "posted_at": pr.posted_at.isoformat() if pr.posted_at else None,
        }
        for pr in promos_result.scalars().all()
    ]

    # Get short videos
    videos_result = await db.execute(
        select(ShortVideo).where(ShortVideo.project_id == project_id).order_by(ShortVideo.scheduled_at)
    )
    videos = [
        {
            "id": v.id, "source": v.source, "title": v.title, "file_path": v.file_path,
            "file_path_en": v.file_path_en,
            "scheduled_at": (v.scheduled_at.isoformat() + "Z") if v.scheduled_at else None,
            "status": v.status,
        }
        for v in videos_result.scalars().all()
    ]

    return serialize_project(p, topic_list, promos, videos)


@router.put("/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate, db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "Project not found")
    if body.title is not None:
        p.title = body.title
    if body.live_date is not None:
        live_dt = datetime.fromisoformat(body.live_date)
        p.live_date = live_dt
        p.cron_date = compute_cron_date(live_dt)
    if body.status is not None:
        p.status = body.status
    p.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(p)
    return serialize_project(p)


@router.delete("/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "Project not found")
    await db.delete(p)
    await db.commit()
    return {"ok": True}


@router.post("/{project_id}/cut-clips")
async def cut_clips(project_id: str, db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "Project not found")

    import subprocess
    subprocess.Popen(
        [PYTHON_BIN, str(APP_DIR / "cut_clips_runner.py"), project_id],
        stdout=open(LOG_DIR / f"tj-live-clips-{project_id}.log", "w"),
        stderr=subprocess.STDOUT,
        cwd=str(APP_DIR),
    )
    return {"ok": True, "message": "Cutting clips..."}


@router.post("/{project_id}/fetch-restream-clips")
async def fetch_restream_clips(project_id: str, db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "Project not found")

    import subprocess
    subprocess.Popen(
        [PYTHON_BIN, str(APP_DIR / "fetch_restream_runner.py"), project_id],
        stdout=open(LOG_DIR / f"tj-live-restream-{project_id}.log", "w"),
        stderr=subprocess.STDOUT,
        cwd=str(APP_DIR),
    )
    return {"ok": True, "message": "Fetching clips from Restream..."}


@router.post("/{project_id}/auto-cut-schedule")
async def auto_cut_schedule(project_id: str, db: AsyncSession = Depends(get_db)):
    """Mark Done → full pipeline: VOD → 7 clips → upload-post schedule."""
    p = await db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "Project not found")
    if p.status == "processing":
        raise HTTPException(409, "Pipeline already running for this project")

    p.status = "processing"
    p.updated_at = datetime.now(timezone.utc)
    await db.commit()

    import subprocess
    subprocess.Popen(
        [PYTHON_BIN, str(APP_DIR / "auto_cut_schedule_runner.py"), project_id],
        stdout=open(LOG_DIR / f"tj-live-autocut-{project_id}.log", "w"),
        stderr=subprocess.STDOUT,
        cwd=str(APP_DIR),
    )
    return {"ok": True, "message": "Pipeline started — Telegram when done (~2 hours)"}


@router.post("/{project_id}/approve")
async def approve_project(project_id: str, db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "Project not found")
    if p.approved_at:
        raise HTTPException(400, "Already approved")

    p.approved_at = datetime.now(timezone.utc)
    p.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(p)

    # Trigger promo creation via codex in background (subscription auth, NOT API key).
    # Agentic task (reads a skill + calls the API) → danger-full-access. Only if PROMO_SKILL_PATH set + codex installed.
    env = _load_env()
    skill_path = env.get("PROMO_SKILL_PATH", "")
    codex_node_bin = "/root/.nvm/versions/node/v22.22.3/bin"
    codex_bin = f"{codex_node_bin}/codex"
    if skill_path and Path(codex_bin).exists():
        import subprocess
        prompt = (
            f"A TJ Live project was just approved. "
            f"Project ID: {project_id}. "
            f"Read the skill at {skill_path} and follow it exactly. "
            f"The skill tells you to read project data from the API — do that to get the latest topics/bullets and live_date."
        )
        codex_env = {**os.environ, "PATH": codex_node_bin + ":" + os.environ.get("PATH", ""), "HOME": "/root"}
        subprocess.Popen(
            [codex_bin, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "danger-full-access",
             "-m", "gpt-5.4", "-c", "model_reasoning_effort=low", "-c", "approval_policy=never", prompt],
            stdout=open(LOG_DIR / f"tj-live-promo-{project_id}.log", "w"),
            stderr=subprocess.STDOUT,
            cwd=str(APP_DIR),
            env=codex_env,
        )

    return {**serialize_project(p), "message": "Approved!"}


# ── Topics ──

@router.post("/{project_id}/topics")
async def create_topic(project_id: str, body: TopicCreate, db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "Project not found")
    t = Topic(project_id=project_id, title=body.title, sort_order=body.sort_order)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return serialize_topic(t, [])


@router.put("/topics/{topic_id}")
async def update_topic(topic_id: str, body: TopicUpdate, db: AsyncSession = Depends(get_db)):
    t = await db.get(Topic, topic_id)
    if not t:
        raise HTTPException(404, "Topic not found")
    if body.title is not None:
        t.title = body.title
    if body.sort_order is not None:
        t.sort_order = body.sort_order
    await db.commit()
    await db.refresh(t)
    return serialize_topic(t)


@router.delete("/topics/{topic_id}")
async def delete_topic(topic_id: str, db: AsyncSession = Depends(get_db)):
    t = await db.get(Topic, topic_id)
    if not t:
        raise HTTPException(404, "Topic not found")
    await db.delete(t)
    await db.commit()
    return {"ok": True}


class ReorderItem(BaseModel):
    id: str
    sort_order: int


@router.put("/{project_id}/reorder-topics")
async def reorder_topics(project_id: str, items: list[ReorderItem], db: AsyncSession = Depends(get_db)):
    for item in items:
        t = await db.get(Topic, item.id)
        if t and t.project_id == project_id:
            t.sort_order = item.sort_order
    await db.commit()
    return {"ok": True}


@router.put("/topics/{topic_id}/reorder-bullets")
async def reorder_bullets(topic_id: str, items: list[ReorderItem], db: AsyncSession = Depends(get_db)):
    for item in items:
        b = await db.get(Bullet, item.id)
        if b and b.topic_id == topic_id:
            b.sort_order = item.sort_order
    await db.commit()
    return {"ok": True}


# ── Bullets ──

@router.post("/topics/{topic_id}/bullets")
async def create_bullet(topic_id: str, body: BulletCreate, db: AsyncSession = Depends(get_db)):
    t = await db.get(Topic, topic_id)
    if not t:
        raise HTTPException(404, "Topic not found")
    b = Bullet(topic_id=topic_id, text=body.text, sort_order=body.sort_order, is_sub=body.is_sub)
    db.add(b)
    await db.commit()
    await db.refresh(b)
    return serialize_bullet(b)


@router.put("/bullets/{bullet_id}")
async def update_bullet(bullet_id: str, body: BulletUpdate, db: AsyncSession = Depends(get_db)):
    b = await db.get(Bullet, bullet_id)
    if not b:
        raise HTTPException(404, "Bullet not found")
    if body.text is not None:
        b.text = body.text
    if body.sort_order is not None:
        b.sort_order = body.sort_order
    if body.is_sub is not None:
        b.is_sub = body.is_sub
    await db.commit()
    await db.refresh(b)
    return serialize_bullet(b)


@router.delete("/bullets/{bullet_id}")
async def delete_bullet(bullet_id: str, db: AsyncSession = Depends(get_db)):
    b = await db.get(Bullet, bullet_id)
    if not b:
        raise HTTPException(404, "Bullet not found")
    await db.delete(b)
    await db.commit()
    return {"ok": True}
