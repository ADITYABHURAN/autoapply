"""Decide what goes in each field.

Order of precedence, per field:
  1. approved  - a value you edited/approved for this application
  2. rule      - deterministic mapping from your profile (name, email, work authorization, EEO...)
  3. saved     - an answer you approved on an earlier form with the same question
  4. llm       - open-ended or unusual questions, answered from your profile only
Fields nothing can answer are left blank and flagged if required.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import yaml
from sqlalchemy import select

from .. import llm
from ..db import SavedAnswer, session
from ..profile import full_name
from .forms import Field, choose_option, question_key


@dataclass
class Context:
    profile: dict[str, Any]
    job_title: str = ""
    company: str = ""
    description: str = ""
    resume_pdf: str = ""
    cover_letter: str = ""
    cover_letter_pdf: str = ""
    warnings: list = field(default_factory=list)  # shown in the activity log


@dataclass
class Answer:
    field: Field
    value: Any = None
    source: str = "none"  # approved | rule | saved | llm | none
    ok: Optional[bool] = None
    error: str = ""

    def receipt(self) -> dict[str, Any]:
        v = self.value
        if self.field.kind == "file" and v:
            v = str(v).replace("\\", "/").rsplit("/", 1)[-1]
        return {"key": self.field.key, "label": self.field.label, "kind": self.field.kind,
                "required": self.field.required, "options": self.field.options[:60],
                "value": v, "source": self.source, "ok": self.ok, "error": self.error}


def _yn(flag: Any) -> str:
    return "Yes" if flag else "No"


_DECLINE = re.compile(r"decline|do(n'?t| not) wish|prefer not|not to (say|answer|disclose|identify)|choose not", re.I)


def _eeo(setting: str, f: Field) -> Optional[str]:
    """Pick the profile's EEO answer if the form offers it, else the 'decline' option."""
    if f.options:
        return choose_option(f.options, setting) or next((o for o in f.options if _DECLINE.search(o)), None)
    return setting


def _location(p: dict) -> str:
    return ", ".join(x for x in (p.get("city"), p.get("state")) if x)


# (label pattern, kinds it applies to or None for any, resolver). First match wins.
Rule = tuple[re.Pattern, Optional[tuple[str, ...]], Callable[[Context, Field], Any]]
TEXTY = ("text", "textarea", "combobox", "select")


def _rules() -> list[Rule]:
    P = lambda c: c.profile["personal"]            # noqa: E731
    W = lambda c: c.profile["work_authorization"]  # noqa: E731
    E = lambda c: c.profile["eeo"]                 # noqa: E731
    X = lambda c: (c.profile["experience"] or [{}])[0]  # noqa: E731
    ED = lambda c: (c.profile["education"] or [{}])[0]  # noqa: E731
    R = lambda pattern: re.compile(pattern, re.I)  # noqa: E731
    return [
        (R(r"resume|résumé|\bcv\b"), ("file",), lambda c, f: c.resume_pdf or None),
        (R(r"cover letter"), ("file",), lambda c, f: c.cover_letter_pdf or None),
        (R(r"cover letter"), ("text", "textarea"), lambda c, f: c.cover_letter or None),
        (R(r"first name|given name"), TEXTY, lambda c, f: P(c).get("first_name")),
        (R(r"last name|family name|surname"), TEXTY, lambda c, f: P(c).get("last_name")),
        (R(r"preferred (first )?name|nickname"), TEXTY, lambda c, f: P(c).get("first_name")),
        (R(r"^(full |legal |your )*name$|full name|legal name"), TEXTY, lambda c, f: full_name(c.profile)),
        (R(r"e-?mail"), ("text",), lambda c, f: P(c).get("email")),
        (R(r"phone|mobile|telephone"), ("text",), lambda c, f: P(c).get("phone")),
        (R(r"linkedin"), ("text",), lambda c, f: P(c).get("linkedin")),
        (R(r"github"), ("text",), lambda c, f: P(c).get("github")),
        (R(r"website|portfolio|personal site"), ("text",), lambda c, f: P(c).get("website") or P(c).get("github")),
        (R(r"street|address line|^address"), ("text",), lambda c, f: P(c).get("address")),
        (R(r"zip|postal"), ("text",), lambda c, f: P(c).get("zip")),
        (R(r"^(state|province)|state/province"), TEXTY, lambda c, f: P(c).get("state")),
        (R(r"^country|country of residence|country.*(live|reside|located)"), TEXTY, lambda c, f: P(c).get("country")),
        (R(r"^(current )?(city|location)|where are you (currently )?(located|based)|city.*(live|reside)"), TEXTY,
         lambda c, f: P(c).get("city") if re.search(r"^city$", f.label, re.I) else _location(P(c))),
        (R(r"sponsor"), None, lambda c, f: _yn(W(c).get("requires_sponsorship_future") or W(c).get("requires_sponsorship_now"))),
        (R(r"(legally )?(authorized|authorised|eligible|permitted|entitled|right) to work|work authori[sz]ation"), None,
         lambda c, f: _yn(W(c).get("authorized_to_work_in_us", True)) if (f.options or f.kind != "text") else None),
        (R(r"visa status|immigration status"), ("text",), lambda c, f: W(c).get("visa_status") or None),
        (R(r"hispanic|latino"), None, lambda c, f: _eeo(E(c).get("hispanic", "Decline to self-identify"), f)),
        (R(r"gender|\bsex\b|pronoun"), None, lambda c, f: _eeo(E(c).get("gender", "Decline to self-identify"), f)),
        (R(r"\brace\b|ethnic"), None, lambda c, f: _eeo(E(c).get("race", "Decline to self-identify"), f)),
        (R(r"veteran"), None, lambda c, f: _eeo(E(c).get("veteran", "I am not a protected veteran"), f)),
        (R(r"disabilit"), None, lambda c, f: _eeo(E(c).get("disability", "I do not wish to answer"), f)),
        (R(r"(18|eighteen) years"), None, lambda c, f: "Yes"),
        (R(r"salary|compensation|pay expectation|desired pay"), ("text", "textarea"),
         lambda c, f: c.profile["preferences"].get("salary_expectation") or None),
        (R(r"start date|when (can|could) you start|notice period|earliest.*start|available to start"), ("text", "textarea"),
         lambda c, f: c.profile["preferences"].get("start_date") or None),
        (R(r"(on-?site|in-?person|in[- ]office|hybrid|from (the|our|an) office|days? (a|per) week in)"), None,
         lambda c, f: _yn(c.profile["preferences"].get("open_to_onsite", True)) if f.options else None),
        (R(r"relocat"), None, lambda c, f: _yn(c.profile["preferences"].get("willing_to_relocate")) if f.options else None),
        (R(r"how did you (hear|find|learn)|source of (application|referral)"), None,
         lambda c, f: (choose_option(f.options, "Company website") or choose_option(f.options, "Careers page")
                       or choose_option(f.options, "Other")) if f.options else "Company careers page"),
        (R(r"current (company|employer)|most recent (company|employer)"), ("text",), lambda c, f: X(c).get("company")),
        (R(r"current (job )?title|current (role|position)|most recent title"), ("text",), lambda c, f: X(c).get("title")),
        (R(r"^school|university|college|institution"), TEXTY, lambda c, f: ED(c).get("school")),
        (R(r"^degree"), TEXTY, lambda c, f: ED(c).get("degree")),
        (R(r"^(discipline|major|field of study)"), TEXTY, lambda c, f: ED(c).get("field")),
        (R(r"\bgpa\b"), ("text",), lambda c, f: ED(c).get("gpa") or None),
        # Consent boxes that gate submission. They are listed in the review screen.
        (R(r"privacy (policy|notice)|i (agree|acknowledge|consent|certify|understand)|terms (and|&) conditions"),
         ("checkbox",), lambda c, f: True),
    ]


RULES = _rules()


def rule_answer(f: Field, ctx: Context) -> Any:
    for pattern, kinds, resolver in RULES:
        if kinds is not None and f.kind not in kinds:
            continue
        if not pattern.search(f.label):
            continue
        value = resolver(ctx, f)
        if value in (None, ""):
            return None
        if f.is_choice and not isinstance(value, bool):
            return choose_option(f.options, str(value))  # None -> let a later source try
        return value
    return None


LLM_SYSTEM = """You fill job application form fields for a candidate. Answer EVERY field you can; the candidate reviews all answers before anything is submitted.
Rules:
- For fields with "options", the value MUST be copied exactly from the options list (for checkbox_group, a list of options).
- Free-text questions ("Why this company?", "Describe a project...", "Anything else?"): write a specific, concise first-person answer (2-5 sentences unless the question asks for more), tying the candidate's real experience to this job and company. Use the job description for what the company does.
- Facts about the candidate (employers, degrees, certifications, clearances, years of experience, numbers, links) come ONLY from the profile and its extra_facts. Never invent them.
- Yes/no questions about a skill or experience: Yes only if the profile supports it. Years-of-experience questions: compute from the experience dates.
- Acknowledgements, consents and policy agreements the form requires (privacy notice, arbitration agreement, AI-use policy, "I certify the above is true"): choose the affirmative/acknowledging option.
- "Have you worked/interviewed here before?", "Do you have relatives here?", "Are you subject to a non-compete?": answer No unless the profile says otherwise.
- Logistics (start date, notice period, timelines, work location) come from preferences and personal location; if nothing is stated, give a short reasonable answer such as "No constraints".
- Use null ONLY for: demographic questions (gender, race, veteran, disability), a link/ID/number the profile does not contain, or a puzzle/assessment you cannot actually solve.
Return JSON only: {"<field id>": <value or null>, ...}"""

RETRY_NOTE = """

These REQUIRED fields were left unanswered on the first pass. Answer each one now with your best truthful answer, following the rules above. Use null only if answering would require inventing a fact about the candidate."""


def llm_answers(fields: list[Field], ctx: Context, note: str = "") -> dict[str, Any]:
    if not fields:
        return {}
    if not llm.available():
        ctx.warnings.append(f"{len(fields)} question(s) need an LLM key (GEMINI_API_KEY or ANTHROPIC_API_KEY) to be answered")
        return {}
    spec = [{"id": f.id, "label": f.label, "kind": f.kind, "required": f.required,
             **({"options": f.options[:80]} if f.options else {}),
             **({"maxlength": f.maxlength} if f.maxlength else {})} for f in fields]
    profile = {k: v for k, v in ctx.profile.items() if k not in ("eeo", "base_resume_pdf")}
    user = (f"JOB: {ctx.job_title} at {ctx.company}\n\nJOB DESCRIPTION (excerpt):\n{ctx.description[:5000]}\n\n"
            f"CANDIDATE PROFILE:\n{yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)}\n\n"
            f"FIELDS:\n{json.dumps(spec, indent=1)}{note}")
    try:
        reply = llm.complete_json(LLM_SYSTEM, user, max_tokens=6000)
    except Exception as e:  # noqa: BLE001 - quota or network trouble must not sink the form
        ctx.warnings.append(f"LLM could not answer {len(fields)} question(s): {str(e).splitlines()[0][:200]}")
        return {}
    return reply if isinstance(reply, dict) else {}


def _valid(f: Field, value: Any) -> Any:
    """Normalize a candidate value for a field; None if it can't be used."""
    if value in (None, "", []):
        return None
    if f.kind == "checkbox_group":
        vals = value if isinstance(value, list) else [value]
        picked = [c for c in (choose_option(f.options, str(v)) for v in vals) if c]
        return picked or None
    if f.is_choice:
        return choose_option(f.options, str(value))
    return value


def resolve(fields: list[Field], ctx: Context, approved: dict[str, Any] | None = None, use_llm: bool = True) -> list[Answer]:
    approved = approved or {}
    answers = [Answer(field=f) for f in fields]

    with session() as s:
        saved = {row.question_key: row.answer for row in s.scalars(select(SavedAnswer)).all()}

    pending: list[Answer] = []
    for a in answers:
        f = a.field
        if f.key in approved and _valid(f, approved[f.key]) is not None:
            a.value, a.source = _valid(f, approved[f.key]), "approved"
            continue
        value = rule_answer(f, ctx)
        if value is not None:
            a.value, a.source = value, "rule"
            continue
        if f.kind == "file":
            continue
        value = _valid(f, saved.get(question_key(f.label)))
        if value is not None and f.label:
            a.value, a.source = value, "saved"
            continue
        if f.label and f.kind != "checkbox":
            pending.append(a)

    # A lone unlabeled required upload is the résumé.
    files = [a for a in answers if a.field.kind == "file"]
    if ctx.resume_pdf and files and not any(a.value == ctx.resume_pdf for a in files):
        blank = [a for a in files if a.value is None]
        if len(files) == 1 or (blank and blank[0].field.required):
            blank[0].value, blank[0].source = ctx.resume_pdf, "rule"

    if pending and use_llm:
        reply = llm_answers([a.field for a in pending], ctx)
        for a in pending:
            value = _valid(a.field, reply.get(a.field.id))
            if value is not None:
                a.value, a.source = value, "llm"
        # One more try for required questions it skipped.
        skipped = [a for a in pending if a.value is None and a.field.required]
        if skipped and reply:
            retry = llm_answers([a.field for a in skipped], ctx, note=RETRY_NOTE)
            for a in skipped:
                value = _valid(a.field, retry.get(a.field.id))
                if value is not None:
                    a.value, a.source = value, "llm"
    return answers


def remember(receipts: list[dict[str, Any]]) -> None:
    """Save approved LLM/edited answers so the same question is answered identically next time."""
    with session() as s:
        for r in receipts:
            if r.get("source") not in ("llm", "approved") or r.get("kind") in ("file", "checkbox", "textarea") or not r.get("value"):
                continue
            # Reuse choices and short facts only; free-text answers are specific to one employer.
            if not r.get("options") and len(str(r["value"])) > 60:
                continue
            key = question_key(r["label"])
            if not key:
                continue
            value = "; ".join(r["value"]) if isinstance(r["value"], list) else str(r["value"])
            row = s.scalar(select(SavedAnswer).where(SavedAnswer.question_key == key))
            if row:
                row.answer = value
            else:
                s.add(SavedAnswer(question_key=key, question=r["label"], answer=value))
