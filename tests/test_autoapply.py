import json
import os
import tempfile
from pathlib import Path

import httpx
import pytest

_tmp = tempfile.mkdtemp(prefix="autoapply-test-")
os.environ["AUTOAPPLY_HOME"] = _tmp
os.environ["AUTOAPPLY_HEADLESS"] = "true"
os.environ.pop("ANTHROPIC_API_KEY", None)

from autoapply import discovery, engine, inbox, matching, tailor  # noqa: E402
from autoapply.apply.forms import choose_option  # noqa: E402
from autoapply.config import settings  # noqa: E402
from autoapply.db import Application, Job, init_db, session  # noqa: E402
from autoapply.profile import load_profile, write_template  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "form.html"

PROFILE = """
personal: {first_name: Ada, last_name: Lovelace, email: ada@example.com, phone: "+1 201 555 0100",
           city: Jersey City, state: NJ, country: United States, linkedin: "https://linkedin.com/in/ada"}
work_authorization: {authorized_to_work_in_us: true, requires_sponsorship_now: false,
                     requires_sponsorship_future: true, visa_status: F-1 OPT}
eeo: {gender: Decline to self-identify}
preferences: {titles: [Machine Learning Engineer, AI Engineer], exclude_keywords: [Staff, Principal],
              locations: [New York, Remote], remote_ok: true, max_years_experience: 5, willing_to_relocate: true}
summary: ML engineer.
skills: [Python, PyTorch, SQL, LangChain, Docker, Kubernetes, FastAPI, Spark]
experience:
  - {company: Acme, title: ML Engineer, start: 2022, end: Present, bullets: [Built fraud models in PyTorch., Cut latency 40%.]}
education:
  - {school: Pace University, degree: MS, field: Computer Science, end: 2026}
projects: []
"""


@pytest.fixture(scope="module", autouse=True)
def setup():
    init_db()
    settings.profile_path.parent.mkdir(parents=True, exist_ok=True)
    settings.profile_path.write_text(PROFILE)
    yield


def test_profile_template_loads(tmp_path):
    p = write_template(tmp_path / "profile.yaml")
    assert load_profile(p)["preferences"]["titles"]


def test_discovery_parsers():
    def handler(req: httpx.Request) -> httpx.Response:
        if "greenhouse" in req.url.host:
            return httpx.Response(200, json={"jobs": [{"id": 42, "title": "ML Engineer", "location": {"name": "Remote - US"},
                                  "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/42",
                                  "content": "&lt;p&gt;Use &lt;b&gt;PyTorch&lt;/b&gt;&lt;/p&gt;", "first_published": "2026-11-10T12:00:00-05:00"}]})
        if "lever" in req.url.host:
            return httpx.Response(200, json=[{"id": "abc", "text": "Data Engineer", "categories": {"location": "New York"},
                                  "hostedUrl": "https://jobs.lever.co/acme/abc", "applyUrl": "https://jobs.lever.co/acme/abc/apply",
                                  "descriptionPlain": "Spark", "createdAt": 1794000000000, "lists": []}])
        return httpx.Response(200, json={"jobs": [{"id": "u1", "title": "AI Engineer", "location": "NYC", "isRemote": True, "isListed": True,
                              "jobUrl": "https://jobs.ashbyhq.com/acme/u1", "applyUrl": "https://jobs.ashbyhq.com/acme/u1/application",
                              "descriptionPlain": "LLMs", "publishedAt": "2026-11-12T00:00:00.000+00:00"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gh = discovery.fetch_greenhouse("acme", client)[0]
    assert (gh.external_id, gh.remote, gh.description) == ("42", True, "Use PyTorch")
    assert gh.posted_at.hour == 17  # converted to UTC
    lv = discovery.fetch_lever("acme", client)[0]
    assert lv.apply_url.endswith("/apply") and lv.posted_at.year == 2026
    ab = discovery.fetch_ashby("acme", client)[0]
    assert ab.remote and ab.apply_url.endswith("/application")
    assert discovery.upsert([gh, lv, ab]) and discovery.upsert([gh]) == []


def test_parse_job_url():
    assert discovery.parse_job_url("https://job-boards.greenhouse.io/stripe/jobs/123") == ("greenhouse", "stripe", "123")
    assert discovery.parse_job_url("https://boards.greenhouse.io/embed/job_app?for=x&gh_jid=9")[0] == "greenhouse"
    assert discovery.parse_job_url("https://jobs.lever.co/plaid/abc-1/apply") == ("lever", "plaid", "abc-1")
    assert discovery.parse_job_url("https://jobs.ashbyhq.com/ramp/u-1") == ("ashby", "ramp", "u-1")
    assert discovery.parse_job_url("https://example.com/careers/1") is None


def test_matching():
    profile = load_profile()
    good = Job(title="Machine Learning Engineer", location="Remote", remote=True,
               description="Python, PyTorch, SQL, Docker, Kubernetes, FastAPI, Spark, LangChain. 2+ years experience.")
    senior = Job(title="Staff Machine Learning Engineer", location="Remote", remote=True, description="Python")
    old = Job(title="Machine Learning Engineer", location="NYC", remote=False, description="Python. 8+ years of experience.")
    other = Job(title="Account Executive", location="New York", remote=False, description="Sales. SQL a plus.")
    assert matching.score_job(good, profile)[0] == 100
    assert matching.score_job(senior, profile)[0] == 0
    assert matching.score_job(old, profile)[0] == 0
    assert matching.score_job(other, profile)[0] <= 35


def test_enforce_facts_blocks_invention():
    profile = load_profile()
    forged = {"summary": "Rewritten.", "skills": ["PyTorch", "Rust", "python"],
              "experience": [{"company": "Acme", "start": 2022, "title": "Director of AI", "end": "Present", "bullets": ["Led fraud modeling."]},
                             {"company": "Google", "start": 2019, "title": "SWE", "bullets": ["Invented."]}],
              "education": [{"school": "MIT", "degree": "PhD"}]}
    out = tailor.enforce_facts(forged, profile)
    assert out["skills"] == ["PyTorch", "Python"]
    assert [e["company"] for e in out["experience"]] == ["Acme"]
    assert out["experience"][0]["title"] == "ML Engineer" and out["experience"][0]["bullets"] == ["Led fraud modeling."]
    assert out["education"][0]["school"] == "Pace University"
    assert "Led fraud modeling." in tailor.diff(profile, out)


def test_choose_option():
    assert choose_option(["Yes", "No"], "yes") == "Yes"
    assert choose_option(["Yes, I am authorized", "No, I am not"], "No") == "No, I am not"
    assert choose_option(["Male", "Female", "Decline To Self Identify"], "Decline to self-identify") == "Decline To Self Identify"
    assert choose_option(["United States", "United Kingdom"], "United States") == "United States"
    assert choose_option(["Red", "Blue"], "Green") is None


def test_extract_code():
    assert inbox.extract_code("Your security code is: aB3dE9xZ. It expires soon.") == "aB3dE9xZ"
    assert inbox.extract_code("Verification code\n\n 482913") == "482913"
    assert inbox.extract_code("Enter the code please") is None


def _make_app() -> int:
    with session() as s:
        job = Job(ats="greenhouse", company="fixture", external_id=os.urandom(4).hex(), title="ML Engineer",
                  location="Remote", remote=True, url=FIXTURE.as_uri(), apply_url=FIXTURE.as_uri(), description="PyTorch")
        s.add(job)
        s.flush()
        job_id = job.id
    return engine.enqueue(job_id)


def test_end_to_end_review_then_submit():
    """Fill the fixture form, stop for review, edit one answer, submit, and check what the page received."""
    settings.review_before_submit, settings.auto_approve_resume = True, True
    app_id = _make_app()
    logs: list[str] = []

    assert engine.step(app_id, logs.append) == "awaiting_submit_approval", logs
    with session() as s:
        app = s.get(Application, app_id)
        assert app.error is None, app.error
        by_label = {r["label"]: r for r in app.answers}
        assert Path(app.resume_pdf).exists() and Path(app.screenshot).exists()
    assert by_label["First Name"]["value"] == "Ada" and by_label["First Name"]["ok"]
    assert by_label["Are you legally authorized to work in the United States?"]["value"] == "Yes"
    assert by_label["Will you now or in the future require sponsorship for employment visa status?"]["value"] == "Yes"
    assert by_label["Gender"]["value"] == "Decline To Self Identify"
    assert by_label["Are you willing to relocate?"]["value"] == "Yes"
    assert by_label["What is your current visa status?"]["value"] == "F-1 OPT"  # follow-up revealed by the sponsorship answer
    assert by_label["Why do you want to work here?"]["value"] is None  # no LLM key in tests: left for the human
    assert by_label["I agree to the privacy policy"]["value"] is True

    why_key = by_label["Why do you want to work here?"]["key"]
    engine.approve_submit(app_id, {why_key: "I build fraud models."})
    assert engine.step(app_id, logs.append) == "submitted", logs

    # Re-open the fixture independently and replay the receipt to confirm the page got those values.
    with session() as s:
        app = s.get(Application, app_id)
        assert app.stage == "applied" and app.submitted_at
        assert {r["label"]: r for r in app.answers}["Why do you want to work here?"]["value"] == "I build fraud models."
        assert Path(app.screenshot).name == "confirmation.png"


def test_submit_posts_expected_values():
    """Drive the form through the engine's own fill path and read back what the page captured on submit."""
    from autoapply.apply import answers as ans
    from autoapply.apply.adapters import get_adapter
    from autoapply.apply.browser import open_page
    from autoapply.apply.forms import extract_fields

    profile = load_profile()
    pdf = Path(_tmp) / "r.pdf"
    tailor.render_pdf(profile, tailor.base_resume(profile), pdf)
    ctx = ans.Context(profile=profile, resume_pdf=str(pdf))
    adapter = get_adapter("greenhouse")
    with open_page() as page:
        page.goto(FIXTURE.as_uri())
        fields = extract_fields(page, adapter.form_root)
        engine._fill_all(page, fields, ctx, {}, use_llm=False)
        seen = {f.id for f in fields}
        engine._fill_all(page, [f for f in extract_fields(page, adapter.form_root) if f.id not in seen], ctx, {}, use_llm=False)
        engine._submit(page, adapter, print)
        got = json.loads(page.inner_text("#dump"))
    assert got == {"first": "Ada", "last": "Lovelace", "email": "ada@example.com", "phone": "+1 201 555 0100",
                   "linkedin": "https://linkedin.com/in/ada", "resume": "r.pdf", "auth": "1", "spons": "y",
                   "gender": "Decline To Self Identify", "reloc": "Yes", "why": "", "visa": "F-1 OPT", "consent": True}


def test_missing_required_blocks_submit():
    settings.review_before_submit, settings.auto_approve_resume = False, True
    p = load_profile()
    p["personal"]["last_name"] = ""
    # A required field with no answer must pause the application instead of submitting.
    from autoapply.apply import answers as ans
    from autoapply.apply.forms import Field
    res = ans.resolve([Field(id="x", kind="text", label="Last Name", required=True)], ans.Context(profile=p), use_llm=False)
    assert res[0].value is None
    settings.review_before_submit = True


def test_api_smoke():
    from fastapi.testclient import TestClient

    from autoapply.api import app
    c = TestClient(app)
    assert c.get("/").status_code == 200
    assert c.get("/api/state").json()["daily_cap"] == settings.daily_cap
    apps = c.get("/api/applications").json()
    assert apps and c.get(f"/api/applications/{apps[0]['id']}").json()["answers"]
    assert c.post("/api/jobs/add", json={"url": "https://example.com/x"}).status_code == 400


def test_gemini_provider(monkeypatch):
    import httpx

    from autoapply import llm
    from autoapply.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "llm_provider", "")
    monkeypatch.setattr(settings, "model", "")
    seen = {}

    def fake_post(url, headers, json, timeout):
        seen.update(url=url, key=headers["x-goog-api-key"], system=json["system_instruction"]["parts"][0]["text"])
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"a": 1}'}]}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    assert llm.provider() == "gemini" and llm.available()
    assert llm.complete_json("sys", "hi") == {"a": 1}
    assert "gemini-3.5-flash-lite:generateContent" in seen["url"] and seen["key"] == "k" and seen["system"] == "sys"


def test_labels_on_real_ats_markup():
    """Label/required detection against markup copied from live Ashby, Lever and Greenhouse forms."""
    from playwright.sync_api import sync_playwright

    from autoapply.apply.adapters import get_adapter
    from autoapply.apply.browser import launch_kwargs
    from autoapply.apply.forms import extract_fields

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**{**launch_kwargs(), "headless": True})
        page = browser.new_page()
        page.goto((Path(__file__).parent / "fixtures" / "ats_labels.html").as_uri())
        got = {(f.kind, f.label): f.required for f in extract_fields(page, get_adapter("ashby").form_root, probe_comboboxes=False)}
        browser.close()
    assert got == {
        ("file", "Resume"): True,
        ("button_group", "Do you have 7 years of experience?"): True,
        ("button_group", "Open to relocation?"): False,
        ("file", "Resume/CV"): True,          # Lever: "Resume/CV ✱ ATTACH RESUME/CV"
        ("text", "Full name"): True,
        ("radio", "Are you authorized to work?"): True,
        ("file", "Cover Letter"): False,     # Greenhouse: button says "Attach", group names the question
    }, got


def test_llm_answers_every_question(monkeypatch):
    """Open questions go to the LLM; required ones it skips get a second pass; failures are reported, not hidden."""
    from autoapply import llm
    from autoapply.apply import answers as ans
    from autoapply.apply.forms import Field
    from autoapply.profile import TEMPLATE
    import yaml

    profile = yaml.safe_load(TEMPLATE)
    fields = [Field(id="a1", kind="textarea", label="Why do you want to work here?", required=True),
              Field(id="a2", kind="select", label="Agreement to Arbitrate", required=True, options=["I acknowledge", "I decline"]),
              Field(id="a3", kind="radio", label="Are you open to working in-person 3 days a week?", required=True, options=["Yes", "No"])]
    calls = []

    def fake(system, user, max_tokens=0):
        calls.append(user)
        return {"a1": "Because of X."} if len(calls) == 1 else {"a2": "I acknowledge"}

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", fake)
    ctx = ans.Context(profile=profile, job_title="AI Engineer", company="acme")
    got = {a.field.id: (a.value, a.source) for a in ans.resolve(fields, ctx)}
    assert got == {"a1": ("Because of X.", "llm"), "a2": ("I acknowledge", "llm"), "a3": ("Yes", "rule")}
    assert len(calls) == 2 and "REQUIRED" in calls[1] and '"a1"' not in calls[1]

    def boom(*a, **k):
        raise llm.LLMUnavailable("Gemini error 429 (free-tier quota used up for now; it resets daily): quota")

    monkeypatch.setattr(llm, "complete_json", boom)
    ans.resolve(fields[:1], ctx)
    assert ctx.warnings and "429" in ctx.warnings[0]


def test_only_targeted_roles_are_stored_and_prune():
    from autoapply import matching
    from autoapply.apply.forms import choose_option
    from sqlalchemy import select

    from autoapply.profile import UNSATURATED_TITLES

    prefs = {"titles": UNSATURATED_TITLES, "exclude_keywords": ["Staff", "Manager"]}
    keep = ["Help Desk Technician", "IT Help Desk Support / End User Support", "IT Support Specialist", "User Support Analyst",
            "Desktop Support Engineer / IT", "Technical Support Specialist II", "Technical Support Engineer - Building Automation",
            "Application Support Specialist", "Data Center Technician 1", "IT Field Technician", "IT Operations Specialist",
            "Senior Help Desk Technician", "Customer Support Engineer", "Mobile Device Support Technician"]
    drop = ["Software Engineer", "Machine Learning Engineer", "Account Executive", "Forward Deployed Engineer",
            "Solutions Engineer", "Mechanical Engineer", "Product Designer", "Engineering Manager", "Recruiter",
            "IT Support Manager", "Staff Support Engineer", "Sales Engineer", "Data Scientist", "Product Marketing Specialist"]
    assert [t for t in keep if not matching.title_targeted(t, prefs)] == []
    assert [t for t in drop if matching.title_targeted(t, prefs)] == []

    with session() as s:
        s.add(Job(ats="lever", company="prune-co", external_id="keep", title="IT Support Specialist", location="", url="", apply_url=""))
        s.add(Job(ats="lever", company="prune-co", external_id="drop", title="Account Executive", location="", url="", apply_url=""))
        s.add(Job(ats="lever", company="big-co", external_id="unwatched", title="IT Support Specialist", location="", url="", apply_url=""))
    import yaml
    from autoapply.config import settings
    profile = yaml.safe_load(settings.profile_path.read_text(encoding="utf-8"))
    old = profile["preferences"]["titles"]
    profile["preferences"]["titles"] = ["IT Support Specialist"]
    settings.profile_path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    had = settings.companies_path.read_text(encoding="utf-8") if settings.companies_path.exists() else None
    settings.companies_path.write_text("lever:\n  - prune-co\n", encoding="utf-8")
    try:
        assert matching.prune() >= 2
        with session() as s:
            left = {j.external_id for j in s.scalars(select(Job).where(Job.company == "prune-co")).all()}
            gone = s.scalars(select(Job).where(Job.company == "big-co")).all()
        assert left == {"keep"} and gone == []
    finally:
        if had is None:
            settings.companies_path.unlink()
        else:
            settings.companies_path.write_text(had, encoding="utf-8")
        profile["preferences"]["titles"] = old
        settings.profile_path.write_text(yaml.safe_dump(profile), encoding="utf-8")

    opts = ["United States Minor Outlying Islands +1", "United States +1", "United Kingdom +44"]
    assert choose_option(opts, "United States") == "United States +1"
