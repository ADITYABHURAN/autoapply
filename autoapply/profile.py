"""Your profile: the single source of truth for every form field and every résumé rewrite."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import settings

TEMPLATE = """\
# AutoApply profile. Everything the agent says about you comes from this file.
personal:
  first_name: ""
  last_name: ""
  email: ""
  phone: ""
  city: ""
  state: ""
  country: "United States"
  zip: ""
  address: ""
  linkedin: ""
  github: ""
  website: ""

# Used for work-authorization and sponsorship questions on every form.
work_authorization:
  authorized_to_work_in_us: true
  requires_sponsorship_now: false
  requires_sponsorship_future: true
  visa_status: ""            # e.g. "F-1 OPT"
  notes: ""

# Voluntary self-identification. Leave as-is to always pick "decline to answer".
eeo:
  gender: "Decline to self-identify"
  race: "Decline to self-identify"
  veteran: "I am not a protected veteran"
  disability: "I do not wish to answer"

preferences:
  titles: ["AI Engineer", "Machine Learning Engineer", "Software Engineer"]  # `autoapply focus` sets ~55 less crowded roles
  keywords: []               # nice-to-have words in the description
  exclude_keywords: ["Staff", "Principal", "Director", "Manager", "Clearance"]
  locations: ["New York", "New Jersey", "Remote"]
  remote_ok: true
  max_years_experience: 5    # skip roles asking for more than this
  salary_expectation: ""     # used only when a form asks
  start_date: "Immediately"
  willing_to_relocate: true
  open_to_onsite: true       # in-office / hybrid questions

# Anything else forms tend to ask. Plain sentences; the AI uses these facts when answering questions.
# e.g. "I have not worked or interviewed at any company I'm applying to." / "I can start two weeks after an offer."
extra_facts: []

summary: ""

skills: []

experience:
  - company: ""
    title: ""
    location: ""
    start: ""
    end: "Present"
    bullets: []

education:
  - school: ""
    degree: ""
    field: ""
    start: ""
    end: ""
    gpa: ""

projects: []
#  - name: ""
#    bullets: []

# Free-form facts the agent may use for open-ended questions ("Why this company?").
about_me: ""

# Path to your base résumé PDF, uploaded when tailoring is off.
base_resume_pdf: ""
"""


def write_template(path: Path | None = None) -> Path:
    path = path or settings.profile_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(TEMPLATE, encoding="utf-8")
    return path


def load_profile(path: Path | None = None) -> dict[str, Any]:
    path = path or settings.profile_path
    if not path.exists():
        raise FileNotFoundError(f"No profile at {path}. Run `autoapply init` and fill it in.")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:  # a file saved by an older version on Windows
        text = path.read_text(encoding="cp1252", errors="replace")
    profile = yaml.safe_load(text) or {}
    for key in ("personal", "work_authorization", "eeo", "preferences"):
        profile.setdefault(key, {})
    for key in ("skills", "experience", "education", "projects"):
        profile[key] = profile.get(key) or []
    return profile


# Less crowded roles a CS/AI-ML grad with Python, SQL, cloud and data-pipeline experience fits.
# These want engineering skills but draw far fewer applicants than "Software Engineer" postings.
# This is the default focus of the board: only jobs matching your titles are stored.
UNSATURATED_TITLES = [
    # customer-facing engineering
    "Forward Deployed Engineer", "Solutions Engineer", "AI Solutions Engineer", "Solutions Architect",
    "Customer Engineer", "Implementation Engineer", "Integration Engineer", "Deployment Engineer",
    "Technical Consultant", "Sales Engineer", "Developer Relations Engineer", "Developer Advocate",
    "Applied AI Specialist", "AI Deployment Strategist", "Partner Engineer", "Field Engineer",
    "Professional Services Engineer", "Onboarding Engineer", "Technical Solutions Consultant",
    # support engineering
    "Technical Support Engineer", "Support Engineer", "Product Support Engineer", "Customer Support Engineer",
    "Cloud Support Engineer", "Escalation Engineer", "Technical Support Specialist", "Product Specialist",
    "Customer Success Engineer", "Developer Support Engineer", "API Support Engineer",
    # IT
    "IT Support Specialist", "IT Support Engineer", "Help Desk Technician", "Service Desk Analyst",
    "IT Engineer", "Systems Administrator", "Desktop Support Technician", "IT Operations Analyst",
    "IT Specialist", "IT Administrator",
    # quality / automation / operations
    "QA Engineer", "Software Engineer in Test", "Automation Engineer", "Test Engineer",
    "Release Engineer", "Technical Operations Engineer", "Production Support Engineer",
    # niche data / AI
    "Fraud Analyst", "Risk Analyst", "Data Quality Engineer", "AI Evaluation Engineer",
    "Data Operations Analyst", "AI Trainer", "Prompt Engineer", "Trust and Safety Analyst",
]

# The crowded core roles. Not targeted unless you ask: `autoapply focus --include-core`.
CORE_TITLES = [
    "AI Engineer", "Applied AI Engineer", "Machine Learning Engineer", "ML Engineer", "Software Engineer",
    "Backend Engineer", "Full Stack Engineer", "Python Developer", "Software Developer", "Data Scientist",
    "Research Engineer", "MLOps Engineer", "ML Platform Engineer", "AI Agent Engineer", "LLM Engineer",
    "Data Engineer", "Analytics Engineer", "Data Analyst", "Business Intelligence Engineer",
    "DevOps Engineer", "Site Reliability Engineer", "Platform Engineer", "Cloud Engineer", "Infrastructure Engineer",
    "Systems Engineer",
]


def validate(profile: dict[str, Any]) -> list[str]:
    problems = []
    p = profile["personal"]
    for key in ("first_name", "last_name", "email", "phone"):
        if not p.get(key):
            problems.append(f"personal.{key} is empty")
    if not profile["experience"] or not profile["experience"][0].get("company"):
        problems.append("experience is empty")
    if not profile["skills"]:
        problems.append("skills is empty")
    return problems


def full_name(profile: dict[str, Any]) -> str:
    p = profile["personal"]
    return f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()


def resume_text(profile: dict[str, Any]) -> str:
    """Flat text of the résumé parts, for matching and LLM prompts."""
    parts = [profile.get("summary", ""), "Skills: " + ", ".join(profile["skills"])]
    for job in profile["experience"]:
        parts.append(f"{job.get('title', '')} at {job.get('company', '')} ({job.get('start', '')} - {job.get('end', '')})")
        parts.extend(f"- {b}" for b in job.get("bullets") or [])
    for edu in profile["education"]:
        parts.append(f"{edu.get('degree', '')} {edu.get('field', '')}, {edu.get('school', '')} ({edu.get('end', '')})")
    for proj in profile["projects"]:
        parts.append(f"Project: {proj.get('name', '')}")
        parts.extend(f"- {b}" for b in proj.get("bullets") or [])
    return "\n".join(x for x in parts if x)
