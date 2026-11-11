"""Endpoints that don't need a console sign-in: tracking, unsubscribe, Events API, cron, webhooks."""
from __future__ import annotations

import base64
import hmac
import html
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import orchestrator
from ..agents.base import Ctx
from ..config import settings
from ..db import get_db
from ..models import Audit, Contact, Send, Workspace
from ..services import clock, ingest, tracking

router = APIRouter()
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def _send(db, token: str) -> Send | None:
    return db.scalar(select(Send).where(Send.token == token))


# ---------------------------------------------------------------- tracking
@router.get("/t/o/{token}.gif")
def opened(token: str, db: Session = Depends(get_db)):
    s = _send(db, token)
    if s and not s.opened_at:
        s.opened_at = clock.utcnow()
        s.contact.unopened_streak = 0
        db.commit()
    return Response(GIF, media_type="image/gif", headers={"Cache-Control": "no-store, max-age=0"})


@router.get("/t/c/{token}")
def clicked(token: str, u: str, s: str = "", db: Session = Depends(get_db)):
    if not tracking.valid_click(token, u, s) or urlparse(u).scheme not in ("http", "https"):
        raise HTTPException(400, "This link is not valid.")
    snd = _send(db, token)
    if snd:
        now = clock.utcnow()
        snd.clicked_at = snd.clicked_at or now
        snd.opened_at = snd.opened_at or now
        snd.contact.unopened_streak = 0
        db.commit()
    return RedirectResponse(u, status_code=302)


def _unsub(db, s: Send) -> None:
    c = s.contact
    if c.status == "active":
        c.status = "unsubscribed"
    c.subscribed = False
    s.unsub_at = s.unsub_at or clock.utcnow()
    for q in db.scalars(select(Send).where(Send.contact_id == c.id, Send.status.in_(("queued", "planned")))):
        q.status, q.hold_reason = "skipped", "Unsubscribed"
    db.add(Audit(workspace_id=s.workspace_id, who="Customer", text=f"{c.email} unsubscribed with the email link."))
    db.commit()


PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1"><title>{t}</title></head>
<body style="font:16px/1.5 Arial,sans-serif;color:#1E1638;background:#F6F5FB;margin:0">
<div style="max-width:440px;margin:12vh auto;background:#fff;padding:32px;border-radius:14px;box-shadow:0 6px 30px rgba(30,22,56,.08)">{b}</div></body></html>"""


@router.get("/u/{token}", response_class=HTMLResponse)
def unsub_page(token: str, db: Session = Depends(get_db)):
    s = _send(db, token)
    if not s:
        return HTMLResponse(PAGE.format(t="Link expired", b="<h2>This link has expired.</h2>"), status_code=404)
    ws = db.get(Workspace, s.workspace_id)
    if not s.contact.subscribed:
        return PAGE.format(t="Unsubscribed", b=f"<h2>You are unsubscribed.</h2><p>{html.escape(ws.name)} will not email {html.escape(s.contact.email)} again.</p>")
    return PAGE.format(t="Unsubscribe", b=f"<h2>Stop emails from {html.escape(ws.name)}?</h2>"
                       f"<p>{html.escape(s.contact.email)} will not get reminders any more.</p>"
                       f"<form method=post><button style='background:#5B3DF5;color:#fff;border:0;padding:12px 20px;border-radius:8px;font-size:15px;cursor:pointer'>Unsubscribe</button></form>")


@router.post("/u/{token}", response_class=HTMLResponse)
def unsub_post(token: str, db: Session = Depends(get_db)):
    """Also the RFC 8058 one-click target (List-Unsubscribe-Post)."""
    s = _send(db, token)
    if not s:
        raise HTTPException(404, "Link expired.")
    _unsub(db, s)
    ws = db.get(Workspace, s.workspace_id)
    return PAGE.format(t="Unsubscribed", b=f"<h2>You are unsubscribed.</h2><p>{html.escape(ws.name)} will not email you again.</p>")


# ---------------------------------------------------------------- Events API (your own systems)
def _ws_by_key(db, key: str | None) -> Workspace:
    ws = db.scalar(select(Workspace).where(Workspace.api_key == (key or ""))) if key else None
    if not ws:
        raise HTTPException(401, "Send the workspace API key in the X-API-Key header.")
    return ws


@router.post("/api/v1/events")
def events_api(body: dict | list = Body(...), x_api_key: str | None = Header(None), db: Session = Depends(get_db)):
    """POST {"events":[{"email","at","kind":"purchase","amount","external_id","product"}]} or one event object."""
    ws = _ws_by_key(db, x_api_key)
    evs = body if isinstance(body, list) else body.get("events") or [body]
    if len(evs) > 5000:
        raise HTTPException(413, "Send at most 5,000 events per call.")
    return ingest.add_events(db, ws, evs, source="api")


@router.post("/api/v1/contacts")
def contacts_api(body: dict | list = Body(...), x_api_key: str | None = Header(None), db: Session = Depends(get_db)):
    ws = _ws_by_key(db, x_api_key)
    rows = body if isinstance(body, list) else body.get("contacts") or [body]
    return ingest.upsert_contacts(db, ws, rows, source="api")


# ---------------------------------------------------------------- cron (free hosts sleep; a cron service wakes them)
def _check_cron(secret: str | None):
    if not settings.cron_secret or not hmac.compare_digest(settings.cron_secret, secret or ""):
        raise HTTPException(401, "Set CRON_SECRET on the server and pass it as ?secret= or the X-Cron-Secret header.")


@router.api_route("/cron/tick", methods=["GET", "POST"])
def cron_tick(secret: str | None = None, x_cron_secret: str | None = Header(None), db: Session = Depends(get_db)):
    _check_cron(secret or x_cron_secret)
    return {"ok": True, "results": orchestrator.tick_all(db)}


# ---------------------------------------------------------------- Brevo webhooks (bounces, spam, unsubscribes)
@router.post("/webhooks/brevo")
async def brevo_hook(request: Request, secret: str | None = None, db: Session = Depends(get_db)):
    _check_cron(secret)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Expected JSON.")
    events = payload if isinstance(payload, list) else [payload]
    n = 0
    for e in events:
        kind = str(e.get("event") or "").lower()
        mid = e.get("message-id") or e.get("messageId") or ""
        s = db.scalar(select(Send).where(Send.message_id == mid)) if mid else None
        if not s:
            continue
        c, now = s.contact, clock.utcnow()
        if kind in ("hard_bounce", "invalid_email", "blocked"):
            s.status, c.status = "bounced", "bounced"
        elif kind == "spam":
            s.complained_at, c.status, c.subscribed = now, "complained", False
        elif kind == "unsubscribed":
            _unsub(db, s)
        elif kind in ("opened", "unique_opened") and not s.opened_at:
            s.opened_at, c.unopened_streak = now, 0
        elif kind == "click" and not s.clicked_at:
            s.clicked_at, c.unopened_streak = now, 0
        n += 1
    db.commit()
    return {"ok": True, "handled": n}
