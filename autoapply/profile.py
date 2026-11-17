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
  titles: ["AI Engineer", "Machine Learning Engineer", "Software Engineer"]  # `autoapply focus` sets ~50 IT / help desk / technical support titles
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


# The board's default focus: IT support, help desk and technical support roles. They draw far
# fewer applicants than engineering postings, and a CS grad is a strong candidate for them.
# Only jobs matching your titles are stored.
UNSATURATED_TITLES = [
    # help desk / service desk
    "Help Desk Technician", "Help Desk Engineer", "Help Desk Analyst", "Help Desk Support Specialist",
    "IT Help Desk", "Service Desk Analyst", "Service Desk Technician", "Service Desk Engineer",
    # IT support
    "IT Support Specialist", "IT Support Engineer", "IT Support Analyst", "IT Support Technician",
    "IT Specialist", "IT Technician", "IT Associate", "IT Analyst", "IT Engineer", "IT Administrator",
    "IT Operations Specialist", "IT Operations Analyst", "IT Operations Engineer", "IT Systems Engineer",
    "Systems Administrator", "User Support Analyst", "End User Support", "Workplace Technology Specialist",
    # desktop / device / field
    "Desktop Support Technician", "Desktop Support Engineer", "Mobile Device Support Technician",
    "IT Field Technician", "Field Service Technician", "Field Support Specialist",
    # technical / application / product support
    "Technical Support Specialist", "Technical Support Engineer", "Technical Support Analyst", "Technical Support",
    "Application Support Specialist", "Application Support Analyst", "Application Support Engineer",
    "Product Support Specialist", "Product Support Engineer", "Customer Support Engineer", "Support Engineer",
    "Support Specialist", "Cloud Support Engineer", "Production Support Analyst",
    # network / data center
    "Network Support Technician", "NOC Technician", "Data Center Technician",
]

# Extra title words to exclude for this focus (support roles that are not technical).
FOCUS_EXCLUDES = ["Forward Deployed", "Sales", "Legal", "Clinical", "Billing", "Payroll"]

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
