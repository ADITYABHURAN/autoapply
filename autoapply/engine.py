"""The application pipeline: tailor -> (approve résumé) -> fill -> (approve form) -> submit -> record."""
from __future__ import annotations

import copy
import re
import time
import traceback
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, select

from . import inbox, llm, tailor
from .apply import answers as ans
from .apply.adapters import CODE_PROMPT, LOGIN_WALL, Adapter, captcha_challenge_visible, get_adapter
from .apply.forms import Field, FillError, extract_fields, fill_field
from .config import settings
from .db import Application, Job, session, utcnow
from .profile import load_profile, validate

Log = Callable[[str], None]


class NeedsManual(Exception):
    """The form needs a human (CAPTCHA, login wall, emailed code with no mailbox)."""


# ---------------------------------------------------------------- queueing

def enqueue(job_id: int, source: str = "manual") -> int:
    with session() as s:
        app = s.scalar(select(Application).where(Application.job_id == job_id))
        if app is None:
            app = Application(job_id=job_id, source=source)
            s.add(app)
            s.flush()
        elif app.status in ("failed", "cancelled", "needs_manual"):
            app.status, app.error = ("ready_to_fill" if app.resume_pdf else "queued"), None
        return app.id


def submitted_today() -> int:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with session() as s:
        return s.scalar(select(func.count()).select_from(Application).where(Application.submitted_at >= start)) or 0


def auto_select(log: Log = print) -> list[int]:
    """Auto Apply: queue the best new matches within the threshold, age window and daily cap."""
    with session() as s:
        in_flight = s.scalar(select(func.count()).select_from(Application).where(
            Application.status.notin_(("submitted", "failed", "cancelled", "needs_manual")))) or 0
        room = settings.daily_cap - submitted_today() - in_flight
        if room <= 0:
            log(f"Daily cap of {settings.daily_cap} reached.")
            return []
        cutoff = utcnow() - timedelta(hours=settings.max_age_hours)
        applied = select(Application.job_id)
        jobs = s.scalars(
            select(Job).where(Job.score >= settings.match_threshold, Job.dismissed.is_(False), Job.id.notin_(applied),
                              func.coalesce(Job.posted_at, Job.first_seen) >= cutoff)
            .order_by(Job.score.desc()).limit(min(room, 60))).all()
        ids = [j.id for j in jobs]
    return [enqueue(i, source="auto") for i in ids]


# ---------------------------------------------------------------- stage 1: prepare documents

def prepare(app_id: int, log: Log = print) -> None:
    profile = load_profile()
    problems = validate(profile)
    if problems:
        raise RuntimeError("Profile incomplete: " + "; ".join(problems))
    with session() as s:
        app = s.get(Application, app_id)
        job = app.job
        app.status = "tailoring"
        title, company, desc = job.title, job.company, job.description
    out_dir = settings.artifacts_dir / f"app-{app_id}"
    name = f"{profile['personal']['first_name']}_{profile['personal']['last_name']}".strip("_") or "resume"

    resume, diff_text, cover = tailor.base_resume(profile), "", ""
    if llm.available():
        log(f"  tailoring résumé for {title} at {company}")
        resume = tailor.tailor_resume(profile, title, company, desc)
        diff_text = tailor.diff(profile, resume)
        cover = tailor.write_cover_letter(profile, resume, title, company, desc)

    base_pdf = profile.get("base_resume_pdf")
    if not llm.available() and base_pdf and Path(base_pdf).expanduser().exists():
        pdf = Path(base_pdf).expanduser()
    else:
        pdf = tailor.render_pdf(profile, resume, out_dir / f"{name}_Resume.pdf")

    with session() as s:
        app = s.get(Application, app_id)
        app.tailored_resume, app.resume_diff, app.resume_pdf, app.cover_letter = resume, diff_text, str(pdf), cover
        needs_review = bool(diff_text) and not settings.auto_approve_resume
        app.status = "awaiting_resume_approval" if needs_review else "ready_to_fill"


def approve_resume(app_id: int, resume: dict[str, Any] | None = None, cover_letter: str | None = None) -> None:
    """Approve (optionally with edits) the tailored résumé; re-renders the PDF if edited."""
    profile = load_profile()
    with session() as s:
        app = s.get(Application, app_id)
        if resume is not None:
            app.tailored_resume = tailor.enforce_facts(resume, profile)
            tailor.render_pdf(profile, app.tailored_resume, Path(app.resume_pdf))
        if cover_letter is not None:
            app.cover_letter = cover_letter
        app.status = "ready_to_fill"


# ---------------------------------------------------------------- stage 2/3: fill and submit

def _open_form(page, adapter: Adapter) -> list[Field]:
    fields = extract_fields(page, adapter.form_root)
    if len([f for f in fields if f.kind != "file"]) >= 3:
        return fields
    for sel in adapter.open_form:
        try:
            target = page.locator(sel).first
            if target.is_visible():
                target.click()
                page.wait_for_timeout(1500)
                fields = extract_fields(page, adapter.form_root)
                if len(fields) >= 3:
                    return fields
        except Exception:
            continue
    return fields or extract_fields(page, None)


def _fill_all(page, fields: list[Field], ctx: ans.Context, approved: dict, use_llm: bool, log: Log = print) -> list[ans.Answer]:
    results = ans.resolve(fields, ctx, approved, use_llm=use_llm)
    while ctx.warnings:
        log("  ! " + ctx.warnings.pop(0))
    for a in results:
        if a.value is None:
            continue
        try:
            fill_field(page, a.field, a.value)
            a.ok = True
        except (FillError, Exception) as e:  # noqa: BLE001 - one bad field must not sink the form
            a.ok, a.error = False, str(e).splitlines()[0][:200]
    return results


def _wait_for_human(page, still_blocked: Callable[[], bool], what: str, log: Log, seconds: int = 180) -> None:
    if settings.headless:
        raise NeedsManual(f"{what}. Re-run with AUTOAPPLY_HEADLESS=false to handle it yourself.")
    log(f"  {what}: finish it in the browser window (waiting up to {seconds}s)")
    deadline = time.time() + seconds
    while time.time() < deadline:
        page.wait_for_timeout(1500)
        if not still_blocked():
            return
    raise NeedsManual(f"{what} was not completed in time")


def _body_text(page) -> str:
    try:
        return page.inner_text("body", timeout=5000)
    except Exception:
        return ""


def _submit(page, adapter: Adapter, log: Log) -> None:
    started = time.time()
    clicked = False
    for sel in adapter.submit:
        try:
            btn = page.locator(sel).first
            if btn.is_visible():
                btn.scroll_into_view_if_needed()
                btn.click()
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        raise RuntimeError("Could not find the submit button")

    success_url, success_text = re.compile(adapter.success_url, re.I), re.compile(adapter.success_text, re.I)
    code_tried = False
    deadline = time.time() + 45
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        body = _body_text(page)
        if success_url.search(page.url) or success_text.search(body):
            return
        if captcha_challenge_visible(page):
            _wait_for_human(page, lambda: captcha_challenge_visible(page), "CAPTCHA challenge", log)
            deadline = time.time() + 30
            continue
        if CODE_PROMPT.search(body) and not code_tried:
            code_tried = True
            code = inbox.wait_for_code(since=started) if inbox.configured() else None
            if code:
                log("  entering emailed verification code")
                box = page.locator('input[autocomplete="one-time-code"], input[maxlength="1"]:visible, input[name*="code" i]:visible').first
                box.click()
                page.keyboard.type(code, delay=40)
                for sel in adapter.submit:
                    try:
                        if page.locator(sel).first.is_visible():
                            page.locator(sel).first.click()
                            break
                    except Exception:
                        continue
            else:
                _wait_for_human(page, lambda: bool(CODE_PROMPT.search(_body_text(page)))
                                and not success_text.search(_body_text(page)), "Emailed verification code", log)
            deadline = time.time() + 30
    errors = page.locator('[class*="error" i]:visible, [role="alert"]:visible').all_inner_texts()[:5]
    detail = "; ".join(e.strip() for e in errors if e.strip())[:400]
    raise RuntimeError("No confirmation after submit" + (f": {detail}" if detail else ""))


def run_form(app_id: int, submit: bool, log: Log = print) -> None:
    """Open the employer's form, fill it, and either stop for review or submit."""
    from .apply.browser import open_page

    profile = load_profile()
    with session() as s:
        app = s.get(Application, app_id)
        job = app.job
        adapter = get_adapter(job.ats)
        url, title, company = job.apply_url, job.title, job.company
        if job.ats == "greenhouse" and "/embed/job_app" not in url and job.external_id.isdigit():
            from .discovery import greenhouse_apply_url
            url = greenhouse_apply_url(job.company, job.external_id)  # jobs discovered before this fix
        # Carry forward what you edited and what the LLM wrote; profile-derived answers are recomputed.
        approved = {r["key"]: r["value"] for r in (app.answers or [])
                    if r.get("value") not in (None, "", []) and r.get("kind") != "file"
                    and r.get("source") in ("approved", "llm", "saved")}
        out_dir = settings.artifacts_dir / f"app-{app_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        cover_pdf = ""
        if app.cover_letter:
            cover_pdf = str(_cover_pdf(profile, app.cover_letter, out_dir))
        ctx = ans.Context(profile=profile, job_title=title, company=company, description=job.description,
                          resume_pdf=app.resume_pdf or "", cover_letter=app.cover_letter or "", cover_letter_pdf=cover_pdf)
        app.status = "submitting" if submit else "filling"

    log(f"  opening {url}")
    with open_page() as page:
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        if LOGIN_WALL.search(_body_text(page)):
            raise NeedsManual("This employer requires an account/login before applying")

        fields = _open_form(page, adapter)
        if not fields:
            raise RuntimeError("No application form found on the page")
        # On the submit pass every answer was already approved, so the LLM is not consulted again.
        results = _fill_all(page, fields, ctx, approved, use_llm=not submit, log=log)

        # Some answers reveal follow-up questions; pick those up in a second pass.
        seen = {f.id for f in fields}
        extra = [f for f in extract_fields(page, adapter.form_root) if f.id not in seen]
        if extra:
            results += _fill_all(page, extra, ctx, approved, use_llm=not submit, log=log)

        receipts = [a.receipt() for a in results]
        missing = [r["label"] or r["kind"] for r in receipts if r["required"] and (r["value"] in (None, "", []) or r["ok"] is False)]
        shot = out_dir / ("submitted.png" if submit else "review.png")
        try:
            page.screenshot(path=str(shot), full_page=True)
        except Exception:
            shot = None

        def save(status: str, error: str | None = None, done: bool = False) -> None:
            with session() as s:
                a = s.get(Application, app_id)
                a.answers, a.status, a.error = receipts, status, error
                a.screenshot = str(shot) if shot else a.screenshot
                if done:
                    a.submitted_at, a.stage = utcnow(), "applied"

        if missing:
            save("awaiting_submit_approval", f"{len(missing)} required field(s) need your answer: " + "; ".join(missing)[:500])
            log(f"  paused: {len(missing)} required field(s) unanswered")
            return
        if not submit:
            save("awaiting_submit_approval")
            log("  filled; waiting for your review")
            return

        _submit(page, adapter, log)
        try:
            page.screenshot(path=str(out_dir / "confirmation.png"), full_page=True)
            shot = out_dir / "confirmation.png"
        except Exception:
            pass
        save("submitted", done=True)
        ans.remember(receipts)
        log(f"  submitted: {title} at {company}")


def _cover_pdf(profile: dict, text: str, out_dir: Path) -> Path:
    import html as _html

    from playwright.sync_api import sync_playwright

    from .apply.browser import launch_kwargs

    path = out_dir / "Cover_Letter.pdf"
    body = "".join(f"<p>{_html.escape(p)}</p>" for p in text.split("\n") if p.strip())
    doc = f"<html><body style='font:11pt/1.5 Arial,sans-serif;margin:0'>{body}</body></html>"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**{**launch_kwargs(), "headless": True})
        page = browser.new_page()
        page.set_content(doc)
        page.pdf(path=str(path), format="Letter", margin={"top": "1in", "bottom": "1in", "left": "1in", "right": "1in"})
        browser.close()
    return path


def approve_submit(app_id: int, edits: dict[str, Any] | None = None) -> None:
    """Approve the filled form (with optional edits keyed by receipt `key`) and mark it for submission."""
    with session() as s:
        app = s.get(Application, app_id)
        receipts = copy.deepcopy(app.answers or [])  # a new object, so the JSON column registers the change
        for r in receipts:
            if edits and r["key"] in edits:
                r["value"], r["source"], r["ok"], r["error"] = edits[r["key"]], "approved", None, ""
        app.answers = receipts
        app.status, app.error = "submitting", None


# ---------------------------------------------------------------- driver

def step(app_id: int, log: Log = print) -> str:
    """Advance one application as far as it can go without you. Returns the new status."""
    with session() as s:
        status = s.get(Application, app_id).status
    try:
        if status in ("queued", "tailoring"):
            prepare(app_id, log)
            with session() as s:
                status = s.get(Application, app_id).status
        if status in ("ready_to_fill", "filling"):
            run_form(app_id, submit=not settings.review_before_submit, log=log)
        elif status == "submitting":
            run_form(app_id, submit=True, log=log)
    except NeedsManual as e:
        _fail(app_id, "needs_manual", str(e))
        log(f"  needs you: {e}")
    except Exception as e:  # noqa: BLE001
        _fail(app_id, "failed", f"{e}\n{traceback.format_exc(limit=3)}")
        log(f"  failed: {e}")
    with session() as s:
        return s.get(Application, app_id).status


def _fail(app_id: int, status: str, error: str) -> None:
    with session() as s:
        app = s.get(Application, app_id)
        app.status, app.error = status, error[:2000]


def process_queue(log: Log = print) -> dict[str, int]:
    """Run every application that is waiting on the machine rather than on you."""
    with session() as s:
        ids = s.scalars(select(Application.id).where(
            Application.status.in_(("queued", "ready_to_fill", "submitting"))).order_by(Application.id)).all()
    tally: dict[str, int] = {}
    for app_id in ids:
        if submitted_today() >= settings.daily_cap:
            log(f"Daily cap of {settings.daily_cap} reached; stopping.")
            break
        log(f"Application #{app_id}")
        status = step(app_id, log)
        tally[status] = tally.get(status, 0) + 1
    return tally
