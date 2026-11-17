"""Score jobs 0-100 against your profile. Deterministic and free: no LLM calls.

score = 45 * title fit + 40 * skill coverage + 15 * location fit, with hard filters
(excluded keywords, too many years required) that force the score to 0.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from .db import Job, session
from .profile import load_profile

_WORD = re.compile(r"[a-z0-9+#.]+")
_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years|yrs)", re.I)
_STOP = {"and", "of", "the", "a", "i", "ii", "iii", "sr", "senior", "junior", "jr", "new", "grad"}


_GENERIC = {"engineer", "specialist", "analyst", "technician", "associate", "administrator", "developer",
            "product", "customer", "technical", "operations", "services", "service", "user", "level", "tier"}


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower())) - _STOP


def _has(term: str, text_lower: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", text_lower) is not None


def required_years(description: str) -> int | None:
    years = [int(m.group(1)) for m in _YEARS.finditer(description) if int(m.group(1)) <= 20]
    return min(years) if years else None


def title_fit(title: str, prefs: dict[str, Any]) -> float:
    """Best token overlap between a job title and any target title (0-1)."""
    job_tokens, best = _tokens(title), 0.0
    for target in prefs.get("titles") or []:
        t = _tokens(target)
        shared = t & job_tokens
        # Sharing only job-family filler ("Product ... Specialist") is not a match.
        if t and (shared - _GENERIC or not t - _GENERIC):
            best = max(best, len(shared) / len(t))
    return best


def title_targeted(title: str, prefs: dict[str, Any]) -> bool:
    """True when a title is one of the roles you target and not excluded. Decides what gets stored."""
    title_l = title.lower()
    if any(_has(w, title_l) for w in prefs.get("exclude_keywords") or []):
        return False
    return title_fit(title, prefs) >= 0.6


def score_job(job: Job, profile: dict[str, Any]) -> tuple[int, dict]:
    prefs = profile["preferences"]
    title_l, desc_l = job.title.lower(), (job.description or "").lower()
    reasons: dict[str, Any] = {}

    for word in prefs.get("exclude_keywords") or []:
        if _has(word, title_l):
            return 0, {"excluded": f"title contains '{word}'"}
    years = required_years(job.description or "")
    max_years = prefs.get("max_years_experience")
    if years and max_years and years > int(max_years):
        return 0, {"excluded": f"asks for {years}+ years"}

    # Title fit: best token overlap with any target title.
    fit = title_fit(job.title, prefs)
    reasons["title_fit"] = round(fit, 2)

    # Skill coverage: share of your skills the description mentions, saturating at 8 hits.
    skills = profile["skills"] + (prefs.get("keywords") or [])
    hits = [s for s in skills if _has(s, desc_l) or _has(s, title_l)]
    skill_fit = min(len(hits) / 8, 1.0) if skills else 0.0
    reasons["matched_skills"] = hits[:15]

    # Location fit.
    loc_l = (job.location or "").lower()
    wanted = [l.lower() for l in prefs.get("locations") or []]
    if job.remote and prefs.get("remote_ok", True):
        loc_fit = 1.0
    elif any(w in loc_l for w in wanted):
        loc_fit = 1.0
    elif prefs.get("willing_to_relocate") and ("united states" in loc_l or re.search(r",\s*[A-Z]{2}\b", job.location or "")):
        loc_fit = 0.6
    else:
        loc_fit = 0.0 if wanted else 0.5
    reasons["location_fit"] = loc_fit

    score = round(45 * fit + 40 * skill_fit + 15 * loc_fit)
    if fit == 0:
        score = min(score, 35)  # right skills, wrong job family
    elif fit < 0.6:
        score = min(score, 55)  # shares only a generic word ("Engineer") with your target titles
    return score, reasons


def score_all(only_unscored: bool = False) -> int:
    profile = load_profile()
    # Score in short transactions and write only rows that changed, so a rescore of tens of
    # thousands of jobs never holds the database lock for long.
    with session() as s:
        q = select(Job.id)
        if only_unscored:
            q = q.where(Job.score.is_(None))
        ids = list(s.scalars(q).all())
    for i in range(0, len(ids), 500):
        with session() as s:
            for job in s.scalars(select(Job).where(Job.id.in_(ids[i:i + 500]))).all():
                score, reasons = score_job(job, profile)
                if job.score != score or job.score_reasons != reasons:
                    job.score, job.score_reasons = score, reasons
    return len(ids)


def prune() -> int:
    """Delete stored jobs that are not a targeted role (jobs you applied to are always kept)."""
    from sqlalchemy import delete, text

    from .db import Application
    prefs = load_profile()["preferences"]
    with session() as s:
        applied = set(s.scalars(select(Application.job_id)).all())
        rows = s.execute(select(Job.id, Job.title, Job.ats, Job.company)).all()
    from .discovery import load_companies
    watched = {(ats, str(c)) for ats, names in load_companies().items() for c in names}
    drop = [i for i, title, ats, company in rows if i not in applied
            and (not title_targeted(title, prefs) or (watched and (ats, company) not in watched))]
    for i in range(0, len(drop), 500):
        with session() as s:
            s.execute(delete(Job).where(Job.id.in_(drop[i:i + 500])))
    if drop:
        from . import db
        with db._engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            try:
                conn.execute(text("VACUUM"))
            except Exception:  # noqa: BLE001 - space is reclaimed later; not worth failing over
                pass
    return len(drop)
