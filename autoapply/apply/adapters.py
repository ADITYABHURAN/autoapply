"""Per-ATS quirks: where the form lives, how to submit it, what success looks like."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Adapter:
    name: str
    form_root: str | None = None                 # CSS selector scoping field extraction
    open_form: list[str] = field(default_factory=list)   # clicked in order if present, to reveal the form
    submit: list[str] = field(default_factory=list)
    success_url: str = r"$^"
    success_text: str = r"thank you for applying|application (has been |was )?(successfully )?(submitted|received)|thanks for applying|we('ve| have) received your application"


GENERIC_SUBMIT = [
    'button[type="submit"]:visible',
    'input[type="submit"]:visible',
    'button:has-text("Submit application"):visible',
    'button:has-text("Submit"):visible',
]

ADAPTERS = {
    "greenhouse": Adapter(
        name="greenhouse",
        form_root="#application-form, #application_form, form#application",
        open_form=['button:has-text("Apply"):visible'],
        submit=['#application-form button[type="submit"]', '#submit_app', *GENERIC_SUBMIT],
        success_url=r"/confirmation",
    ),
    "lever": Adapter(
        name="lever",
        form_root="#application-form, form.application-form, form",
        submit=["#btn-submit", 'button[data-qa="btn-submit"]', *GENERIC_SUBMIT],
        success_url=r"/thanks",
    ),
    "ashby": Adapter(
        name="ashby",
        form_root='.ashby-application-form-container, form',
        open_form=['#job-application-form', 'a:has-text("Application"):visible'],
        submit=['button.ashby-application-form-submit-button', 'button:has-text("Submit Application"):visible', *GENERIC_SUBMIT],
    ),
    "generic": Adapter(name="generic", form_root=None, submit=GENERIC_SUBMIT),
}


def get_adapter(ats: str) -> Adapter:
    return ADAPTERS.get(ats, ADAPTERS["generic"])


CAPTCHA_FRAMES = re.compile(r"recaptcha/(api2|enterprise)/bframe|hcaptcha\.com/captcha|challenges\.cloudflare\.com", re.I)
CODE_PROMPT = re.compile(r"(verification|security|confirmation) code", re.I)
LOGIN_WALL = re.compile(r"sign in to (apply|continue)|create an account to apply|log in to apply", re.I)


def captcha_challenge_visible(page) -> bool:
    """True when an interactive CAPTCHA challenge is on screen (invisible badges don't count)."""
    for frame in page.frames:
        if not CAPTCHA_FRAMES.search(frame.url or ""):
            continue
        try:
            el = frame.frame_element()
            box = el.bounding_box()
            if box and box["width"] > 100 and box["height"] > 100 and el.is_visible():
                return True
        except Exception:
            continue
    return False
