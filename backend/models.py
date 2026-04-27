from datetime import datetime, timezone
from sqlalchemy import Column, Text, Integer, Boolean, DateTime, ForeignKey
from backend.database import Base
import uuid


def new_id():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(Text, primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id = Column(Text, primary_key=True, default=new_id)
    title = Column(Text, nullable=False)
    live_date = Column(DateTime, nullable=False)
    cron_date = Column(DateTime, nullable=False)
    status = Column(Text, default="upcoming")  # upcoming / live / done
    approved_at = Column(DateTime, nullable=True)
    recap_documentor_id = Column(Text, nullable=True)
    recap_scheduled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class Topic(Base):
    __tablename__ = "topics"
    id = Column(Text, primary_key=True, default=new_id)
    project_id = Column(Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    sort_order = Column(Integer, nullable=False)
    title = Column(Text, nullable=False)


class Bullet(Base):
    __tablename__ = "bullets"
    id = Column(Text, primary_key=True, default=new_id)
    topic_id = Column(Text, ForeignKey("topics.id", ondelete="CASCADE"), nullable=False)
    sort_order = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    is_sub = Column(Boolean, default=False)


class Promo(Base):
    __tablename__ = "promos"
    id = Column(Text, primary_key=True, default=new_id)
    project_id = Column(Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    label = Column(Text, nullable=True)  # e.g. "อา. โปรโมท", "จ. โปรโมท"
    caption_th = Column(Text, nullable=True)
    caption_en = Column(Text, nullable=True)
    image_path = Column(Text, nullable=True)
    scheduled_at = Column(DateTime, nullable=True)
    status = Column(Text, default="draft")  # draft / scheduled / posted / failed
    fb_post_id_th = Column(Text, nullable=True)
    ig_post_id_th = Column(Text, nullable=True)
    fb_post_id_en = Column(Text, nullable=True)
    ig_post_id_en = Column(Text, nullable=True)
    posted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class ShortVideo(Base):
    __tablename__ = "short_videos"
    id = Column(Text, primary_key=True, default=new_id)
    project_id = Column(Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    source = Column(Text, default="tim")  # tim / restream
    restream_clip_id = Column(Text, nullable=True)
    title = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    file_path = Column(Text, nullable=True)
    file_path_en = Column(Text, nullable=True)
    scheduled_at = Column(DateTime, nullable=True)
    status = Column(Text, default="draft")  # draft / scheduled / posted / failed
    fb_post_id_th = Column(Text, nullable=True)
    ig_post_id_th = Column(Text, nullable=True)
    fb_post_id_en = Column(Text, nullable=True)
    ig_post_id_en = Column(Text, nullable=True)
    upload_post_job_id = Column(Text, nullable=True)
    posted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)
