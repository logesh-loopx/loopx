"""Signed links for open tracking, click tracking and one-click unsubscribe."""
from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import quote

from ..config import settings


def _sig(*parts: str) -> str:
    mac = hmac.new(settings.secret_key.encode(), "|".join(parts).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:12]).decode().rstrip("=")


def pixel_url(token: str) -> str:
    return f"{settings.public_url}/t/o/{token}.gif"


def click_url(token: str, target: str) -> str:
    return f"{settings.public_url}/t/c/{token}?u={quote(target, safe='')}&s={_sig(token, target)}"


def valid_click(token: str, target: str, sig: str) -> bool:
    return hmac.compare_digest(_sig(token, target), sig or "")


def unsub_url(token: str) -> str:
    return f"{settings.public_url}/u/{token}"
