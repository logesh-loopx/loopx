"""Data for the console's Agents page: the five agents, their settings and what they did today."""
from __future__ import annotations

import copy
from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import auth
from ..agents.analyst import compute_days, lift
from ..agents.base import Ctx
from ..agents.spec import ACTIONS, AGENT_INFO, AGENT_NAMES, RULE_META, level_for, rule
from ..config import settings
from ..db import get_db
from ..models import Audit, Contact, Decision, Plan, Reply, Send, User, Workspace
from ..services import ai, clock, inbox, research

router = APIRouter(prefix="/api")
ORDER = ["timing", "writer", "guard", "analyst", "listener"]
LEVELS = ("off", "suggest", "ask", "auto")


def _ws(db, ws_id: int) -> Workspace:
    ws = db.get(Workspace, ws_id)
    if not ws:
        raise HTTPException(404, "Workspace not found.")
    return ws


def _stats(db, ws: Workspace, ctx: Ctx, plan: Plan | None, agent: str) -> list[dict]:
    """Three real numbers per agent for its card."""
    S = (plan.summary or {}) if plan else {}
    start = ctx.now - timedelta(hours=24)
    if agent == "timing":
        c = S.get("counts") or {}
        return [{"label": "In today's plan", "value": S.get("emails", 0) + S.get("holdout", 0)},
                {"label": "Holdout", "value": S.get("holdout", 0)},
                {"label": "Waiting (below certainty)", "value": c.get("below_certainty", 0)}]
    if agent == "writer":
        return [{"label": "Emails written today", "value": S.get("emails", 0)},
                {"label": "Personalised by AI", "value": S.get("ai_personalised", 0)},
                {"label": "Subject tests running", "value": len((ws.state or {}).get("tests") or {})}]
    if agent == "guard":
        sup = db.scalar(select(func.count(Contact.id)).where(Contact.workspace_id == ws.id,
                                                             (Contact.status != "active") | Contact.subscribed.is_(False))) or 0
        return [{"label": "Held today", "value": S.get("held", 0)},
                {"label": "Never emailed (opted out)", "value": sup},
                {"label": "Test wave", "value": S.get("wave", 0)}]
    if agent == "analyst":
        L = lift(compute_days(ctx, 35), 30, ctx.today)
        return [{"label": "Emails, last 30 days", "value": L["contacted"]},
                {"label": "Returned after an email", "value": f"{L['with_rate']}%"},
                {"label": "Returned without (holdout)", "value": f"{L['without_rate']}%" if L["without_rate"] is not None else "–"}]
    # listener
    rep = db.scalar(select(func.count(Reply.id)).where(Reply.workspace_id == ws.id, Reply.received_at >= start)) or 0
    uns = db.scalar(select(func.count(Reply.id)).where(Reply.workspace_id == ws.id, Reply.received_at >= start,
                                                       Reply.category == "unsubscribe")) or 0
    return [{"label": "Replies read today", "value": rep}, {"label": "Unsubscribed on request", "value": uns},
            {"label": "Reply inbox", "value": "On" if inbox.configured(ws) else "Off"}]


def _extras(ws: Workspace, agent: str) -> list[dict]:
    """Other settings an agent depends on, shown on its settings tab."""
    st = ws.settings or {}
    if agent == "timing":
        return [{"k": "timezone", "type": "text", "label": "Client's time zone", "value": st.get("timezone", "UTC"),
                 "help": "For example Asia/Kolkata. Used when a customer has no time zone of their own."},
                {"k": "country", "type": "text", "label": "Bank-holiday country", "value": st.get("country", ""),
                 "help": "Two letters, for example IN, US or GB. Empty means no holiday calendar."}]
    if agent == "listener":
        return [{"k": "owner_email", "type": "text", "label": "Team email for complaints and alerts", "value": st.get("owner_email", "")},
                {"k": "reply_to", "type": "text", "label": "Reply-to address (the inbox the Listener reads)", "value": st.get("reply_to", "")}]
    if agent == "guard":
        return [{"k": "_daily", "type": "info", "label": "Daily sending limit (all clients)", "value": f"{settings.daily_send_limit} emails",
                 "help": "Set DAILY_SEND_LIMIT on the server. Brevo's free plan allows 300."},
                {"k": "_wave", "type": "info", "label": "Wait before checking the test wave", "value": f"{settings.wave_wait_minutes} minutes",
                 "help": "Set WAVE_WAIT_MINUTES on the server."}]
    if agent == "analyst":
        bm = (ws.state or {}).get("benchmark") or {}
        r = research.status()
        val = (f"About {bm['open_rate']}% opened" + (f", {bm['click_rate']}% clicked" if bm.get("click_rate") else "")) if bm.get("open_rate") else \
            ("Tavily is on; the next lookup runs this month" if r["on"] else "Add a free Tavily key under Connections, AI")
        return [{"k": "_bench", "type": "info", "label": "Industry benchmark", "value": val, "help": bm.get("source", "")},
                {"k": "_attr", "type": "info", "label": "A purchase counts as a return within", "value": f"{settings.attribution_days} days of an email",
                 "help": "Set ATTRIBUTION_DAYS on the server."}]
    if agent == "writer":
        return [{"k": "_ai", "type": "info", "label": "AI for personalising", "value": " → ".join(ai.active_chain()) or "off (rules only)",
                 "help": "Change providers under Connections, AI."}]
    return []


def _agent_out(db, ws: Workspace, ctx: Ctx, plan, agent: str, decs_today: dict) -> dict:
    cfg = (ws.agents or {}).get(agent, {})
    rules = []
    for m in RULE_META[agent]:
        r = dict(m)
        if m["type"] != "locked":
            r["value"] = rule(ws, agent, m["k"])
        rules.append(r)
    actions = []
    for k, (label, default, locked) in ACTIONS[agent].items():
        actions.append({"k": k, "label": label, "default": default, "locked": locked,
                        "level": level_for(ws, agent, k) if not ws.agents_paused else ("off" if not locked else default),
                        "override": ((cfg.get("perms") or {}).get(k) if not locked else None)})
    mine = decs_today.get(agent, [])
    last = next((d for d in mine if d.status != "info"), mine[0] if mine else None)
    return {"key": agent, "name": AGENT_NAMES[agent], **AGENT_INFO[agent],
            "on": bool(cfg.get("on", True)), "level": cfg.get("level", "approve"),
            "rules": rules, "actions": actions, "extras": _extras(ws, agent), "stats": _stats(db, ws, ctx, plan, agent),
            "today": len(mine), "pending": sum(1 for d in mine if d.status in ("pending", "suggested")),
            "last": {"title": last.title, "at": last.created_at.isoformat() + "Z", "status": last.status} if last else None}


@router.get("/workspaces/{ws_id}/agents")
def agents_page(ws_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    ws = _ws(db, ws_id)
    ctx = Ctx(db, ws)
    plan = db.scalar(select(Plan).where(Plan.workspace_id == ws.id, Plan.day == ctx.today))
    since = ctx.now - timedelta(hours=24)
    decs: dict[str, list] = {}
    q = select(Decision).where(Decision.workspace_id == ws.id,
                               (Decision.created_at >= since) | Decision.status.in_(("pending", "suggested")))
    for d in db.scalars(q.order_by(Decision.created_at.desc(), Decision.id.desc())):
        decs.setdefault(d.agent, []).append(d)
    return {"paused": ws.agents_paused, "canEdit": user.role in ("Admin", "Marketing manager"),
            "canRun": user.role in ("Admin", "Marketing manager", "Approver"),
            "ai": {"on": ai.available(), "chain": ai.active_chain()},
            "lastTick": (ws.state or {}).get("last_tick", ""), "plan": plan.status if plan else "none",
            "agents": [_agent_out(db, ws, ctx, plan, a, decs) for a in ORDER]}


def _coerce(meta: dict, v):
    t = meta["type"]
    if t in ("range", "number"):
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise HTTPException(400, f"“{meta['label']}” needs a number.")
        x = max(meta["min"], min(meta["max"], x))
        return int(x) if float(meta.get("step", 1)).is_integer() and float(x).is_integer() else round(x, 2)
    if t == "toggle":
        return bool(v)
    if t == "choice":
        opts = [o[0] for o in meta["options"]]
        if str(v) not in opts:
            raise HTTPException(400, f"“{meta['label']}” must be one of {', '.join(opts)}.")
        return str(v)
    if t == "time":
        h, m = str(v).split(":")[:2]
        return f"{max(0, min(23, int(h))):02d}:{max(0, min(59, int(m))):02d}"
    return str(v)[:500]


@router.patch("/workspaces/{ws_id}/agents/{agent}")
def save_agent(ws_id: int, agent: str, body: dict = Body(...), user: User = Depends(auth.current_user),
               db: Session = Depends(get_db)):
    """body: {on?, level?, rules?: {key: value}, perms?: {action: level|null}, extras?: {key: value}}"""
    if agent not in RULE_META:
        raise HTTPException(404, "Unknown agent.")
    ws = _ws(db, ws_id)
    only_switch = set(body) <= {"on"}
    auth.require(user, "run" if only_switch else "edit")
    agents = copy.deepcopy(ws.agents or {})
    cfg = dict(agents.get(agent) or {})
    changes = []
    if "on" in body:
        cfg["on"] = bool(body["on"])
        changes.append("switched on" if cfg["on"] else "switched off")
    if "level" in body:
        if body["level"] not in ("suggest", "approve", "auto"):
            raise HTTPException(400, "Level must be suggest, approve or auto.")
        cfg["level"], cfg["perms"] = body["level"], {}  # a new level resets per-action overrides, like the console
        changes.append({"suggest": "suggests only", "approve": "acts after the daily approval", "auto": "acts on its own"}[body["level"]])
    meta = {m["k"]: m for m in RULE_META[agent]}
    rules = dict(cfg.get("rules") or {})
    opt = dict(ws.opt or {})
    for k, v in (body.get("rules") or {}).items():
        m = meta.get(k)
        if not m or m["type"] == "locked":
            continue
        v = _coerce(m, v)
        if m.get("bind") == "s":
            cfg["s"] = v
        elif m.get("bind") == "cap":
            ws.cap = int(v)
        elif m.get("bind") == "holdout":
            opt["holdout"] = v
        else:
            rules[k] = v
        changes.append(f"{m['label'].lower()}: {v}")
    cfg["rules"] = rules
    perms = dict(cfg.get("perms") or {})
    for k, v in (body.get("perms") or {}).items():
        spec = ACTIONS[agent].get(k)
        if not spec or spec[2]:
            continue
        if v in (None, "", "default"):
            perms.pop(k, None)
            changes.append(f"{spec[0].lower()}: back to the level default")
        elif v in LEVELS:
            perms[k] = v
            changes.append(f"{spec[0].lower()}: {v}")
    cfg["perms"] = perms
    agents[agent] = cfg
    ws.agents = agents
    ws.opt = opt
    allowed_extras = {"timing": {"timezone", "country"}, "listener": {"owner_email", "reply_to"}}.get(agent, set())
    if body.get("extras"):
        st = dict(ws.settings or {})
        for k, v in body["extras"].items():
            if k in allowed_extras:
                v = str(v or "").strip()
                if k == "timezone" and v and clock.zone(v).key == "UTC" and v != "UTC":
                    raise HTTPException(400, f"“{v}” is not a time zone. Use a name like Asia/Kolkata.")
                st[k] = v.upper() if k == "country" else v
                changes.append(f"{k.replace('_', ' ')}: {v or 'empty'}")
        ws.settings = st
    if changes:
        db.add(Audit(workspace_id=ws.id, who=user.name or user.email,
                     text=f"{AGENT_NAMES[agent]} settings changed: " + "; ".join(changes) + "."))
    db.commit()
    return agents_page(ws_id, user, db)
