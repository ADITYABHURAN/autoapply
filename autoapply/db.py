"""Database models. SQLite by default; set DATABASE_URL for Postgres."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("ats", "company", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ats: Mapped[str] = mapped_column(String(32))
    company: Mapped[str] = mapped_column(String(128))
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(512))
    location: Mapped[str] = mapped_column(String(512), default="")
    remote: Mapped[bool] = mapped_column(default=False)
    url: Mapped[str] = mapped_column(String(1024))
    apply_url: Mapped[str] = mapped_column(String(1024))
    description: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    score_reasons: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    dismissed: Mapped[bool] = mapped_column(default=False)

    applications: Mapped[list["Application"]] = relationship(back_populates="job")


# Application lifecycle:
#   queued -> tailoring -> awaiting_resume_approval -> ready_to_fill -> filling
#   -> awaiting_submit_approval -> submitting -> submitted
#   Any step can end in: failed, needs_manual (CAPTCHA/login wall), cancelled
STATUSES = (
    "queued", "tailoring", "awaiting_resume_approval", "ready_to_fill", "filling",
    "awaiting_submit_approval", "submitting", "submitted", "failed", "needs_manual", "cancelled",
)
TRACKER_STAGES = ("applied", "interviewing", "offer", "rejected", "ghosted")


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    status: Mapped[str] = mapped_column(String(40), default="queued")
    stage: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")  # manual | auto
    tailored_resume: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    resume_diff: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resume_pdf: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    cover_letter: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    answers: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # the receipt: every field + value
    screenshot: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    job: Mapped[Job] = relationship(back_populates="applications")


class SavedAnswer(Base):
    """Answers you've approved or edited, reused on later forms before asking the LLM."""
    __tablename__ = "saved_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_key: Mapped[str] = mapped_column(String(512), unique=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)


_engine = None
_Session = None


def init_db(url: str | None = None):
    global _engine, _Session
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(url or settings.database_url, future=True)
    Base.metadata.create_all(_engine)
    _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session() -> Iterator[Session]:
    if _Session is None:
        init_db()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
