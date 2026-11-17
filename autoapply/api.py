"""Local web API and dashboard. One background thread runs browser work, one application at a time."""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func, select

import yaml

from . import discovery, engine, matching
from .config import settings
from .db import TRACKER_STAGES, Application, Job, init_db, session
from .profile import TEMPLATE, load_profile, validate, write_template

app = FastAPI(title="AutoApply")
init_db()
write_template()
if not settings.companies_path.exists():
    import shutil
    shutil.copy(Path(__file__).parent / "companies.seed.yaml", settings.companies_path)

_work: "queue.Queue[tuple[str, Any]]" = queue.Queue()
_log: list[str] = []
_busy = threading.Event()


def _say(line: str) -> None:
    _log.append(str(line))
    del _log[:-200]


def _worker() -> None:
    while True:
        kind, arg = _work.get()
        _busy.set()
        try:
            if kind == "step":
                _say(f"Application #{arg}")
                engine.step(arg, _say)
            elif kind == "discover":
                _say("Checking career pages...")
                new = discovery.discover(log=_say)
                matching.score_all()
                _say(f"{len(new)} new jobs.")
            elif kind == "auto":
                discovery.discover(log=_say)
                matching.score_all()
                for app_id in engine.auto_select(_say):
                    _work.put(("step", app_id))
        except Exception as e:  # noqa: BLE001
            _say(f"error: {e}")
        finally:
            _busy.clear()
            _work.task_done()


threading.Thread(target=_worker, daemon=True).start()


def _job(j: Job) -> dict[str, Any]:
    return {"id": j.id, "ats": j.ats, "company": j.company, "title": j.title, "location": j.location,
            "remote": j.remote, "url": j.url, "score": j.score, "reasons": j.score_reasons,
            "posted_at": (j.posted_at or j.first_seen).isoformat()}


def _app(a: Application, full: bool = False) -> dict[str, Any]:
    out = {"id": a.id, "status": a.status, "stage": a.stage, "source": a.source, "error": a.error,
           "created_at": a.created_at.isoformat(),
           "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None, "job": _job(a.job),
           "has_screenshot": bool(a.screenshot and Path(a.screenshot).exists()),
           "has_resume": bool(a.resume_pdf and Path(a.resume_pdf).exists())}
    if full:
        out.update(resume=a.tailored_resume, resume_diff=a.resume_diff, cover_letter=a.cover_letter, answers=a.answers or [])
    return out


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (Path(__file__).parent / "web" / "index.html").read_text()


@app.get("/api/jobs")
def list_jobs(min_score: int = 0, q: str = "", limit: int = 100) -> list[dict]:
    with session() as s:
        stmt = select(Job).where(Job.dismissed.is_(False), func.coalesce(Job.score, 0) >= min_score,
                                 Job.id.notin_(select(Application.job_id)))
        if q:
            like = f"%{q}%"
            stmt = stmt.where(Job.title.ilike(like) | Job.company.ilike(like) | Job.location.ilike(like))
        rows = s.scalars(stmt.order_by(Job.score.desc(), Job.first_seen.desc()).limit(limit)).all()
        return [_job(j) for j in rows]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int) -> dict:
    with session() as s:
        j = s.get(Job, job_id)
        if not j:
            raise HTTPException(404)
        return {**_job(j), "description": j.description}


class UrlIn(BaseModel):
    url: str


@app.post("/api/jobs/add")
def add_job(body: UrlIn) -> dict:
    job_id = discovery.add_job_from_url(body.url)
    if job_id is None:
        raise HTTPException(400, "Unsupported link. Paste a Greenhouse, Lever or Ashby job URL.")
    matching.score_all(only_unscored=True)
    return {"job_id": job_id}


@app.post("/api/jobs/{job_id}/dismiss")
def dismiss(job_id: int) -> dict:
    with session() as s:
        s.get(Job, job_id).dismissed = True
    return {"ok": True}


@app.post("/api/jobs/{job_id}/apply")
def apply(job_id: int) -> dict:
    app_id = engine.enqueue(job_id)
    _work.put(("step", app_id))
    return {"application_id": app_id}


@app.post("/api/discover")
def run_discover() -> dict:
    _work.put(("discover", None))
    return {"ok": True}


@app.post("/api/auto")
def run_auto() -> dict:
    _work.put(("auto", None))
    return {"ok": True}


@app.get("/api/applications")
def list_apps() -> list[dict]:
    with session() as s:
        return [_app(a) for a in s.scalars(select(Application).order_by(Application.id.desc()).limit(300)).all()]


@app.get("/api/applications/{app_id}")
def get_app(app_id: int) -> dict:
    with session() as s:
        a = s.get(Application, app_id)
        if not a:
            raise HTTPException(404)
        return _app(a, full=True)


class ResumeApproval(BaseModel):
    resume: Optional[dict] = None
    cover_letter: Optional[str] = None


@app.post("/api/applications/{app_id}/approve-resume")
def approve_resume(app_id: int, body: ResumeApproval) -> dict:
    engine.approve_resume(app_id, body.resume, body.cover_letter)
    _work.put(("step", app_id))
    return {"ok": True}


class SubmitApproval(BaseModel):
    edits: dict[str, Any] = {}


@app.post("/api/applications/{app_id}/approve-submit")
def approve_submit(app_id: int, body: SubmitApproval) -> dict:
    engine.approve_submit(app_id, body.edits)
    _work.put(("step", app_id))
    return {"ok": True}


@app.post("/api/applications/{app_id}/retry")
def retry(app_id: int) -> dict:
    with session() as s:
        job_id = s.get(Application, app_id).job_id
    engine.enqueue(job_id)
    _work.put(("step", app_id))
    return {"ok": True}


@app.post("/api/applications/{app_id}/cancel")
def cancel(app_id: int) -> dict:
    with session() as s:
        a = s.get(Application, app_id)
        if a.status != "submitted":
            a.status = "cancelled"
    return {"ok": True}


class StageIn(BaseModel):
    stage: str


@app.post("/api/applications/{app_id}/stage")
def set_stage(app_id: int, body: StageIn) -> dict:
    if body.stage not in TRACKER_STAGES:
        raise HTTPException(400, f"stage must be one of {TRACKER_STAGES}")
    with session() as s:
        s.get(Application, app_id).stage = body.stage
    return {"ok": True}


@app.get("/api/applications/{app_id}/screenshot")
def screenshot(app_id: int):
    with session() as s:
        a = s.get(Application, app_id)
        if not a or not a.screenshot or not Path(a.screenshot).exists():
            raise HTTPException(404)
        return FileResponse(a.screenshot)


@app.get("/api/applications/{app_id}/resume.pdf")
def resume_pdf(app_id: int):
    with session() as s:
        a = s.get(Application, app_id)
        if not a or not a.resume_pdf or not Path(a.resume_pdf).exists():
            raise HTTPException(404)
        return FileResponse(a.resume_pdf, media_type="application/pdf")


@app.get("/api/profile")
def get_profile() -> dict:
    """The profile plus whatever the blank template defines, so new fields show up in the form."""
    base = yaml.safe_load(TEMPLATE)
    try:
        current = load_profile()
    except FileNotFoundError:
        current = {}
    merged = {**base, **current}
    return {"profile": merged, "problems": validate(merged)}


@app.post("/api/profile")
def save_profile(body: dict[str, Any]) -> dict:
    settings.profile_path.parent.mkdir(parents=True, exist_ok=True)
    settings.profile_path.write_text(yaml.safe_dump(body, sort_keys=False, allow_unicode=True))
    problems = validate(body)
    if not problems:
        from . import matching
        matching.score_all()  # new target titles/locations take effect immediately
    return {"ok": True, "problems": problems}


@app.get("/api/state")
def state() -> dict:
    with session() as s:
        counts = dict(s.execute(select(Application.status, func.count()).group_by(Application.status)).all())
        jobs_total = s.scalar(select(func.count()).select_from(Job)) or 0
    from . import llm
    return {"busy": _busy.is_set(), "queued_work": _work.qsize(), "log": _log[-40:], "counts": counts,
            "jobs_total": jobs_total, "submitted_today": engine.submitted_today(), "daily_cap": settings.daily_cap,
            "threshold": settings.match_threshold, "llm": llm.available(),
            "review_before_submit": settings.review_before_submit, "auto_approve_resume": settings.auto_approve_resume}
