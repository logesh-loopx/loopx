"""Create .env from .env.example on first run, with fresh random secrets."""
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
env, example = root / ".env", root / ".env.example"
if not env.exists():
    text = example.read_text()
    text = text.replace("SECRET_KEY=change-me", "SECRET_KEY=" + secrets.token_hex(32))
    text = text.replace("CRON_SECRET=change-me-too", "CRON_SECRET=" + secrets.token_urlsafe(24))
    env.write_text(text)
    print("Created .env with new secrets. Edit it to add your email provider and Claude key.")
