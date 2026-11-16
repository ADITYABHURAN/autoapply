"""Browser launch settings shared by the apply engine and the PDF renderer."""
from __future__ import annotations

import shutil
from contextlib import contextmanager

from ..config import settings


def launch_kwargs() -> dict:
    kwargs: dict = {"headless": settings.headless}
    channel = settings.browser_channel
    # Prefer the installed Chrome; fall back to Playwright's bundled Chromium.
    if channel == "chrome" and not any(shutil.which(x) for x in ("google-chrome", "chrome", "google-chrome-stable")):
        import os
        import sys
        mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        win = os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe")
        if not (sys.platform == "darwin" and os.path.exists(mac)) and not (sys.platform == "win32" and os.path.exists(win)):
            channel = ""
    if channel:
        kwargs["channel"] = channel
    kwargs["args"] = ["--no-sandbox"] if _in_container() else []
    return kwargs


def _in_container() -> bool:
    import os
    return os.path.exists("/.dockerenv") or os.environ.get("AUTOAPPLY_NO_SANDBOX") == "1" or (hasattr(os, "geteuid") and os.geteuid() == 0)


@contextmanager
def open_page():
    """A page in a persistent profile, so cookies and ATS logins survive between runs."""
    from playwright.sync_api import sync_playwright

    profile_dir = settings.data_dir / "browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(profile_dir), viewport={"width": 1280, "height": 900}, **launch_kwargs())
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(15000)
        try:
            yield page
        finally:
            ctx.close()
