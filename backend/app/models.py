"""Database tables. Works on SQLite (local) and Postgres (hosted)."""
from __future__ import annotations

import copy
import secrets
from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, Index)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # stored as naive UTC


def new_key(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(24)


# Default agent settings, matching the LOOPX console's agentsCfg / opt / cap.
DEFAULT_AGENTS = {
    "timing": {"on": True, "level": "approve", "s": 70},
    "writer": {"on": True, "level": "approve", "s": 2},
    "guard": {"on": True, "level": "auto", "s": 0.5},
    "analyst": {"on": True, "level": "auto", "s": 15},
    "listener": {"on": True, "level": "suggest", "s": 80},
}
DEFAULT_OPT = {"goal": 70, "explore": 10, "holdout": 5}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(40), default="Admin")
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuthSession(Base):
    __tablename__ = "sessions"
    token: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    user: Mapped[User] = relationship()


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    industry: Mapped[str] = mapped_column(String(40), default="store")
    theme: Mapped[str] = mapped_column(String(40), default="violet")
    api_key: Mapped[str] = mapped_column(String(80), default=lambda: new_key("lx_"), unique=True)
    # sender, reply-to, timezone, country, website, connector credentials, IMAP override...
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    playbook: Mapped[dict] = mapped_column(JSON, default=dict)   # segment emails from the setup wizard
    agents: Mapped[dict] = mapped_column(JSON, default=lambda: copy.deepcopy(DEFAULT_AGENTS))
    opt: Mapped[dict] = mapped_column(JSON, default=lambda: copy.deepcopy(DEFAULT_OPT))
    cap: Mapped[int] = mapped_column(Integer, default=2)          # most emails per customer in 7 days
    agents_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[dict] = mapped_column(JSON, default=dict)       # agent memory: last runs, winners...
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("workspace_id", "email"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255))
    first: Mapped[str] = mapped_column(String(120), default="")
    last: Mapped[str] = mapped_column(String(120), default="")
    company: Mapped[str] = mapped_column(String(255), default="")
    first_at: Mapped[date | None] = mapped_column(Date, nullable=True)   # first purchase / visit
    last_at: Mapped[date | None] = mapped_column(Date, nullable=True)    # latest purchase
    n_orders: Mapped[int] = mapped_column(Integer, default=0)
    spend: Mapped[float] = mapped_column(Float, default=0.0)
    subscribed: Mapped[bool] = mapped_column(Boolean, default=True)
    # active | unsubscribed | bounced | complained | closed
    status: Mapped[str] = mapped_column(String(20), default="active")
    tags: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(40), default="file")
    tz: Mapped[str] = mapped_column(String(64), default="")
    send_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)  # learned from opens
    unopened_streak: Mapped[int] = mapped_column(Integer, default=0)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def emailable(self) -> bool:
        return self.subscribed and self.status == "active"


class Event(Base):
    """A purchase, visit or other activity. Purchases drive the Timing agent."""
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("workspace_id", "external_id"),
                      Index("ix_events_ws_contact_at", "workspace_id", "contact_id", "at"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20), default="purchase")  # purchase | visit | refund
    at: Mapped[datetime] = mapped_column(DateTime)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    product: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(String(40), default="api")
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Plan(Base):
    """Today's plan: who gets which email, built by Timing + Writer + Guard."""
    __tablename__ = "plans"
    __table_args__ = (UniqueConstraint("workspace_id", "day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)
    # draft (waiting for approval) | approved | wave (test wave out) | sending | paused | done | empty
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_by: Mapped[str] = mapped_column(String(255), default="")
    wave_done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)


class Send(Base):
    __tablename__ = "sends"
    __table_args__ = (Index("ix_sends_ws_status", "workspace_id", "status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), nullable=True, index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    seg: Mapped[str] = mapped_column(String(20), default="")
    campaign: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    variant: Mapped[str] = mapped_column(String(4), default="A")
    body_text: Mapped[str] = mapped_column(Text, default="")
    # planned | holdout | held | queued | sent | failed | bounced | skipped
    status: Mapped[str] = mapped_column(String(20), default="planned")
    hold_reason: Mapped[str] = mapped_column(String(255), default="")
    wave: Mapped[bool] = mapped_column(Boolean, default=False)
    certainty: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    unsub_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    complained_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    token: Mapped[str] = mapped_column(String(64), default=lambda: secrets.token_urlsafe(16), unique=True, index=True)
    message_id: Mapped[str] = mapped_column(String(255), default="")
    error: Mapped[str] = mapped_column(String(500), default="")
    ai_done: Mapped[bool] = mapped_column(Boolean, default=False)   # Writer personalised it with AI
    ai_note: Mapped[str] = mapped_column(String(255), default="")
    contact: Mapped[Contact] = relationship()


class Decision(Base):
    """Everything an agent decides, with the permission outcome."""
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    agent: Mapped[str] = mapped_column(String(20), index=True)
    action: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(500))
    reason: Mapped[str] = mapped_column(Text, default="")
    conf: Mapped[int] = mapped_column(Integer, default=100)
    rule: Mapped[str] = mapped_column(String(255), default="")
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True)
    # done | pending (ask) | suggested | approved | declined | undone | info | blocked (off)
    status: Mapped[str] = mapped_column(String(20), default="done")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    decided_by: Mapped[str] = mapped_column(String(255), default="")
    contact: Mapped[Contact | None] = relationship()


class Reply(Base):
    __tablename__ = "replies"
    __table_args__ = (UniqueConstraint("workspace_id", "message_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True)
    send_id: Mapped[int | None] = mapped_column(ForeignKey("sends.id", ondelete="SET NULL"), nullable=True)
    message_id: Mapped[str] = mapped_column(String(255))
    from_email: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    category: Mapped[str] = mapped_column(String(30), default="")
    conf: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(String(500), default="")
    new_email: Mapped[str] = mapped_column(String(255), default="")


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    who: Mapped[str] = mapped_column(String(255), default="LOOPX")
    text: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20), default="info")  # info | alert | report
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)


class AppSetting(Base):
    """Server-wide settings edited in the console (AI providers, Tavily). Overrides .env."""
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DailyMetric(Base):
    """Analyst's per-day numbers, used by Reports and alerts."""
    __tablename__ = "daily_metrics"
    __table_args__ = (UniqueConstraint("workspace_id", "day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
