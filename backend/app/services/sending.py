"""Turns queued Send rows into real emails, with tracking and unsubscribe built in."""
from __future__ import annotations

import logging

from sqlalchemy import select

from ..config import settings
from ..models import Plan, Send
from . import mailer, tracking

log = logging.getLogger(__name__)


def build_mail(ws, s: Send) -> mailer.Mail:
    cfg = ws.settings or {}
    pb = ws.playbook or {}
    url = pb.get("url") or cfg.get("website") or ""
    if url and not url.startswith("http"):
        url = "https://" + url
    lines = s.body_text.split("\n\n")
    greeting, paras = lines[0], lines[1:]
    sign = paras.pop() if paras and paras[-1].startswith("Thanks") else ""
    from ..agents.spec import seg_def, terms, tx
    cta = tx(seg_def(ws, s.seg).get("cta", "") or "", terms(ws))
    unsub = tracking.unsub_url(s.token)
    footer = pb.get("footer") or f"You are receiving this because you are a customer of {ws.name}."
    tracked = tracking.click_url(s.token, url) if url else None
    html = mailer.render_html([greeting] + paras + ([sign] if sign else []), cta, tracked,
                              footer, unsub, tracking.pixel_url(s.token), ws.name)
    text = s.body_text + (f"\n\n{cta}: {tracked}" if tracked and cta else "") + f"\n\n--\n{footer}\nUnsubscribe: {unsub}"
    return mailer.Mail(
        to=s.contact.email, subject=s.subject, text=text, html=html,
        from_email=cfg.get("from_email") or settings.mail_from,
        from_name=cfg.get("from_name") or ws.name,
        reply_to=cfg.get("reply_to") or settings.reply_to,
        headers={"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                 "X-LOOPX-Send": s.token},
        tags=["loopx", f"ws-{ws.id}", s.seg],
    )


def send_batch(ctx, plan: Plan, limit: int, wave_only: bool) -> int:
    db, ws = ctx.db, ctx.ws
    q = select(Send).where(Send.plan_id == plan.id, Send.status == "queued", Send.scheduled_at <= ctx.now)
    if wave_only:
        q = q.where(Send.wave.is_(True))
    n = 0
    for s in db.scalars(q.order_by(Send.scheduled_at, Send.id).limit(max(0, limit))):
        c = s.contact
        from .ingest import undeliverable
        if undeliverable(c.email):
            s.status, s.hold_reason = "skipped", "Demo address, never emailed"
            continue
        if not c.emailable:  # status changed after planning
            s.status, s.hold_reason = "skipped", "Contact no longer emailable"
            continue
        try:
            mid = mailer.send(build_mail(ws, s))
            s.status, s.sent_at, s.message_id, s.error = "sent", ctx.now, mid or "", ""
            c.unopened_streak = (c.unopened_streak or 0) + 1  # reset when they open
            n += 1
        except mailer.SendError as e:
            s.error = str(e)[:500]
            if e.permanent:
                s.status = "bounced"
                c.status = "bounced"
                n += 1
            else:
                s.status = "failed"
            log.warning("Send %s failed: %s", s.id, e)
        db.flush()
    return n
