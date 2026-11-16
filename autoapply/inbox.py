"""Read verification codes from your mailbox over IMAP (optional).

Some employer systems email a code mid-application. With IMAP_HOST/IMAP_USER/IMAP_PASSWORD
set (Gmail: imap.gmail.com + an app password), the engine picks the code up itself.
"""
from __future__ import annotations

import email
import imaplib
import re
import time
from email.utils import parsedate_to_datetime

from .config import settings

CODE = re.compile(r"(?:code|Code|CODE)(?:\s+is)?[^A-Za-z0-9]{1,60}([A-Za-z0-9]{6,8})\b")


def configured() -> bool:
    return bool(settings.imap_host and settings.imap_user and settings.imap_password)


def extract_code(text: str) -> str | None:
    for m in CODE.finditer(text):
        token = m.group(1)
        if any(ch.isdigit() for ch in token) or token.isupper() or len(token) == 8:
            return token
    return None


def _body(msg) -> str:
    parts = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True) or b""
            parts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    return re.sub(r"<[^>]+>", " ", "\n".join(parts))


def wait_for_code(since: float, timeout: int = 90, poll: int = 6) -> str | None:
    """Poll the inbox for a message newer than `since` (epoch seconds) containing a code."""
    if not configured():
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            box = imaplib.IMAP4_SSL(settings.imap_host)
            box.login(settings.imap_user, settings.imap_password)
            box.select("INBOX")
            _, data = box.search(None, "UNSEEN")
            for num in reversed(data[0].split()[-10:]):
                _, raw = box.fetch(num, "(BODY.PEEK[])")
                msg = email.message_from_bytes(raw[0][1])
                try:
                    if parsedate_to_datetime(msg["Date"]).timestamp() < since - 60:
                        continue
                except Exception:
                    pass
                code = extract_code(f"{msg.get('Subject', '')}\n{_body(msg)}")
                if code:
                    box.logout()
                    return code
            box.logout()
        except Exception:
            pass
        time.sleep(poll)
    return None
