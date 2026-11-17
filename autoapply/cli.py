"""Command line: autoapply init | discover | jobs | add | apply | run | auto | status | serve"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import typer
from sqlalchemy import select

from . import discovery, engine, matching
from .config import settings
from .db import Application, Job, init_db, session
from .profile import load_profile, validate, write_template

app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=True)
SEED = Path(__file__).parent / "companies.seed.yaml"


@app.callback()
def _startup() -> None:
    init_db()


@app.command()
def init() -> None:
    """Create data/profile.yaml and data/companies.yaml."""
    path = write_template()
    if not settings.companies_path.exists():
        shutil.copy(SEED, settings.companies_path)
    typer.echo(f"Profile:   {path}\nCompanies: {settings.companies_path}\nFill in the profile, then run `autoapply discover`.")


@app.command()
def discover() -> None:
    """Pull open jobs from every company in companies.yaml and score them."""
    typer.echo("Checking career pages...")
    new = discovery.discover(log=typer.echo)
    scored = matching.score_all()
    typer.echo(f"{len(new)} new jobs; {scored} scored. See them with `autoapply jobs`.")


@app.command()
def jobs(min_score: int = typer.Option(None, help="Defaults to your match threshold"), limit: int = 30) -> None:
    """List the best unapplied matches."""
    floor = settings.match_threshold if min_score is None else min_score
    with session() as s:
        rows = s.scalars(select(Job).where(Job.score >= floor, Job.dismissed.is_(False),
                                           Job.id.notin_(select(Application.job_id)))
                         .order_by(Job.score.desc(), Job.first_seen.desc()).limit(limit)).all()
        for j in rows:
            typer.echo(f"#{j.id:<6} {j.score:>3}  {j.title[:48]:<48} {j.company[:18]:<18} {j.location[:26]}")
    if not rows:
        typer.echo("No matches at that score. Try --min-score 50, or add companies to data/companies.yaml.")


@app.command()
def add(url: str) -> None:
    """Add one job from a Greenhouse, Lever or Ashby link."""
    job_id = discovery.add_job_from_url(url)
    if job_id is None:
        typer.echo("Couldn't read that link (supported: Greenhouse, Lever, Ashby job pages).")
        raise typer.Exit(1)
    matching.score_all(only_unscored=True)
    typer.echo(f"Added as job #{job_id}. Apply with `autoapply apply {job_id}`.")


@app.command()
def apply(job: str, yes: bool = typer.Option(False, "--yes", help="Skip both review gates for this run")) -> None:
    """Apply to a job by id or URL. Stops for your review unless --yes."""
    job_id: Optional[int] = int(job) if job.isdigit() else discovery.add_job_from_url(job)
    if job_id is None:
        typer.echo("Unsupported or unknown job link.")
        raise typer.Exit(1)
    if yes:
        settings.auto_approve_resume, settings.review_before_submit = True, False
    app_id = engine.enqueue(job_id)
    status = engine.step(app_id, log=typer.echo)
    typer.echo(f"Application #{app_id}: {status}")
    if status.startswith("awaiting"):
        typer.echo("Review it in the dashboard (`autoapply serve`) or approve with `autoapply approve "
                   f"{app_id}`.")


@app.command()
def approve(app_id: int) -> None:
    """Approve whatever application #ID is waiting on (résumé or filled form) and continue."""
    with session() as s:
        status = s.get(Application, app_id).status
    if status == "awaiting_resume_approval":
        engine.approve_resume(app_id)
    elif status == "awaiting_submit_approval":
        engine.approve_submit(app_id)
    else:
        typer.echo(f"Application #{app_id} is '{status}', nothing to approve.")
        raise typer.Exit(1)
    typer.echo(f"Application #{app_id}: {engine.step(app_id, log=typer.echo)}")


@app.command()
def run() -> None:
    """Process every queued/approved application."""
    typer.echo(engine.process_queue(log=typer.echo) or "Nothing to do.")


@app.command()
def auto() -> None:
    """Auto Apply: discover, score, queue the top matches within your daily cap, and process them."""
    discovery.discover(log=typer.echo)
    matching.score_all()
    queued = engine.auto_select(log=typer.echo)
    typer.echo(f"Queued {len(queued)} application(s).")
    typer.echo(engine.process_queue(log=typer.echo) or "Nothing to do.")


@app.command()
def status() -> None:
    """Show the application tracker."""
    with session() as s:
        rows = s.scalars(select(Application).order_by(Application.id.desc()).limit(50)).all()
        for a in rows:
            typer.echo(f"#{a.id:<5} {a.status:<26} {a.job.title[:44]:<44} {a.job.company}")
            if a.error and a.status != "submitted":
                typer.echo(f"       {a.error.splitlines()[0][:110]}")
    typer.echo(f"Submitted today: {engine.submitted_today()} / {settings.daily_cap}")


@app.command()
def check() -> None:
    """Validate your profile and settings."""
    problems = validate(load_profile())
    for p in problems:
        typer.echo(f"profile: {p}")
    from . import llm
    typer.echo(f"LLM: {'ready (' + llm.provider() + ', ' + llm.model() + ')' if llm.available() else 'No ANTHROPIC_API_KEY or GEMINI_API_KEY set (no tailoring, no open-ended answers)'}")
    typer.echo(f"Review before submit: {settings.review_before_submit} | auto-approve résumé: {settings.auto_approve_resume}")
    typer.echo("Profile OK." if not problems else f"{len(problems)} profile problem(s).")


@app.command("companies-update")
def companies_update() -> None:
    """Add companies from the built-in list to your data/companies.yaml (keeps everything already there)."""
    import yaml
    from pathlib import Path
    seed = yaml.safe_load((Path(__file__).parent / "companies.seed.yaml").read_text(encoding="utf-8")) or {}
    mine = (yaml.safe_load(settings.companies_path.read_text(encoding="utf-8")) if settings.companies_path.exists() else None) or {}
    added = 0
    for ats, names in seed.items():
        have = {str(n) for n in mine.setdefault(ats, [])}
        new = [n for n in names if str(n) not in have]
        mine[ats] += new
        added += len(new)
    settings.companies_path.parent.mkdir(parents=True, exist_ok=True)
    settings.companies_path.write_text(yaml.safe_dump(mine, sort_keys=False), encoding="utf-8")
    typer.echo(f"Added {added} companies; now watching {sum(len(v) for v in mine.values())}. Run `autoapply discover` next.")


@app.command()
def focus(include_core: bool = typer.Option(False, help="Also target the crowded core roles (Software/AI/ML/Data Engineer...)"),
          keep_companies: bool = typer.Option(False, help="Keep your current company list instead of the small/mid-size one")) -> None:
    """Target IT / help desk / technical support roles at small and mid-size companies; drop every other stored job."""
    import yaml
    from .profile import CORE_TITLES, FOCUS_EXCLUDES, UNSATURATED_TITLES
    profile = load_profile()
    prefs = profile["preferences"]
    prefs["titles"] = UNSATURATED_TITLES + (CORE_TITLES if include_core else [])
    excl = prefs.get("exclude_keywords") or []
    prefs["exclude_keywords"] = excl + [w for w in FOCUS_EXCLUDES if w.lower() not in {e.lower() for e in excl}]
    settings.profile_path.write_text(yaml.safe_dump(profile, sort_keys=False, allow_unicode=True), encoding="utf-8")
    if not keep_companies:
        if settings.companies_path.exists():
            shutil.copy(settings.companies_path, settings.companies_path.with_name("companies.backup.yaml"))
        shutil.copy(Path(__file__).parent / "companies.seed.yaml", settings.companies_path)
    watching = sum(len(v) for v in discovery.load_companies().values())
    removed = matching.prune()
    kept = matching.score_all()
    typer.echo(f"Targeting {len(prefs['titles'])} titles at {watching} companies. "
               f"Removed {removed} jobs outside them; {kept} kept and rescored. Run `autoapply discover` next.")


@app.command()
def prune() -> None:
    """Delete stored jobs that don't match your current target titles (applied-to jobs are kept)."""
    typer.echo(f"Removed {matching.prune()} jobs.")


@app.command("llm-test")
def llm_test() -> None:
    """Make one real LLM call to confirm your API key works."""
    from . import llm
    if not llm.available():
        typer.echo("No ANTHROPIC_API_KEY or GEMINI_API_KEY in .env")
        raise typer.Exit(1)
    try:
        reply = llm.complete("Reply with exactly one word.", "Say: ready", max_tokens=200).strip()
    except Exception as e:  # noqa: BLE001
        typer.echo(f"LLM call failed ({llm.provider()}, {llm.model()}): {e}")
        raise typer.Exit(1)
    typer.echo(f"LLM OK ({llm.provider()}, {llm.model()}): {reply}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the dashboard at http://127.0.0.1:8000"""
    import uvicorn
    uvicorn.run("autoapply.api:app", host=host, port=port)


if __name__ == "__main__":
    app()
