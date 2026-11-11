"""Email + password sign-in with server-side sessions (cookie or Bearer token)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_db
from .models import AuthSession, User, utcnow

COOKIE = "lx_session"
SESSION_DAYS = 30


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_password(pw: str, stored: str) -> bool:
    try:
        _, s, h = stored.split("$")
        dk = hashlib.scrypt(pw.encode(), salt=base64.b64decode(s), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(h))
    except Exception:
        return False


def new_session(db: Session, user: User, keep: bool = True) -> AuthSession:
    s = AuthSession(token=secrets.token_urlsafe(32), user_id=user.id,
                    expires_at=utcnow() + timedelta(days=SESSION_DAYS if keep else 1))
    db.add(s)
    db.commit()
    return s


def user_count(db: Session) -> int:
    return db.scalar(select(func.count(User.id))) or 0


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        raise HTTPException(401, "Sign in first.")
    s = db.get(AuthSession, token)
    if not s or s.expires_at < utcnow():
        raise HTTPException(401, "Your session has ended. Sign in again.")
    return s.user


ROLE_CAN = {"Admin": {"view", "edit", "run"}, "Marketing manager": {"view", "edit", "run"},
            "Approver": {"view", "run"}, "Analyst": {"view"}, "Viewer": {"view"}}


def require(user: User, ability: str) -> None:
    if ability not in ROLE_CAN.get(user.role, {"view"}):
        raise HTTPException(403, f"Your role ({user.role}) cannot {ability} here.")
