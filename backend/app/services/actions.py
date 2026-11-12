"""What happens when a person approves, declines or undoes an agent decision."""
from __future__ import annotations

from sqlalchemy import select

from ..models import Contact, Decision, Plan, Reply, Send
from . import mailer

NOT_UNDOABLE = {"noop", "plan_pick", "forward", "send_answer", "release_guard"}


def apply_payload(ctx, p: dict) -> str:
    """Carry out a decision's action. Returns a short note for the audit log."""
    db, ws = ctx.db, ctx.ws
    op = (p or {}).get("op", "noop")
    if op in ("noop", "plan_pick"):
        return ""
    if op == "set_status":
        c = db.get(Contact, p["contact_id"])
        if c and c.workspace_id == ws.id:
            c.status = p["status"]
            if p["status"] in ("unsubscribed",):
                c.subscribed = False
            _cancel_queued(db, c.id)
        return f"{c.email if c else 'contact'} set to {p['status']}"
    if op == "update_email":
        c = db.get(Contact, p["contact_id"])
        taken = db.scalar(select(Contact.id).where(Contact.workspace_id == ws.id, Contact.email == p["email"]))
        if c and not taken:
            c.email = p["email"]
            return f"email changed to {p['email']}"
        return "address already in use, nothing changed"
    if op == "forward":
        r = db.get(Reply, p.get("reply_id"))
        to = p.get("to") or ""
        if r and "@" in to:
            mailer.send(mailer.Mail(to=to, subject=f"[LOOPX] Complaint from {r.from_email}: {r.subject}"[:200],
                                    text=f"{r.from_email} wrote:\n\n{r.body}\n\n-- Passed on by the LOOPX Listener agent.",
                                    html=f"<p>{r.from_email} wrote:</p><pre style='white-space:pre-wrap'>{r.body}</pre>"
                                         f"<p>-- Passed on by the LOOPX Listener agent.</p>",
                                    reply_to=r.from_email))
            return f"forwarded to {to}"
        return "no team address set, shown in notifications only"
    if op == "resume_plan":
        plan = db.get(Plan, p["plan_id"])
        if plan and plan.status == "paused":
            plan.status = "sending"
        return "send resumed"
    if op == "pause_seg":
        segs = set(ctx.state("paused_segs", []))
        segs.add(p["seg"])
        ctx.set_state("paused_segs", sorted(segs))
        for s in db.scalars(select(Send).where(Send.workspace_id == ws.id, Send.seg == p["seg"], Send.status.in_(("queued", "planned")))):
            s.status, s.hold_reason = "skipped", "Campaign paused"
        return f"campaign {p['seg']} paused"
    if op == "set_winner":
        w = dict(ctx.state("winners", {}) or {})
        w[p["seg"]] = p["variant"]
        ctx.set_state("winners", w)
        return f"subject {p['variant']} now used for {p['seg']}"
    if op == "add_subject":
        pb = dict(ws.playbook or {})
        segs = dict(pb.get("segs") or {})
        sd = dict(segs.get(p["seg"]) or {})
        sd["alt"] = p["subject"]
        segs[p["seg"]] = sd
        pb["segs"] = segs
        ws.playbook = pb
        w = dict(ctx.state("winners", {}) or {})
        w.pop(p["seg"], None)
        ctx.set_state("winners", w)
        return "new subject added as version B"
    if op == "release_guard":
        rv = dict(ctx.state("guard_reviews", {}) or {})
        if p["seg"] in rv:
            rv[p["seg"]] = {**rv[p["seg"]], "released": True}
            ctx.set_state("guard_reviews", rv)
        for s in db.scalars(select(Send).where(Send.workspace_id == ws.id, Send.seg == p["seg"], Send.status == "held",
                                               Send.hold_reason == "guard-ai")):
            plan = db.get(Plan, s.plan_id) if s.plan_id else None
            s.status = "queued" if plan and plan.status in ("wave", "sending", "approved") else "planned"
            s.hold_reason = ""
        return f"emails for {p['seg']} released by a person"
    if op == "send_answer":
        r = db.get(Reply, p.get("reply_id"))
        if not r:
            return "reply not found"
        cfg = ws.settings or {}
        from ..config import settings as S

        mailer.send(mailer.Mail(to=r.from_email, subject=("Re: " + r.subject) if not r.subject.lower().startswith("re:") else r.subject,
                                text=p["text"], html="<p>" + p["text"].replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>") + "</p>",
                                from_email=cfg.get("from_email") or S.mail_from, from_name=cfg.get("from_name") or ws.name,
                                reply_to=cfg.get("reply_to") or S.reply_to,
                                headers={"In-Reply-To": r.message_id, "References": r.message_id} if r.message_id.startswith("<") else {}))
        return f"answer sent to {r.from_email}"
    if op == "approve_template":
        appr = dict(ctx.state("approved_tpl", {}) or {})
        appr[p["seg"]] = p["hash"]
        ctx.set_state("approved_tpl", appr)
        for s in db.scalars(select(Send).where(Send.workspace_id == ws.id, Send.seg == p["seg"], Send.status == "held",
                                               Send.hold_reason == "review")):
            plan = db.get(Plan, s.plan_id) if s.plan_id else None
            s.status = "queued" if plan and plan.status in ("wave", "sending", "approved") else "planned"
            s.hold_reason = ""
        return f"email for {p['seg']} approved"
    return ""


def undo_payload(ctx, p: dict) -> str | None:
    """Reverse a decision. Returns None if it cannot be undone."""
    db = ctx.db
    op = (p or {}).get("op", "noop")
    if op in NOT_UNDOABLE:
        return None
    if op == "set_status":
        if p["status"] == "unsubscribed":
            return None  # an unsubscribe request is always honoured
        c = db.get(Contact, p["contact_id"])
        restored = 0
        if c:
            c.status = p.get("prev") or "active"
            c.subscribed = c.status == "active" or c.subscribed
            if c.emailable:  # give back the emails the decision cancelled, if today's send is still running
                for s in db.scalars(select(Send).where(Send.contact_id == c.id, Send.status == "skipped",
                                                       Send.hold_reason == "Contact no longer emailable", Send.sent_at.is_(None))):
                    plan = db.get(Plan, s.plan_id) if s.plan_id else None
                    if plan and plan.status in ("draft", "wave", "sending", "paused"):
                        s.status, s.hold_reason = ("planned" if plan.status == "draft" else "queued"), ""
                        restored += 1
        return f"{c.email if c else 'contact'} back to {c.status if c else ''}" + (f", {restored} email(s) back in today's plan" if restored else "")
    if op == "update_email":
        c = db.get(Contact, p["contact_id"])
        if c and c.email == p["email"]:
            c.email = p["prev"]
        return "email address restored"
    if op == "pause_seg":
        segs = set(ctx.state("paused_segs", []))
        segs.discard(p["seg"])
        ctx.set_state("paused_segs", sorted(segs))
        return f"campaign {p['seg']} running again"
    if op == "set_winner":
        w = dict(ctx.state("winners", {}) or {})
        w.pop(p["seg"], None)
        ctx.set_state("winners", w)
        return "winner cleared"
    return None


def _cancel_queued(db, contact_id: int) -> None:
    for s in db.scalars(select(Send).where(Send.contact_id == contact_id, Send.status.in_(("queued", "planned")))):
        s.status, s.hold_reason = "skipped", "Contact no longer emailable"


def decide(ctx, d: Decision, verdict: str, who: str) -> dict:
    """verdict: approve | decline | undo"""
    from ..agents.base import agent_label

    label = agent_label(d.agent)
    if verdict == "approve":
        if d.status not in ("pending", "suggested"):
            return {"ok": False, "error": "This decision is not waiting for approval."}
        note = apply_payload(ctx, d.payload)
        d.status, d.decided_by = "approved", who
        ctx.audit(f"{label}: approved: {d.title.rstrip('.?')}." + (f" ({note})" if note else ""), who)
    elif verdict == "decline":
        if d.status not in ("pending", "suggested"):
            return {"ok": False, "error": "This decision is not waiting for approval."}
        d.status, d.decided_by = "declined", who
        ctx.audit(f"{label}: declined: {d.title.rstrip('.?')}.", who)
    elif verdict == "undo":
        if d.status not in ("done", "approved"):
            return {"ok": False, "error": "Only completed decisions can be undone."}
        note = undo_payload(ctx, d.payload)
        if note is None:
            return {"ok": False, "error": "This one cannot be undone." + (" Unsubscribe requests are always honoured." if d.payload.get("status") == "unsubscribed" else "")}
        d.status, d.decided_by = "undone", who
        ctx.audit(f"{label}: undid: {d.title.rstrip('.?')}. ({note})", who)
    else:
        return {"ok": False, "error": "Unknown verdict."}
    return {"ok": True, "status": d.status}
