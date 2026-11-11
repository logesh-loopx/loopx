"""All settings come from environment variables (or a .env file)."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # optional
    pass


def _env(name: str, default: str = "") -> str:
    """Empty values in .env count as 'not set'."""
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip()


def _bool(name: str, default: bool) -> bool:
    v = _env(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


class Settings:
    # --- core ---
    database_url: str = _env("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'loopx.db'}")
    secret_key: str = _env("SECRET_KEY") or secrets.token_hex(32)
    public_url: str = _env("PUBLIC_URL", "http://localhost:8000").rstrip("/")
    allow_signup: bool = _bool("ALLOW_SIGNUP", True)  # first user can always sign up

    # --- email sending: console | smtp | brevo ---
    mail_provider: str = _env("MAIL_PROVIDER", "console").lower()
    mail_from: str = _env("MAIL_FROM", "reminders@example.com")
    mail_from_name: str = _env("MAIL_FROM_NAME", "LOOPX")
    reply_to: str = _env("REPLY_TO", "")
    smtp_host: str = _env("SMTP_HOST", "")
    smtp_port: int = int(_env("SMTP_PORT", "587"))
    smtp_user: str = _env("SMTP_USER", "")
    smtp_password: str = _env("SMTP_PASSWORD", "")
    smtp_ssl: bool = _bool("SMTP_SSL", False)  # True for port 465
    brevo_api_key: str = _env("BREVO_API_KEY", "")
    daily_send_limit: int = int(_env("DAILY_SEND_LIMIT", "300"))  # Brevo free plan
    send_batch_size: int = int(_env("SEND_BATCH_SIZE", "50"))

    # --- replies (Listener) ---
    imap_host: str = _env("IMAP_HOST", "")
    imap_user: str = _env("IMAP_USER", "")
    imap_password: str = _env("IMAP_PASSWORD", "")
    imap_folder: str = _env("IMAP_FOLDER", "INBOX")

    # --- Claude ---
    anthropic_api_key: str = _env("ANTHROPIC_API_KEY", "")
    fast_model: str = _env("FAST_MODEL", "claude-haiku-5-5")
    smart_model: str = _env("SMART_MODEL", "claude-sonnet-5-5")

    # --- scheduling ---
    cron_secret: str = _env("CRON_SECRET", "")
    run_scheduler: bool = _bool("RUN_SCHEDULER", True)
    tick_seconds: int = int(_env("TICK_SECONDS", "300"))
    plan_hour: int = int(_env("PLAN_HOUR", "6"))  # local hour the daily plan is built
    wave_wait_minutes: int = int(_env("WAVE_WAIT_MINUTES", "60"))
    attribution_days: int = int(_env("ATTRIBUTION_DAYS", "7"))

    # --- local dev ---
    seed_demo: bool = _bool("SEED_DEMO", False)
    frontend_file: Path = Path(_env("FRONTEND_FILE", str(ROOT / "frontend" / "index.html")))


settings = Settings()
