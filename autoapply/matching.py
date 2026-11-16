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


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower())) - _STOP


def _has(term: str, text_lower: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", text_lower) is not None


def required_years(description: str) -> int | None:
    years = [int(m.group(1)) for m in _YEARS.finditer(description) if int(m.group(1)) <= 20]
    return min(years) if years else None


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
    job_tokens = _tokens(job.title)
    title_fit = 0.0
    for target in prefs.get("titles") or []:
        t = _tokens(target)
        if t:
            title_fit = max(title_fit, len(t & job_tokens) / len(t))
    reasons["title_fit"] = round(title_fit, 2)

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

    score = round(45 * title_fit + 40 * skill_fit + 15 * loc_fit)
    if title_fit == 0:
        score = min(score, 35)  # right skills, wrong job family
    return score, reasons


def score_all(only_unscored: bool = False) -> int:
    profile = load_profile()
    with session() as s:
        q = select(Job)
        if only_unscored:
            q = q.where(Job.score.is_(None))
        jobs = s.scalars(q).all()
        for job in jobs:
            job.score, job.score_reasons = score_job(job, profile)
        return len(jobs)
