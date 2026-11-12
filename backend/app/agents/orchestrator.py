"""Runs the five agents. One `tick()` per workspace, every few minutes:

  1. Listener reads new replies
  2. store connectors sync (hourly)
  3. after PLAN_HOUR local time: Timing -> Writer -> Guard build today's plan
  4. an approved plan is sent: test wave first, Guard checks it, then the full send
  5. Writer checks subject tests; Timing learns send hours
  6. Analyst updates numbers, alerts and the weekly report
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select

from ..config import settings
from ..models import Decision, Plan, Send, Workspace
from ..services import clock, connectors
from ..services.actions import apply_payload
from ..services.sending import send_batch
from .analyst import AnalystAgent
from .base import Ctx
from .guard import GuardAgent, quota_left
from .listener import ListenerAgent
from .spec import is_on, level_for, rule
from .timing import TimingAgent, learn_send_hours
from .writer import WriterAgent, personalize, template_hash

log = logging.getLogger(__name__)
PLAN_OPS = {"plan_pick", "approve_template", "noop"}


def build_plan(ctx: Ctx, day: date | None = None, force: bool = False) -> Plan:
    db, ws = ctx.db, ctx.ws
    day = day or ctx.today
    plan = db.scalar(select(Plan).where(Plan.workspace_id == ws.id, Plan.day == day))
    if plan and not force:
        return plan
    topup = False
    if plan and force and plan.status == "done":
        # Today's emails are out; add anyone who became due since (e.g. newly added test people).
        topup = True
        plan.status, plan.created_at, plan.wave_done_at = "draft", ctx.now, None
        plan.approved_at, plan.approved_by = None, ""
        db.flush()
    elif plan and force:
        if plan.status not in ("draft", "empty"):
            return plan  # never rebuild a plan that is already sending
        db.query(Send).filter(Send.plan_id == plan.id).delete()
        db.query(Decision).filter(Decision.workspace_id == ws.id, Decision.created_at >= plan.created_at,
                                  Decision.agent.in_(("timing", "writer", "guard")), Decision.status.in_(("pending", "suggested", "done", "info")),
                                  ).delete(synchronize_session=False)
        db.delete(plan)
        db.flush()
    if not topup:
        plan = Plan(workspace_id=ws.id, day=day, status="draft", created_at=ctx.now)
        db.add(plan)
        db.flush()

    if not is_on(ws, "timing"):
        plan.status, plan.summary = "empty", {"note": "The Timing agent is off or all agents are paused."}
        return plan
    picks, counts = TimingAgent(ctx).select(day)
    if is_on(ws, "writer"):
        rows, wstats = WriterAgent(ctx).compose(picks)
    else:
        plan.status, plan.summary = "empty", {"note": "The Writer agent is off, so no emails were written.", **counts}
        return plan
    gstats = GuardAgent(ctx).screen(rows) if is_on(ws, "guard") else {"wave": 0}
    if not is_on(ws, "guard"):  # Guard's locked rules still apply: never email suppressed contacts
        rows = [r for r in rows if r["pick"].contact.emailable]

    per_customer_tz = rule(ws, "timing", "tz") == "customer"
    for r in rows:
        p = r["pick"]
        tz = p.contact.tz if (per_customer_tz and p.contact.tz) else ctx.tz
        when = clock.to_utc_naive(day, p.hour, p.minute, tz)
        if (p.contact.extra or {}).get("test"):
            when = min(when, ctx.now)  # test people get their email as soon as the plan is approved
        status = "holdout" if p.holdout else r["status"]
        db.add(Send(workspace_id=ws.id, plan_id=plan.id, contact_id=p.contact.id, seg=p.seg, campaign=r["campaign"],
                    subject=r["subject"], variant=r["variant"], body_text=r["body"], status=status,
                    hold_reason=r["hold"] if status == "held" else "", wave=bool(r.get("wave")), certainty=p.a.prob,
                    scheduled_at=when))
    goes = lambda r: not r["pick"].holdout and (r["status"] == "planned" or r["hold"] == "review")
    n_send = sum(1 for r in rows if goes(r))
    plan.summary = {"emails": n_send, "holdout": sum(1 for r in rows if r["pick"].holdout),
                    "held": sum(1 for r in rows if r["status"] == "held" and not goes(r) and not r["pick"].holdout),
                    "wave": gstats.get("wave", 0), "counts": counts, "writer": wstats, "guard": gstats,
                    "segs": _seg_counts(rows)}
    if not rows:
        plan.status = "done" if topup else "empty"
        return plan
    personalize(ctx, plan, max_calls=1)  # the first batch (test wave first); the rest on the next runs
    if level_for(ws, "timing", "pick") == "auto":
        approve_plan(ctx, plan, "Timing agent (acts on its own)")
    else:
        ctx.notify(f"Today's plan is ready: {n_send} emails are waiting for your approval.")
    return plan


def _seg_counts(rows) -> dict:
    out: dict = {}
    for r in rows:
        o = out.setdefault(r["pick"].seg, {"n": 0, "send": 0, "holdout": 0, "held": 0, "campaign": r["campaign"]})
        o["n"] += 1
        if r["pick"].holdout:
            o["holdout"] += 1
        elif r["status"] == "held" and r["hold"] != "review":
            o["held"] += 1
        else:
            o["send"] += 1
    return out


def approve_plan(ctx: Ctx, plan: Plan, who: str) -> dict:
    db, ws = ctx.db, ctx.ws
    if plan.status != "draft":
        return {"ok": False, "error": f"The plan is already {plan.status}."}
    if ws.agents_paused:
        return {"ok": False, "error": "All agents are paused. Resume them first."}
    plan.status, plan.approved_at, plan.approved_by = "wave", ctx.now, who
    # the approval covers the plan's pending agent decisions and the email wording
    for d in db.scalars(select(Decision).where(Decision.workspace_id == ws.id, Decision.created_at >= plan.created_at,
                                               Decision.agent.in_(("timing", "writer", "guard")),
                                               Decision.status.in_(("pending", "suggested")))):
        op = (d.payload or {}).get("op", "noop")
        if op in PLAN_OPS:
            apply_payload(ctx, d.payload)
            d.status, d.decided_by = "approved", who
    for s in db.scalars(select(Send).where(Send.plan_id == plan.id, Send.status == "planned")):
        s.status = "queued"
    plan.summary = {**(plan.summary or {}), "approved_templates": sorted({s for s in (plan.summary or {}).get("segs", {})})}
    if not db.scalar(select(Send.id).where(Send.plan_id == plan.id, Send.wave.is_(True), Send.status == "queued")):
        plan.status = "sending"
    ctx.audit(f"Today's plan approved by {who}: {(plan.summary or {}).get('emails', 0)} emails.", who)
    ctx.notify(f"Today's plan was approved by {who}. {(plan.summary or {}).get('emails', 0)} emails are scheduled.")
    return {"ok": True}


def send_step(ctx: Ctx) -> int:
    db, ws = ctx.db, ctx.ws
    total = 0
    for plan in db.scalars(select(Plan).where(Plan.workspace_id == ws.id, Plan.status.in_(("wave", "sending")))):
        left = min(settings.send_batch_size, quota_left(db, ctx.now))
        if left <= 0:
            if not ctx.state("quota_note") == ctx.today.isoformat():
                ctx.set_state("quota_note", ctx.today.isoformat())
                ctx.decide("guard", "info", f"Daily sending limit of {settings.daily_send_limit} reached",
                           "The rest of today's emails go out when the limit resets.", 100, "DAILY_SEND_LIMIT", status="info")
            break
        if plan.status == "wave":
            total += send_batch(ctx, plan, left, wave_only=True)
            waiting = db.scalar(select(Send.id).where(Send.plan_id == plan.id, Send.wave.is_(True), Send.status == "queued"))
            if waiting:
                continue
            if plan.wave_done_at is None:
                plan.wave_done_at = ctx.now
            if ctx.now >= plan.wave_done_at + timedelta(minutes=settings.wave_wait_minutes):
                if GuardAgent(ctx).check_wave(plan):
                    plan.status = "sending"
        if plan.status == "sending":
            total += send_batch(ctx, plan, left, wave_only=False)
            if not db.scalar(select(Send.id).where(Send.plan_id == plan.id, Send.status == "queued")):
                plan.status = "done"
                ctx.notify(f"Every email in the plan for {plan.day:%b %d} has been sent.")
    return total


def tick(db, ws: Workspace, now: datetime | None = None, force_plan: bool = False) -> dict:
    ctx = Ctx(db, ws, now or clock.utcnow())
    out: dict = {"workspace": ws.id}
    if ws.agents_paused:
        out["paused"] = True
        return out
    if is_on(ws, "listener"):
        out["replies"] = ListenerAgent(ctx).poll()
    last_sync = ctx.state("last_sync_at")
    if (ws.settings or {}).get("connectors") and (not last_sync or ctx.now - datetime.fromisoformat(last_sync) > timedelta(hours=1)):
        ctx.set_state("last_sync_at", ctx.now.isoformat())
        out["sync"] = connectors.sync_all(db, ws)
    local = clock.local_now(ctx.tz)
    if force_plan or local.hour >= settings.plan_hour:
        plan = build_plan(ctx)
        out["plan"] = {"id": plan.id, "status": plan.status}
    # Writer keeps personalising today's emails before they go out (a few AI calls per run)
    for plan in db.scalars(select(Plan).where(Plan.workspace_id == ws.id, Plan.status.in_(("draft", "wave", "sending")))):
        out["personalised"] = out.get("personalised", 0) + personalize(ctx, plan, max_calls=3)
    out["sent"] = send_step(ctx)
    if is_on(ws, "writer"):
        w = WriterAgent(ctx)
        w.check_winners()
        w.suggest_draft()
    if is_on(ws, "timing"):
        learn_send_hours(ctx)
    if is_on(ws, "analyst"):
        AnalystAgent(ctx).run()
    ctx.set_state("last_tick", ctx.now.isoformat())
    db.commit()
    return out


def tick_all(db) -> list[dict]:
    res = []
    for ws in db.scalars(select(Workspace)):
        try:
            res.append(tick(db, ws))
        except Exception as e:  # one broken workspace must not stop the others
            db.rollback()
            log.exception("tick failed for workspace %s", ws.id)
            res.append({"workspace": ws.id, "error": str(e)[:300]})
    return res
