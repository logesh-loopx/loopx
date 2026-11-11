"""Shared plumbing for the five agents: permission-checked decisions, audit, notifications."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.orm import Session

from ..models import Audit, Contact, Decision, Notification, Workspace
from ..services import clock
from .spec import AGENT_NAMES, level_for

log = logging.getLogger(__name__)

STATUS_FOR_LEVEL = {"auto": "done", "ask": "pending", "suggest": "suggested", "off": "blocked"}


@dataclass
class Ctx:
    db: Session
    ws: Workspace
    now: datetime = field(default_factory=clock.utcnow)  # naive UTC

    @property
    def tz(self) -> str:
        return (self.ws.settings or {}).get("timezone") or "UTC"

    @property
    def country(self) -> str:
        return (self.ws.settings or {}).get("country") or ""

    @property
    def today(self) -> date:
        return clock.local_today(self.tz)

    # ---- the one place agents record what they do ----
    def decide(self, agent: str, action: str, title: str, reason: str = "", conf: int = 100, rule: str = "",
               contact: Contact | None = None, payload: dict | None = None, level: str | None = None,
               status: str | None = None) -> Decision | None:
        """Record a decision. Returns None when the action is switched off (nothing to record).

        status override: 'info' for observations that need no permission.
        """
        lv = level or (level_for(self.ws, agent, action) if action != "info" else "auto")
        if lv == "off" and status is None:
            return None
        d = Decision(workspace_id=self.ws.id, agent=agent, action=action, title=title[:500], reason=reason,
                     conf=int(max(0, min(100, conf))), rule=rule[:255], contact_id=contact.id if contact else None,
                     status=status or STATUS_FOR_LEVEL[lv], payload=payload or {}, created_at=self.now)
        self.db.add(d)
        self.db.flush()
        return d

    def audit(self, text: str, who: str = "LOOPX") -> None:
        self.db.add(Audit(workspace_id=self.ws.id, who=who, text=text, at=self.now))

    def notify(self, text: str, kind: str = "info") -> None:
        self.db.add(Notification(workspace_id=self.ws.id, text=text, kind=kind, at=self.now))

    def state(self, key: str, default=None):
        return (self.ws.state or {}).get(key, default)

    def set_state(self, key: str, value) -> None:
        st = dict(self.ws.state or {})
        st[key] = value
        self.ws.state = st


def agent_label(agent: str) -> str:
    return AGENT_NAMES.get(agent, agent.title())


def who_of(c: Contact | None) -> str:
    if not c:
        return ""
    name = f"{c.first} {c.last}".strip() or c.email
    return f"{name}, {c.company}" if c.company and c.company != name else name


def first_of(c: Contact) -> str:
    return (c.first or "").strip() or c.email.split("@")[0]
