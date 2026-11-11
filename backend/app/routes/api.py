"""JSON API used by the LOOPX console."""
from __future__ import annotations

import copy
from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import auth
from ..agents import orchestrator
from ..agents.analyst import compute_days, lift
from ..agents.base import Ctx, agent_label, who_of
from ..agents.listener import ListenerAgent
from ..agents.spec import ACTIONS, AGENT_NAMES
from ..config import settings
from ..db import DB_URL, get_db
from ..models import (DEFAULT_AGENTS, DEFAULT_OPT, Audit, Contact, DailyMetric, Decision, Notification, Plan, Reply,
                      Send, User, Workspace)
from ..services import ai, clock, connectors, inbox, ingest, mailer
from ..services.actions import decide as decide_action

router = APIRouter(prefix="/api")
SECRET_KEYS = {"password", "token", "key", "secret"}


# ---------------------------------------------------------------- helpers
def _ws(db: Session, ws_id: int) -> Workspace:
    ws = db.get(Workspace, ws_id)
    if not ws:
        raise HTTPException(404, "Workspace not found.")
    return ws


def _mask(d):
    if isinstance(d, dict):
        return {k: ("••••" + str(v)[-4:] if k in SECRET_KEYS and v else _mask(v)) for k, v in d.items()}
    return d


def _user_out(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role}


def _ws_out(ws: Workspace, full: bool = False) -> dict:
    o = {"id": ws.id, "name": ws.name, "industry": ws.industry, "theme": ws.theme, "agents_paused": ws.agents_paused}
    if full:
        o.update(settings=_mask(ws.settings or {}), playbook=ws.playbook or {}, agents=ws.agents or {},
                 opt=ws.opt or {}, cap=ws.cap, api_key=ws.api_key, state={k: v for k, v in (ws.state or {}).items()
                                                                           if k in ("winners", "paused_segs", "last_tick", "imap_error", "tests")})
    return o


def _contact_ui(c: Contact) -> dict:
    """The compact shape the console uses: {id,e,f,l,co,fa,la,n,sp,sub,tg,src,at,rt}."""
    return {"id": "s" + str(c.id), "sid": c.id, "e": c.email, "f": c.first, "l": c.last, "co": c.company,
            "fa": c.first_at.isoformat() if c.first_at else "", "la": c.last_at.isoformat() if c.last_at else "",
            "n": c.n_orders, "sp": c.spend, "sub": c.emailable, "tg": c.tags, "src": c.source,
            "at": c.created_at.strftime("%b %d, %Y") if c.created_at else "", "rt": "", "st": c.status}


def _dec_out(d: Decision) -> dict:
    c = d.contact
    return {"id": d.id, "agent": d.agent, "agentName": AGENT_NAMES.get(d.agent, d.agent), "action": d.action,
            "title": d.title, "reason": d.reason, "conf": d.conf, "rule": d.rule, "status": d.status,
            "at": d.created_at.isoformat() + "Z", "who": who_of(c) if c else "", "contact": c.email if c else "",
            "contactId": c.id if c else None, "decidedBy": d.decided_by,
            "undoable": d.status in ("done", "approved") and (d.payload or {}).get("op") not in ("noop", "plan_pick", "forward", None)
                        and not ((d.payload or {}).get("status") == "unsubscribed")}


# ---------------------------------------------------------------- health & auth
@router.get("/health")
def health(db: Session = Depends(get_db)):
    return {"ok": True, "mail": mailer.provider_name(), "ai": ai.available(),
            "db": "postgres" if DB_URL.startswith("postgresql") else "sqlite", "users": auth.user_count(db),
            "signup": settings.allow_signup or auth.user_count(db) == 0, "scheduler": settings.run_scheduler,
            "daily_limit": settings.daily_send_limit, "public_url": settings.public_url}


@router.post("/auth/signup")
def signup(response: Response, body: dict = Body(...), db: Session = Depends(get_db)):
    first = auth.user_count(db) == 0
    if not first and not settings.allow_signup:
        raise HTTPException(403, "Sign-up is closed. Ask an admin to add you.")
    email = ingest.clean_email(body.get("email"))
    if not email:
        raise HTTPException(400, "Enter a valid work email address.")
    if len(str(body.get("password") or "")) < 8:
        raise HTTPException(400, "The password needs at least 8 characters.")
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "That email already has an account. Sign in instead.")
    role = "Admin" if first else "Viewer"  # an admin raises roles later (PATCH /api/team/{id})
    u = User(email=email, name=str(body.get("name") or "").strip()[:255], role=role,
             password_hash=auth.hash_password(body["password"]))
    db.add(u)
    db.commit()
    s = auth.new_session(db, u, bool(body.get("keep", True)))
    response.set_cookie(auth.COOKIE, s.token, httponly=True, samesite="lax", secure=settings.public_url.startswith("https"),
                        max_age=auth.SESSION_DAYS * 86400 if body.get("keep", True) else None)
    return {"user": _user_out(u), "token": s.token}


@router.post("/auth/login")
def login(response: Response, body: dict = Body(...), db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.email == str(body.get("email") or "").strip().lower()))
    if not u or not auth.check_password(str(body.get("password") or ""), u.password_hash):
        raise HTTPException(401, "The email or password is not right.")
    s = auth.new_session(db, u, bool(body.get("keep", True)))
    response.set_cookie(auth.COOKIE, s.token, httponly=True, samesite="lax", secure=settings.public_url.startswith("https"),
                        max_age=auth.SESSION_DAYS * 86400 if body.get("keep", True) else None)
    return {"user": _user_out(u), "token": s.token}


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    t = request.cookies.get(auth.COOKIE)
    if t:
        db.query(auth.AuthSession).filter_by(token=t).delete()
        db.commit()
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@router.get("/auth/me")
def me(user: User = Depends(auth.current_user)):
    return {"user": _user_out(user)}


@router.get("/team")
def team(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    return {"team": [_user_out(u) for u in db.scalars(select(User).order_by(User.id))]}


@router.post("/team")
def add_member(body: dict = Body(...), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if user.role != "Admin":
        raise HTTPException(403, "Only an admin can add people.")
    email = ingest.clean_email(body.get("email"))
    if not email or len(str(body.get("password") or "")) < 8:
        raise HTTPException(400, "Give an email and a temporary password of 8+ characters.")
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "That person already has an account.")
    u = User(email=email, name=str(body.get("name") or ""), role=body.get("role") if body.get("role") in auth.ROLE_CAN else "Viewer",
             password_hash=auth.hash_password(body["password"]))
    db.add(u)
    db.commit()
    return {"user": _user_out(u)}


@router.patch("/team/{uid}")
def set_role(uid: int, body: dict = Body(...), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if user.role != "Admin":
        raise HTTPException(403, "Only an admin can change roles.")
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "No such person.")
    if body.get("role") in auth.ROLE_CAN:
        if u.id == user.id and body["role"] != "Admin":
            raise HTTPException(400, "You cannot remove your own admin role.")
        u.role = body["role"]
    if body.get("password"):
        if len(str(body["password"])) < 8:
            raise HTTPException(400, "Passwords need 8+ characters.")
        u.password_hash = auth.hash_password(str(body["password"]))
    db.commit()
    return {"user": _user_out(u)}


# ---------------------------------------------------------------- workspaces
@router.get("/workspaces")
def list_ws(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    return {"workspaces": [_ws_out(w) for w in db.scalars(select(Workspace).order_by(Workspace.id))]}


@router.post("/workspaces")
def create_ws(body: dict = Body(...), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    auth.require(user, "edit")
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Give the client a name.")
    st = dict(body.get("settings") or {})
    st.setdefault("timezone", "Asia/Kolkata")
    st.setdefault("owner_email", user.email)
    ws = Workspace(name=name[:255], industry=body.get("industry") or "store", theme=body.get("theme") or "violet",
                   playbook=body.get("playbook") or {}, settings=st, agents=copy.deepcopy(DEFAULT_AGENTS),
                   opt=copy.deepcopy(DEFAULT_OPT))
    db.add(ws)
    db.flush()
    db.add(Audit(workspace_id=ws.id, who=user.name or user.email, text=f"Workspace {name} created."))
    db.commit()
    return {"workspace": _ws_out(ws, True)}


@router.get("/workspaces/{ws_id}")
def get_ws(ws_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    return {"workspace": _ws_out(_ws(db, ws_id), True)}


@router.patch("/workspaces/{ws_id}")
def patch_ws(ws_id: int, body: dict = Body(...), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    ws = _ws(db, ws_id)
    keys = set(body)
    if keys & {"agents_paused"} and keys <= {"agents_paused"}:
        auth.require(user, "run")
    else:
        auth.require(user, "edit")
    for k in ("name", "industry", "theme"):
        if k in body and body[k]:
            setattr(ws, k, str(body[k])[:255])
    if "playbook" in body and isinstance(body["playbook"], dict):
        ws.playbook = body["playbook"]
    if "agents" in body and isinstance(body["agents"], dict):
        cur = copy.deepcopy(ws.agents or {})
        for k, v in body["agents"].items():
            if k in DEFAULT_AGENTS and isinstance(v, dict):
                cur[k] = {**cur.get(k, {}), **{kk: vv for kk, vv in v.items() if kk in ("on", "level", "s", "rules", "perms")}}
        ws.agents = cur
    if "opt" in body and isinstance(body["opt"], dict):
        ws.opt = {**(ws.opt or {}), **{k: v for k, v in body["opt"].items() if k in ("goal", "explore", "holdout")}}
    if "cap" in body:
        ws.cap = max(1, min(7, int(body["cap"])))
    if "agents_paused" in body:
        ws.agents_paused = bool(body["agents_paused"])
        db.add(Audit(workspace_id=ws.id, who=user.name or user.email,
                     text="All agents paused." if ws.agents_paused else "All agents resumed."))
    if "settings" in body and isinstance(body["settings"], dict):
        cur = dict(ws.settings or {})
        for k, v in body["settings"].items():
            if k == "connectors" or k == "imap":
                continue  # set through their own endpoints
            cur[k] = v
        if isinstance(body["settings"].get("imap"), dict):
            im = dict(cur.get("imap") or {})
            for k, v in body["settings"]["imap"].items():
                if not (k == "password" and str(v).startswith("••••")):
                    im[k] = v
            cur["imap"] = im
        ws.settings = cur
    db.commit()
    return {"workspace": _ws_out(ws, True)}


@router.delete("/workspaces/{ws_id}")
def delete_ws(ws_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if user.role != "Admin":
        raise HTTPException(403, "Only an admin can delete a workspace.")
    db.delete(_ws(db, ws_id))
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- contacts & data
@router.get("/workspaces/{ws_id}/contacts")
def contacts(ws_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    _ws(db, ws_id)
    cs = db.scalars(select(Contact).where(Contact.workspace_id == ws_id).order_by(Contact.id))
    return {"contacts": [_contact_ui(c) for c in cs]}


@router.post("/workspaces/{ws_id}/contacts")
def add_contacts(ws_id: int, body: dict = Body(...), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """Accepts the console's mapped rows ({e,f,l,co,fa,la,n,sp,sub,tg}) or plain field names."""
    auth.require(user, "edit")
    ws = _ws(db, ws_id)
    rows = []
    for r in body.get("contacts") or []:
        rows.append({"email": r.get("e") or r.get("email"), "first": r.get("f") or r.get("first"),
                     "last": r.get("l") or r.get("last"), "company": r.get("co") or r.get("company"),
                     "first_at": r.get("fa") or r.get("first_at"), "last_at": r.get("la") or r.get("last_at"),
                     "n_orders": r.get("n") if r.get("n") not in (None, "") else r.get("n_orders"),
                     "spend": r.get("sp") if r.get("sp") not in (None, "") else r.get("spend"),
                     "subscribed": r.get("sub", r.get("subscribed", True)), "tags": r.get("tg") or r.get("tags"),
                     "tz": r.get("tz")})
    res = ingest.upsert_contacts(db, ws, rows, source=str(body.get("source") or "file")[:40])
    db.add(Audit(workspace_id=ws.id, who=user.name or user.email,
                 text=f"Imported customers: {res['added']} added, {res['updated']} updated, {res['skipped']} skipped."))
    db.commit()
    return res


@router.post("/workspaces/{ws_id}/import")
async def import_csv(ws_id: int, request: Request, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    auth.require(user, "edit")
    ws = _ws(db, ws_id)
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/"):
        form = await request.form()
        f = form.get("file")
        text = (await f.read()).decode("utf-8", "replace") if f else ""
    else:
        text = (await request.body()).decode("utf-8", "replace")
    res = ingest.import_csv(db, ws, text)
    if "error" in res:
        raise HTTPException(400, res["error"])
    return res


@router.patch("/workspaces/{ws_id}/contacts/{cid}")
def patch_contact(ws_id: int, cid: int, body: dict = Body(...), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    auth.require(user, "edit")
    c = db.get(Contact, cid)
    if not c or c.workspace_id != ws_id:
        raise HTTPException(404, "Customer not found.")
    if "status" in body and body["status"] in ("active", "unsubscribed", "bounced", "complained", "closed"):
        if c.status == "unsubscribed" and body["status"] == "active" and not body.get("consent"):
            raise HTTPException(400, "This customer unsubscribed. Re-subscribe only with their new consent (send consent: true).")
        c.status = body["status"]
        c.subscribed = body["status"] == "active"
    for k in ("first", "last", "company", "tags", "tz"):
        if k in body:
            setattr(c, k, str(body[k])[:255])
    db.commit()
    return {"contact": _contact_ui(c)}


@router.put("/workspaces/{ws_id}/connectors/{name}")
def set_connector(ws_id: int, name: str, body: dict = Body(...), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    auth.require(user, "edit")
    if name not in connectors.FETCHERS:
        raise HTTPException(404, "Unknown connector. Use shopify, stripe or woocommerce.")
    ws = _ws(db, ws_id)
    conns = dict((ws.settings or {}).get("connectors") or {})
    cur = dict(conns.get(name) or {})
    for k, v in body.items():
        if isinstance(v, str) and v.startswith("••••"):
            continue
        cur[k] = v
    conns[name] = cur
    ws.settings = {**(ws.settings or {}), "connectors": conns}
    db.commit()
    return {"connector": _mask(cur)}


@router.post("/workspaces/{ws_id}/connectors/{name}/sync")
def run_sync(ws_id: int, name: str, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    auth.require(user, "run")
    if name not in connectors.FETCHERS:
        raise HTTPException(404, "Unknown connector.")
    return connectors.sync(db, _ws(db, ws_id), name)


# ---------------------------------------------------------------- plan & agents
def _plan_out(db, plan: Plan | None) -> dict | None:
    if not plan:
        return None
    rows = db.execute(select(Send.status, func.count(Send.id)).where(Send.plan_id == plan.id).group_by(Send.status)).all()
    return {"id": plan.id, "day": plan.day.isoformat(), "status": plan.status, "approvedAt": (plan.approved_at.isoformat() + "Z") if plan.approved_at else "",
            "approvedBy": plan.approved_by, "summary": plan.summary or {}, "byStatus": dict(rows)}


@router.get("/workspaces/{ws_id}/plan")
def get_plan(ws_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    ws = _ws(db, ws_id)
    ctx = Ctx(db, ws)
    plan = db.scalar(select(Plan).where(Plan.workspace_id == ws.id, Plan.day == ctx.today))
    sample = []
    if plan:
        for s in db.scalars(select(Send).where(Send.plan_id == plan.id).order_by(Send.id).limit(60)):
            sample.append({"id": s.id, "to": s.contact.email, "who": who_of(s.contact), "seg": s.seg, "campaign": s.campaign,
                           "subject": s.subject, "variant": s.variant, "status": s.status, "hold": s.hold_reason,
                           "wave": s.wave, "certainty": s.certainty, "at": (s.scheduled_at.isoformat() + "Z") if s.scheduled_at else "",
                           "body": s.body_text})
    return {"plan": _plan_out(db, plan), "sends": sample, "today": ctx.today.isoformat()}


@router.post("/workspaces/{ws_id}/plan/build")
def build(ws_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    auth.require(user, "run")
    ws = _ws(db, ws_id)
    plan = orchestrator.build_plan(Ctx(db, ws), force=True)
    db.commit()
    return {"plan": _plan_out(db, plan)}


@router.post("/workspaces/{ws_id}/plan/approve")
def approve(ws_id: int, body: dict | None = Body(None), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    auth.require(user, "run")
    ws = _ws(db, ws_id)
    ctx = Ctx(db, ws)
    plan = db.scalar(select(Plan).where(Plan.workspace_id == ws.id, Plan.day == ctx.today)) or orchestrator.build_plan(ctx)
    skip = [s for s in ((body or {}).get("skip") or []) if isinstance(s, str)]
    if skip and plan.status == "draft":  # segments the person held back on the plan screen
        for s in db.scalars(select(Send).where(Send.plan_id == plan.id, Send.seg.in_(skip), Send.status.in_(("planned", "held")))):
            s.status, s.hold_reason = "skipped", "Held back on the plan screen"
    res = orchestrator.approve_plan(ctx, plan, user.name or user.email)
    if res.get("ok"):
        orchestrator.send_step(ctx)  # start the test wave right away
    db.commit()
    if not res.get("ok"):
        raise HTTPException(400, res["error"])
    return {"plan": _plan_out(db, plan)}


@router.post("/workspaces/{ws_id}/plan/{action}")
def pause_resume(ws_id: int, action: str, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """Pause or resume today's send by hand (Guard can also pause it after the test wave)."""
    if action not in ("pause", "resume"):
        raise HTTPException(404, "Use pause or resume.")
    auth.require(user, "run")
    ws = _ws(db, ws_id)
    ctx = Ctx(db, ws)
    plan = db.scalar(select(Plan).where(Plan.workspace_id == ws.id, Plan.day == ctx.today))
    if not plan:
        raise HTTPException(400, "There is no plan today.")
    who = user.name or user.email
    if action == "pause" and plan.status in ("wave", "sending"):
        plan.summary = {**(plan.summary or {}), "resume_to": plan.status}
        plan.status = "paused"
        ctx.audit("Today's send paused by hand.", who)
    elif action == "resume" and plan.status == "paused":
        plan.status = (plan.summary or {}).get("resume_to") or ("sending" if plan.wave_done_at else "wave")
        for d in db.scalars(select(Decision).where(Decision.workspace_id == ws.id, Decision.action == "resume", Decision.status == "pending")):
            d.status, d.decided_by = "approved", who
        ctx.audit("Today's send resumed by hand.", who)
    db.commit()
    return {"plan": _plan_out(db, plan)}


@router.post("/workspaces/{ws_id}/tick")
def run_now(ws_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    auth.require(user, "run")
    return orchestrator.tick(db, _ws(db, ws_id), force_plan=True)


@router.get("/workspaces/{ws_id}/decisions")
def decisions(ws_id: int, agent: str = "", days: int = 1, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    _ws(db, ws_id)
    since = clock.utcnow() - timedelta(days=max(1, min(days, 90)))
    q = select(Decision).where(Decision.workspace_id == ws_id, (Decision.created_at >= since) | Decision.status.in_(("pending", "suggested")))
    if agent:
        q = q.where(Decision.agent == agent)
    return {"decisions": [_dec_out(d) for d in db.scalars(q.order_by(Decision.created_at.desc(), Decision.id.desc()).limit(300))],
            "actions": {a: {k: {"label": v[0], "default": v[1], "locked": v[2]} for k, v in acts.items()} for a, acts in ACTIONS.items()}}


@router.post("/workspaces/{ws_id}/decisions/{dec_id}/{verdict}")
def decision_verdict(ws_id: int, dec_id: int, verdict: str, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    auth.require(user, "run")
    ws = _ws(db, ws_id)
    d = db.get(Decision, dec_id)
    if not d or d.workspace_id != ws.id:
        raise HTTPException(404, "Decision not found.")
    res = decide_action(Ctx(db, ws), d, verdict, user.name or user.email)
    if not res.get("ok"):
        db.rollback()
        raise HTTPException(400, res["error"])
    db.commit()
    return {"decision": _dec_out(d)}


@router.get("/workspaces/{ws_id}/audit")
def audit(ws_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Audit).where(Audit.workspace_id == ws_id).order_by(Audit.at.desc()).limit(100))
    return {"audit": [{"id": a.id, "who": a.who, "text": a.text, "at": a.at.isoformat() + "Z"} for a in rows]}


@router.post("/workspaces/{ws_id}/audit")
def add_audit(ws_id: int, body: dict = Body(...), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    _ws(db, ws_id)
    db.add(Audit(workspace_id=ws_id, who=user.name or user.email, text=str(body.get("text") or "")[:1000]))
    db.commit()
    return {"ok": True}


@router.get("/workspaces/{ws_id}/notifications")
def notifications(ws_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Notification).where(Notification.workspace_id == ws_id).order_by(Notification.at.desc()).limit(50))
    return {"notifications": [{"id": n.id, "text": n.text, "kind": n.kind, "at": n.at.isoformat() + "Z", "read": n.read} for n in rows]}


@router.post("/workspaces/{ws_id}/notifications/read")
def notifications_read(ws_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    db.query(Notification).filter(Notification.workspace_id == ws_id, Notification.read.is_(False)).update({"read": True})
    db.commit()
    return {"ok": True}


@router.get("/workspaces/{ws_id}/live")
def live(ws_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """Everything the dashboard polls: today's plan progress, agent activity and alerts."""
    ws = _ws(db, ws_id)
    ctx = Ctx(db, ws)
    plan = db.scalar(select(Plan).where(Plan.workspace_id == ws.id, Plan.day == ctx.today))
    rs = {"sent": 0, "opened": 0, "clicked": 0, "replied": 0, "returned": 0, "unsubs": 0, "bounces": 0, "queued": 0, "held": 0, "holdout": 0, "segs": {}}
    if plan:
        for s in db.scalars(select(Send).where(Send.plan_id == plan.id)):
            if s.status in ("sent", "bounced"):
                sg = rs["segs"].setdefault(s.seg, {"sent": 0, "opened": 0, "clicked": 0, "returned": 0})
                sg["sent"] += 1
                sg["opened"] += 1 if s.opened_at else 0
                sg["clicked"] += 1 if s.clicked_at else 0
                sg["returned"] += 1 if s.returned_at else 0
                rs["sent"] += 1
                rs["opened"] += 1 if s.opened_at else 0
                rs["clicked"] += 1 if s.clicked_at else 0
                rs["replied"] += 1 if s.replied_at else 0
                rs["returned"] += 1 if s.returned_at else 0
                rs["unsubs"] += 1 if (s.unsub_at or s.complained_at) else 0
                rs["bounces"] += 1 if s.status == "bounced" else 0
            elif s.status in ("queued", "planned"):
                rs["queued"] += 1
            elif s.status == "held":
                rs["held"] += 1
            elif s.status == "holdout":
                rs["holdout"] += 1
    st = plan.status if plan else "none"
    # the console's progress stages: 0 waiting, 1 test wave out, 2 wave passed, 3 full send, 4 all sent, 5 returns measured
    tick = {"none": 0, "empty": 0, "draft": 0, "wave": 1, "paused": 1, "sending": 3, "done": 4}.get(st, 0)
    if st == "sending" and plan.wave_done_at and rs["queued"] == 0:
        tick = 4
    if st == "done" and rs["returned"]:
        tick = 5
    if st == "sending" and rs["sent"] and tick == 3 and plan.wave_done_at and ctx.now - plan.wave_done_at < timedelta(minutes=2):
        tick = 2
    recent = db.scalars(select(Decision).where(Decision.workspace_id == ws.id).order_by(Decision.created_at.desc(), Decision.id.desc()).limit(40))
    pend = db.scalar(select(func.count(Decision.id)).where(Decision.workspace_id == ws.id, Decision.status.in_(("pending", "suggested")))) or 0
    notes = db.scalars(select(Notification).where(Notification.workspace_id == ws.id).order_by(Notification.at.desc()).limit(20))
    return {"plan": _plan_out(db, plan), "rs": rs, "tick": tick, "paused": ws.agents_paused,
            "decisions": [_dec_out(d) for d in recent], "pending": pend,
            "notifications": [{"id": n.id, "text": n.text, "kind": n.kind, "at": n.at.isoformat() + "Z", "read": n.read} for n in notes],
            "lastTick": (ws.state or {}).get("last_tick", ""), "imapError": (ws.state or {}).get("imap_error", ""),
            "imap": inbox.configured(ws), "mail": mailer.provider_name(), "ai": ai.available(),
            "quotaLeft": max(0, settings.daily_send_limit - (db.scalar(select(func.count(Send.id)).where(
                Send.sent_at >= ctx.now.replace(hour=0, minute=0, second=0, microsecond=0))) or 0))}


@router.get("/workspaces/{ws_id}/report")
def report(ws_id: int, days: int = 30, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    ws = _ws(db, ws_id)
    ctx = Ctx(db, ws)
    data = compute_days(ctx, days=max(7, min(days, 365)))
    today = ctx.today
    series = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        m = data.get(d) or {}
        series.append({"day": d.isoformat(), **{k: m.get(k, 0) for k in ("contacted", "opened", "clicked", "replied", "returned", "unsubs", "bounces", "holdout", "holdout_returned")},
                       "segs": m.get("segs", {})})
    return {"days": series, "lift": lift(data, days, today)}


@router.get("/workspaces/{ws_id}/replies")
def replies(ws_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Reply).where(Reply.workspace_id == ws_id).order_by(Reply.received_at.desc()).limit(100))
    return {"replies": [{"id": r.id, "from": r.from_email, "subject": r.subject, "body": r.body[:1000], "category": r.category,
                         "conf": r.conf, "summary": r.summary, "at": r.received_at.isoformat() + "Z"} for r in rows]}


@router.post("/workspaces/{ws_id}/replies")
def inbound_reply(ws_id: int, body: dict = Body(...), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """Hand a reply to the Listener (for testing, or when replies are forwarded by a script)."""
    auth.require(user, "run")
    ws = _ws(db, ws_id)
    import uuid
    r = ListenerAgent(Ctx(db, ws)).handle(body.get("message_id") or f"<manual-{uuid.uuid4().hex}@loopx>",
                                         str(body.get("from") or ""), str(body.get("subject") or ""), str(body.get("body") or ""),
                                         in_reply_to=str(body.get("in_reply_to") or ""), auto=bool(body.get("auto")))
    db.commit()
    return {"reply": {"id": r.id, "category": r.category, "conf": r.conf} if r else None}


# ---------------------------------------------------------------- AI (the console's window.claude bridge)
@router.post("/ai/sample")
def ai_sample(body: dict = Body(...), user: User = Depends(auth.current_user)):
    if not ai.available():
        raise HTTPException(503, detail={"code": "not_granted", "message": "Set ANTHROPIC_API_KEY on the server to turn on AI."})
    inp = body.get("input")
    opts = body.get("opts") or {}
    tier = {"quick": "quick", "complex": "default"}.get(opts.get("modelTier"), "default")
    if isinstance(inp, list):
        msgs = [{"role": m.get("role", "user"), "content": str(m.get("content", ""))} for m in inp][-20:]
    else:
        msgs = [{"role": "user", "content": str(inp or "")[:60000]}]
    if body.get("json"):
        msgs[-1]["content"] += "\n\nReturn only valid JSON."
    out = ai.text(msgs, tier=tier, max_tokens=4000 if body.get("json") else 1500)
    if out is None:
        raise HTTPException(502, detail={"code": "unavailable", "message": "Claude did not answer."})
    if body.get("json"):
        return {"json": ai.loose_json(out), "text": out}
    return {"text": out, "truncated": False}
