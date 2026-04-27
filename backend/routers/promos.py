from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database import get_db
from backend.models import Promo
from backend.auth import require_auth
from pydantic import BaseModel
from typing import Optional

APP_DIR = Path(__file__).resolve().parent.parent.parent

router = APIRouter(prefix="/api/promos", dependencies=[Depends(require_auth)])


class PromoCreate(BaseModel):
    project_id: str
    label: Optional[str] = None
    caption_th: Optional[str] = None
    caption_en: Optional[str] = None
    image_path: Optional[str] = None
    scheduled_at: Optional[str] = None


class PromoUpdate(BaseModel):
    label: Optional[str] = None
    caption_th: Optional[str] = None
    caption_en: Optional[str] = None
    image_path: Optional[str] = None
    scheduled_at: Optional[str] = None
    status: Optional[str] = None


def serialize(p):
    return {
        "id": p.id, "project_id": p.project_id, "label": p.label,
        "caption_th": p.caption_th, "caption_en": p.caption_en,
        "image_path": p.image_path,
        "scheduled_at": p.scheduled_at.isoformat() if p.scheduled_at else None,
        "status": p.status,
        "fb_post_id_th": p.fb_post_id_th, "ig_post_id_th": p.ig_post_id_th,
        "fb_post_id_en": p.fb_post_id_en, "ig_post_id_en": p.ig_post_id_en,
        "posted_at": p.posted_at.isoformat() if p.posted_at else None,
    }


@router.get("")
async def list_promos(project_id: str = None, db: AsyncSession = Depends(get_db)):
    q = select(Promo).order_by(Promo.scheduled_at)
    if project_id:
        q = q.where(Promo.project_id == project_id)
    result = await db.execute(q)
    return [serialize(p) for p in result.scalars().all()]


@router.post("")
async def create_promo(body: PromoCreate, db: AsyncSession = Depends(get_db)):
    p = Promo(
        project_id=body.project_id,
        label=body.label,
        caption_th=body.caption_th,
        caption_en=body.caption_en,
        image_path=body.image_path,
        scheduled_at=datetime.fromisoformat(body.scheduled_at) if body.scheduled_at else None,
        status="scheduled" if body.scheduled_at else "draft",
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return serialize(p)


@router.put("/{promo_id}")
async def update_promo(promo_id: str, body: PromoUpdate, db: AsyncSession = Depends(get_db)):
    p = await db.get(Promo, promo_id)
    if not p:
        raise HTTPException(404, "Promo not found")
    if body.label is not None:
        p.label = body.label
    if body.caption_th is not None:
        p.caption_th = body.caption_th
    if body.caption_en is not None:
        p.caption_en = body.caption_en
    if body.image_path is not None:
        p.image_path = body.image_path
    if body.scheduled_at is not None:
        p.scheduled_at = datetime.fromisoformat(body.scheduled_at)
    if body.status is not None:
        p.status = body.status
    await db.commit()
    await db.refresh(p)
    return serialize(p)


@router.post("/{promo_id}/upload-image")
async def upload_promo_image(promo_id: str, file: UploadFile, db: AsyncSession = Depends(get_db)):
    from pathlib import Path
    p = await db.get(Promo, promo_id)
    if not p:
        raise HTTPException(404, "Promo not found")

    # Delete old image if exists
    if p.image_path:
        old = Path(p.image_path)
        if old.exists():
            old.unlink()

    # Save new image
    ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "png"
    media_dir = APP_DIR / "media"
    media_dir.mkdir(exist_ok=True)
    save_path = str(media_dir / f"promo-upload-{promo_id}.{ext}")
    content = await file.read()
    with open(save_path, "wb") as f:
        f.write(content)

    p.image_path = save_path
    await db.commit()
    await db.refresh(p)
    return serialize(p)


@router.delete("/{promo_id}/image")
async def delete_promo_image(promo_id: str, db: AsyncSession = Depends(get_db)):
    from pathlib import Path
    p = await db.get(Promo, promo_id)
    if not p:
        raise HTTPException(404, "Promo not found")

    if p.image_path:
        old = Path(p.image_path)
        if old.exists():
            old.unlink()
        p.image_path = None
        await db.commit()
    return serialize(p)


@router.delete("/{promo_id}")
async def delete_promo(promo_id: str, db: AsyncSession = Depends(get_db)):
    p = await db.get(Promo, promo_id)
    if not p:
        raise HTTPException(404, "Promo not found")
    await db.delete(p)
    await db.commit()
    return {"ok": True}
