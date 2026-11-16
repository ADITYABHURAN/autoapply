"""Job discovery: poll each company's public ATS job feed and upsert new postings.

Greenhouse, Lever and Ashby all publish unauthenticated JSON feeds for their hosted
job boards, so no scraping is needed for these three.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx
import yaml
from sqlalchemy import select

from ..config import settings
from ..db import Job, session

UA = {"User-Agent": "autoapply/0.1 (personal job search tool)"}


@dataclass
class Posting:
    ats: str
    company: str
    external_id: str
    title: str
    location: str
    remote: bool
    url: str
    apply_url: str
    description: str
    posted_at: Optional[datetime]


def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _parse_dt(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, timezone.utc).replace(tzinfo=None)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except (ValueError, OSError):
        return None


def _is_remote(*texts: str) -> bool:
    return any("remote" in (t or "").lower() for t in texts)


def fetch_greenhouse(token: str, client: httpx.Client) -> list[Posting]:
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", params={"content": "true"})
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(Posting(
            ats="greenhouse", company=token, external_id=str(j["id"]), title=j.get("title", "").strip(),
            location=loc, remote=_is_remote(loc, j.get("title", "")),
            url=j.get("absolute_url", ""),
            apply_url=f"https://job-boards.greenhouse.io/{token}/jobs/{j['id']}",
            description=strip_html(j.get("content", "")),
            posted_at=_parse_dt(j.get("first_published") or j.get("updated_at")),
        ))
    return out


def fetch_lever(company: str, client: httpx.Client) -> list[Posting]:
    r = client.get(f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"})
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ""
        desc = j.get("descriptionPlain") or strip_html(j.get("description", ""))
        for block in j.get("lists") or []:
            desc += f"\n\n{block.get('text', '')}\n{strip_html(block.get('content', ''))}"
        out.append(Posting(
            ats="lever", company=company, external_id=j["id"], title=j.get("text", "").strip(),
            location=loc, remote=(j.get("workplaceType") == "remote") or _is_remote(loc),
            url=j.get("hostedUrl", ""), apply_url=j.get("applyUrl") or j.get("hostedUrl", "") + "/apply",
            description=desc.strip(), posted_at=_parse_dt(j.get("createdAt")),
        ))
    return out


def fetch_ashby(board: str, client: httpx.Client) -> list[Posting]:
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}", params={"includeCompensation": "true"})
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location", "") or ""
        url = j.get("jobUrl", "")
        out.append(Posting(
            ats="ashby", company=board, external_id=j["id"], title=j.get("title", "").strip(),
            location=loc, remote=bool(j.get("isRemote")) or _is_remote(loc),
            url=url, apply_url=j.get("applyUrl") or url.rstrip("/") + "/application",
            description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
            posted_at=_parse_dt(j.get("publishedAt")),
        ))
    return out


FETCHERS: dict[str, Callable[[str, httpx.Client], list[Posting]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def parse_job_url(url: str) -> Optional[tuple[str, str, Optional[str]]]:
    """Recognize a pasted job link. Returns (ats, company, external_id) or None."""
    u = urlparse(url)
    host, parts = u.netloc.lower(), [p for p in u.path.split("/") if p]
    if "greenhouse.io" in host and parts:
        m = re.search(r"/jobs/(\d+)", u.path)
        gh_jid = re.search(r"gh_jid=(\d+)", u.query)
        company = parts[1] if parts[0] == "embed" and len(parts) > 1 else parts[0]
        return ("greenhouse", company, m.group(1) if m else gh_jid.group(1) if gh_jid else None)
    if host == "jobs.lever.co" and parts:
        return ("lever", parts[0], parts[1] if len(parts) > 1 else None)
    if host == "jobs.ashbyhq.com" and parts:
        return ("ashby", parts[0], parts[1] if len(parts) > 1 else None)
    return None


def load_companies() -> dict[str, list[str]]:
    path = settings.companies_path
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {ats: list(dict.fromkeys(v or [])) for ats, v in data.items() if ats in FETCHERS}


def upsert(postings: list[Posting]) -> list[int]:
    """Insert new postings, refresh existing ones. Returns ids of newly seen jobs."""
    new_ids = []
    with session() as s:
        for p in postings:
            job = s.scalar(select(Job).where(Job.ats == p.ats, Job.company == p.company, Job.external_id == p.external_id))
            if job is None:
                job = Job(**p.__dict__)
                s.add(job)
                s.flush()
                new_ids.append(job.id)
            else:
                job.title, job.location, job.description = p.title, p.location, p.description
    return new_ids


def discover(companies: dict[str, list[str]] | None = None, log=print) -> list[int]:
    companies = companies if companies is not None else load_companies()
    new_ids: list[int] = []
    with httpx.Client(headers=UA, timeout=30, follow_redirects=True) as client:
        for ats, names in companies.items():
            for name in names:
                try:
                    postings = FETCHERS[ats](name, client)
                except httpx.HTTPError as e:
                    log(f"  {ats}/{name}: failed ({e})")
                    continue
                fresh = upsert(postings)
                new_ids.extend(fresh)
                log(f"  {ats}/{name}: {len(postings)} open, {len(fresh)} new")
    return new_ids


def add_job_from_url(url: str) -> Optional[int]:
    """Add a single job from a pasted link. Returns the job id, or None if unsupported/not found."""
    parsed = parse_job_url(url)
    if not parsed:
        return None
    ats, company, ext_id = parsed
    with httpx.Client(headers=UA, timeout=30, follow_redirects=True) as client:
        postings = FETCHERS[ats](company, client)
    if ext_id:
        postings = [p for p in postings if p.external_id == ext_id]
    if len(postings) != 1:
        return None
    upsert(postings)
    with session() as s:
        p = postings[0]
        return s.scalar(select(Job.id).where(Job.ats == p.ats, Job.company == p.company, Job.external_id == p.external_id))
