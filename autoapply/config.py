"""Settings, read from environment variables or a .env file in the project root."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("AUTOAPPLY_HOME", Path.cwd()))


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    database_url: str = field(
        default_factory=lambda: os.environ.get("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'autoapply.db'}")
    )
    # LLM provider: "anthropic" (ANTHROPIC_API_KEY) or "gemini" (GEMINI_API_KEY, free tier at
    # https://aistudio.google.com/apikey). Auto-detected from whichever key is set if not given.
    llm_provider: str = field(default_factory=lambda: os.environ.get("AUTOAPPLY_LLM_PROVIDER", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""))
    model: str = field(default_factory=lambda: os.environ.get("AUTOAPPLY_MODEL", ""))

    # Browser
    headless: bool = field(default_factory=lambda: _bool("AUTOAPPLY_HEADLESS", False))
    browser_channel: str = field(default_factory=lambda: os.environ.get("AUTOAPPLY_BROWSER_CHANNEL", "chrome"))

    # Gates. Both default to "stop and ask", like Tsenta with auto-approve off.
    auto_approve_resume: bool = field(default_factory=lambda: _bool("AUTOAPPLY_AUTO_APPROVE_RESUME", False))
    review_before_submit: bool = field(default_factory=lambda: _bool("AUTOAPPLY_REVIEW_BEFORE_SUBMIT", True))

    # Auto Apply
    match_threshold: int = field(default_factory=lambda: int(os.environ.get("AUTOAPPLY_MATCH_THRESHOLD", "70")))
    daily_cap: int = field(default_factory=lambda: int(os.environ.get("AUTOAPPLY_DAILY_CAP", "20")))
    max_age_hours: int = field(default_factory=lambda: int(os.environ.get("AUTOAPPLY_MAX_AGE_HOURS", "72")))

    # Optional mailbox for verification codes (IMAP; for Gmail use an app password)
    imap_host: str = field(default_factory=lambda: os.environ.get("IMAP_HOST", ""))
    imap_user: str = field(default_factory=lambda: os.environ.get("IMAP_USER", ""))
    imap_password: str = field(default_factory=lambda: os.environ.get("IMAP_PASSWORD", ""))

    @property
    def profile_path(self) -> Path:
        return self.data_dir / "profile.yaml"

    @property
    def companies_path(self) -> Path:
        return self.data_dir / "companies.yaml"

    @property
    def artifacts_dir(self) -> Path:
        path = self.data_dir / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
