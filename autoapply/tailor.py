"""Résumé tailoring and cover letters.

The LLM may only reword and reorder what is already in your profile. After the model
replies, `enforce_facts` puts every employer, title, date, school and degree back to the
profile's values and drops any skill you didn't list, so a rewrite can't invent history.
"""
from __future__ import annotations

import copy
import difflib
import html
import json
from pathlib import Path
from typing import Any

from . import llm
from .profile import full_name

TAILOR_SYSTEM = """You tailor a résumé to a job description.
Rules:
- Use ONLY facts present in the candidate's résumé JSON. Never add employers, titles, dates, degrees, tools, metrics or achievements that are not there.
- You may reword bullets to use the job description's vocabulary where the underlying fact supports it, reorder bullets so the most relevant come first, drop the least relevant bullets, reorder skills, and rewrite the summary.
- Keep each bullet to one or two lines, starting with a strong verb. Keep every number exactly as given.
- Return JSON only, with the same shape as the input: {"summary": str, "skills": [str], "experience": [{"company","title","location","start","end","bullets":[str]}], "education": [...], "projects": [{"name","bullets":[str]}]}"""

COVER_SYSTEM = """You write a short cover letter (150-220 words, 3 paragraphs, no header or address block, no placeholders).
Use only facts from the candidate's résumé and notes. Be specific about why this role fits the candidate's actual experience. Plain, direct tone; no clichés like "I am writing to express my interest". End with the candidate's name."""


def base_resume(profile: dict[str, Any]) -> dict[str, Any]:
    return {k: copy.deepcopy(profile.get(k)) for k in ("summary", "skills", "experience", "education", "projects")}


def enforce_facts(tailored: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Restore immutable facts from the profile and drop anything the model added."""
    base = base_resume(profile)
    out: dict[str, Any] = {"summary": str(tailored.get("summary") or base["summary"] or "")}

    allowed = {s.lower(): s for s in base["skills"]}
    skills = [allowed[s.lower()] for s in tailored.get("skills") or [] if isinstance(s, str) and s.lower() in allowed]
    out["skills"] = list(dict.fromkeys(skills)) or base["skills"]

    by_company = {(e.get("company", ""), e.get("start", "")): e for e in tailored.get("experience") or [] if isinstance(e, dict)}
    out["experience"] = []
    for orig in base["experience"]:
        new = by_company.get((orig.get("company", ""), orig.get("start", "")), {})
        bullets = [b for b in new.get("bullets") or [] if isinstance(b, str) and b.strip()]
        out["experience"].append({**orig, "bullets": bullets or orig.get("bullets") or []})

    out["education"] = base["education"]

    by_name = {p.get("name", ""): p for p in tailored.get("projects") or [] if isinstance(p, dict)}
    out["projects"] = []
    for orig in base["projects"] or []:
        new = by_name.get(orig.get("name", ""), {})
        bullets = [b for b in new.get("bullets") or [] if isinstance(b, str) and b.strip()]
        out["projects"].append({**orig, "bullets": bullets or orig.get("bullets") or []})
    return out


def tailor_resume(profile: dict[str, Any], job_title: str, company: str, description: str) -> dict[str, Any]:
    user = (
        f"JOB: {job_title} at {company}\n\nJOB DESCRIPTION:\n{description[:8000]}\n\n"
        f"CANDIDATE RÉSUMÉ JSON:\n{json.dumps(base_resume(profile), indent=1)}"
    )
    return enforce_facts(llm.complete_json(TAILOR_SYSTEM, user, max_tokens=5000), profile)


def write_cover_letter(profile: dict[str, Any], resume: dict[str, Any], job_title: str, company: str, description: str) -> str:
    user = (
        f"CANDIDATE: {full_name(profile)}\nNOTES: {profile.get('about_me', '')}\n\n"
        f"RÉSUMÉ JSON:\n{json.dumps(resume, indent=1)}\n\nJOB: {job_title} at {company}\n\n{description[:6000]}"
    )
    return llm.complete(COVER_SYSTEM, user, max_tokens=700).strip()


def resume_lines(resume: dict[str, Any]) -> list[str]:
    lines = [resume.get("summary", ""), "SKILLS: " + ", ".join(resume.get("skills") or [])]
    for e in resume.get("experience") or []:
        lines.append(f"## {e.get('title', '')} | {e.get('company', '')}")
        lines.extend(f"- {b}" for b in e.get("bullets") or [])
    for p in resume.get("projects") or []:
        lines.append(f"## {p.get('name', '')}")
        lines.extend(f"- {b}" for b in p.get("bullets") or [])
    return lines


def diff(profile: dict[str, Any], tailored: dict[str, Any]) -> str:
    return "\n".join(difflib.unified_diff(
        resume_lines(base_resume(profile)), resume_lines(tailored), "base", "tailored", lineterm="", n=1))


def render_html(profile: dict[str, Any], resume: dict[str, Any]) -> str:
    def e(value) -> str:  # YAML gives ints for years
        return html.escape(str(value if value is not None else ""))

    p = profile["personal"]
    contact = " | ".join(e(x) for x in (
        p.get("email"), p.get("phone"), ", ".join(x for x in (p.get("city"), p.get("state")) if x),
        p.get("linkedin"), p.get("github"), p.get("website")) if x)
    parts = [f"<h1>{e(full_name(profile))}</h1><p class=c>{contact}</p>"]
    if resume.get("summary"):
        parts.append(f"<h2>Summary</h2><p>{e(resume['summary'])}</p>")
    if resume.get("skills"):
        parts.append(f"<h2>Skills</h2><p>{e(', '.join(resume['skills']))}</p>")
    if resume.get("experience"):
        parts.append("<h2>Experience</h2>")
        for x in resume["experience"]:
            parts.append(
                f"<div class=r><b>{e(x.get('title', ''))}</b>, {e(x.get('company', ''))}"
                f"<span>{e(x.get('start', ''))} - {e(x.get('end', ''))}</span></div>"
                f"<div class=l>{e(x.get('location', '') or '')}</div><ul>"
                + "".join(f"<li>{e(b)}</li>" for b in x.get("bullets") or []) + "</ul>")
    if resume.get("projects"):
        parts.append("<h2>Projects</h2>")
        for x in resume["projects"]:
            parts.append(f"<div class=r><b>{e(x.get('name', ''))}</b></div><ul>"
                         + "".join(f"<li>{e(b)}</li>" for b in x.get("bullets") or []) + "</ul>")
    if resume.get("education"):
        parts.append("<h2>Education</h2>")
        for x in resume["education"]:
            deg = " ".join(v for v in (x.get("degree"), x.get("field")) if v)
            gpa = f" (GPA {e(str(x['gpa']))})" if x.get("gpa") else ""
            parts.append(f"<div class=r><span><b>{e(x.get('school', ''))}</b>, {e(deg)}{gpa}</span>"
                         f"<span>{e(str(x.get('start', '') or ''))} - {e(str(x.get('end', '') or ''))}</span></div>")
    # Single column, real text, standard headings: parses cleanly in ATS résumé parsers.
    css = ("body{font:10.5pt/1.35 Arial,Helvetica,sans-serif;color:#111;margin:0}"
           "h1{font-size:19pt;margin:0;text-align:center}.c{text-align:center;margin:2pt 0 6pt;font-size:9.5pt}"
           "h2{font-size:11pt;text-transform:uppercase;border-bottom:1px solid #444;margin:9pt 0 4pt;padding-bottom:1pt}"
           "p{margin:0 0 3pt}.r{display:flex;justify-content:space-between;margin-top:4pt}.l{font-style:italic;font-size:9.5pt}"
           "ul{margin:2pt 0 0;padding-left:15pt}li{margin-bottom:1.5pt}")
    return f"<!doctype html><html><head><meta charset=utf-8><style>{css}</style></head><body>{''.join(parts)}</body></html>"


def render_pdf(profile: dict[str, Any], resume: dict[str, Any], out_path: Path) -> Path:
    """Render the résumé to PDF with headless Chromium (already needed for applying)."""
    from playwright.sync_api import sync_playwright

    from .apply.browser import launch_kwargs

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**{**launch_kwargs(), "headless": True})
        page = browser.new_page()
        page.set_content(render_html(profile, resume))
        page.pdf(path=str(out_path), format="Letter",
                 margin={"top": "0.5in", "bottom": "0.5in", "left": "0.6in", "right": "0.6in"})
        browser.close()
    return out_path
