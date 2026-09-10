from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import get_db
from backend.models import Project, Topic, Bullet
from backend.auth import require_auth
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/projects", dependencies=[Depends(require_auth)])


class ProjectCreate(BaseModel):
    title: str


class ProjectUpdate(BaseModel):
    title: Optional[str] = None


class TopicCreate(BaseModel):
    title: str
    sort_order: int


class TopicUpdate(BaseModel):
    title: Optional[str] = None
    sort_order: Optional[int] = None


class BulletCreate(BaseModel):
    text: str
    sort_order: int
    level: int = 0


class BulletUpdate(BaseModel):
    text: Optional[str] = None
    sort_order: Optional[int] = None
    level: Optional[int] = None


def serialize_project(p, topics=None):
    d = {
        "id": p.id,
        "title": p.title,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }
    if topics is not None:
        d["topics"] = topics
    return d


def serialize_topic(t, bullets=None):
    d = {"id": t.id, "project_id": t.project_id, "sort_order": t.sort_order, "title": t.title}
    if bullets is not None:
        d["bullets"] = bullets
    return d


def serialize_bullet(b):
    return {"id": b.id, "topic_id": b.topic_id, "sort_order": b.sort_order, "text": b.text, "level": b.level or 0}


@router.get("")
async def list_projects(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).order_by(Project.created_at.desc()))
    projects = result.scalars().all()
    return [serialize_project(p) for p in projects]


@router.post("")
async def create_project(body: ProjectCreate, db: AsyncSession = Depends(get_db)):
    p = Project(title=body.title)
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

    return serialize_project(p, topic_list)


@router.put("/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate, db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "Project not found")
    if body.title is not None:
        p.title = body.title
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
    b = Bullet(topic_id=topic_id, text=body.text, sort_order=body.sort_order, level=max(0, body.level))
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
    if body.level is not None:
        b.level = max(0, body.level)
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
