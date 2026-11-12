"""Guard agent: suppression, weekly cap, unopened streaks, the test wave and pausing bad sends."""
from __future__ import annotations

import hashlib
import math
from collections import Counter
from datetime import timedelta

from sqlalchemy import func, select

from ..config import settings
from ..models import Contact, Plan, Send
from .base import Ctx, first_of
from .spec import level_for, rule

MIN_WAVE_FOR_RATES = 50  # below this, a couple of unsubscribes would swing the rate too much


class GuardAgent:
    name = "guard"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        ws = ctx.ws
        self.cap = int(rule(ws, "guard", "cap"))
        self.unopened = int(rule(ws, "guard", "unopened"))
        self.wave = int(rule(ws, "guard", "wave"))
        self.unsub_max = float(rule(ws, "guard", "unsub"))
        self.bounce_max = float(rule(ws, "guard", "bounce"))

    def screen(self, rows: list[dict]) -> dict:
        """Apply holds to the Writer's rows in place and choose the test wave."""
        ctx, ws, db = self.ctx, self.ctx.ws, self.ctx.db
        stats = Counter()
        suppressed = dict(db.execute(select(Contact.status, func.count(Contact.id)).where(
            Contact.workspace_id == ws.id, Contact.status != "active").group_by(Contact.status)).all())
        unsub_only = db.scalar(select(func.count(Contact.id)).where(
            Contact.workspace_id == ws.id, Contact.status == "active", Contact.subscribed.is_(False))) or 0
        n_sup = sum(suppressed.values()) + unsub_only
        if n_sup:
            parts = ", ".join(f"{n} {k}" for k, n in suppressed.items())
            ctx.decide("guard", "remove", f"Removed {n_sup} opted-out, bounced or closed addresses before today's plan",
                       f"{parts or str(unsub_only) + ' unsubscribed'}. LOOPX never emails these addresses.", 100, "Always on",
                       payload={"op": "noop"})
        hold_lvl = level_for(ws, "guard", "hold")
        week_ago = ctx.now - timedelta(days=7)
        recent = dict(db.execute(select(Send.contact_id, func.count(Send.id)).where(
            Send.workspace_id == ws.id, Send.status == "sent", Send.sent_at >= week_ago).group_by(Send.contact_id)).all())
        ex_cap = ex_unop = None
        from ..services.ingest import undeliverable
        for r in rows:
            c = r["pick"].contact
            if undeliverable(c.email) and not r["pick"].holdout:
                r["status"], r["hold"] = "held", "Demo address (example.com): it cannot receive email"
                stats["demo"] += 1
                continue
            if r["status"] == "held":
                continue
            if hold_lvl != "off" and recent.get(c.id, 0) >= self.cap:
                r["status"], r["hold"] = "held", f"Weekly limit: {recent[c.id]} emails in the last 7 days"
                stats["cap"] += 1
                ex_cap = ex_cap or c
            elif hold_lvl != "off" and c.unopened_streak >= self.unopened:
                r["status"], r["hold"] = "held", f"Left the last {c.unopened_streak} emails unopened"
                stats["unopened"] += 1
                ex_unop = ex_unop or c
        if stats["demo"]:
            ctx.decide("guard", "remove", f"Held {stats['demo']} demo addresses that cannot receive email",
                       "Addresses at example.com and other reserved test domains are never emailed. Add real people to test sending.",
                       100, "Always on", payload={"op": "noop"}, level="auto")
        if stats["cap"]:
            ctx.decide("guard", "hold", f"Held {stats['cap']} customers at the weekly limit",
                       f"{first_of(ex_cap)} already had {recent.get(ex_cap.id)} emails in the last 7 days, your limit is {self.cap}.",
                       100, f"At most {self.cap} in 7 days", contact=ex_cap, payload={"op": "noop"})
        if stats["unopened"]:
            ctx.decide("guard", "hold", f"Stopped emailing {stats['unopened']} customers until they engage",
                       f"{first_of(ex_unop)} left the last {ex_unop.unopened_streak} emails unopened.", 97,
                       f"{self.unopened} unopened in a row", contact=ex_unop, payload={"op": "noop"})
        # test wave: a spread-out sample of the sendable emails goes first
        sendable = [r for r in rows if not r["pick"].holdout and
                    (r["status"] == "planned" or (r["status"] == "held" and r["hold"] == "review"))]
        sendable.sort(key=lambda r: hashlib.md5(str(r["pick"].contact.id).encode()).hexdigest())
        # Big lists: the rule's wave size. Smaller lists: the first fifth goes out alone.
        wave_n = self.wave if len(sendable) > self.wave else (max(1, math.ceil(len(sendable) * 0.2)) if len(sendable) > 1 else len(sendable))
        for r in sendable[:wave_n]:
            r["wave"] = True
        if sendable:
            ctx.decide("guard", "pause", f"Test wave set to {wave_n} customers",
                       f"The full send waits until unsubscribes stay under {self.unsub_max}% and bounces under {self.bounce_max}%.",
                       100, f"Test wave of {self.wave}", payload={"op": "noop"})
        return dict(stats, wave=wave_n)

    # ---------------------------------------------------------------
    def check_wave(self, plan: Plan) -> bool:
        """Called once the test wave has had time to land. Returns True if the full send may continue."""
        ctx, db = self.ctx, self.ctx.db
        wave = list(db.scalars(select(Send).where(Send.plan_id == plan.id, Send.wave.is_(True))))
        sent = [s for s in wave if s.status in ("sent", "bounced")]
        n = len(sent)
        if not n:
            return True
        bounces = sum(1 for s in sent if s.status == "bounced")
        unsubs = sum(1 for s in sent if s.unsub_at or s.complained_at or s.contact.status in ("unsubscribed", "complained"))
        ur, br = 100 * unsubs / n, 100 * bounces / n
        small = n < MIN_WAVE_FOR_RATES
        bad_u = (unsubs >= 3) if small else ur > self.unsub_max
        bad_b = (bounces >= 3) if small else br > self.bounce_max
        summary = f"unsubscribes {ur:.1f}% ({unsubs}), bounces {br:.1f}% ({bounces}) on {n} emails"
        if bad_u or bad_b:
            plan.status = "paused"
            ctx.decide("guard", "pause", "Paused today's send after the test wave",
                       f"Test wave: {summary}. Limits: unsubscribes {self.unsub_max}%, bounces {self.bounce_max}%."
                       + (" (Small wave: paused at 3 or more.)" if small else ""), 100, "Pause a send that passes a limit",
                       payload={"op": "noop"})
            ctx.decide("guard", "resume", "Resume today's paused send?",
                       "Check the replies and the list source first. Resuming sends the rest of today's plan.", 70,
                       "Resume needs a person", payload={"op": "resume_plan", "plan_id": plan.id})
            ctx.notify(f"Guard paused today's send: {summary}.", "alert")
            return False
        ctx.decide("guard", "pause", "Checked the test wave and let the full send continue",
                   f"Test wave: {summary}.", 100, f"Unsubscribes under {self.unsub_max}%, bounces under {self.bounce_max}%",
                   payload={"op": "noop"}, status="done")
        ctx.notify(f"The test wave passed: {summary}. The full send has started.")
        return True


def sent_today_all_workspaces(db, now) -> int:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(select(func.count(Send.id)).where(Send.sent_at >= start, Send.status.in_(("sent", "bounced")))) or 0


def quota_left(db, now) -> int:
    return max(0, settings.daily_send_limit - sent_today_all_workspaces(db, now))
