from datetime import datetime, timezone
from sqlalchemy import Column, Text, Integer, DateTime, ForeignKey
from backend.database import Base
import uuid


def new_id():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"
    id = Column(Text, primary_key=True, default=new_id)
    title = Column(Text, nullable=False)
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
    level = Column(Integer, default=0)  # 0 = bullet, 1 = sub, 2 = sub-sub
