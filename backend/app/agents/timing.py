"""Timing agent: who is due, when to send, holidays and weekends, who to leave alone.

The segment maths is a direct port of the console's analyse(), so the numbers the
UI shows and the plan the server builds always agree.
"""
from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select

from ..config import settings
from ..models import Contact, Event, Send
from ..services import clock
from .base import Ctx, first_of
from .spec import rule

REMIND = ("wk", "bi", "mo")


def jsround(x: float) -> int:
    return int(math.floor(x + 0.5))


@dataclass
class Analysis:
    seg: str            # wk | bi | mo | re | ra | alone | '' (unsubscribed)
    why: str            # due | wait | risk | dormant | once | nohist | unsub
    prob: int           # certainty, %
    gap: int | None
    since: int | None
    due: int | None     # days until due (negative = overdue)


def slot_for(ws, gap: int) -> str:
    cycles = (ws.playbook or {}).get("cycles") or [["wk", 10], ["bi", 21], ["mo", 1e9]]
    for seg, mx in cycles:
        if gap <= mx:
            return seg
    return cycles[-1][0]


def analyse(ws, c: Contact, today: date, lead: int) -> Analysis:
    la, fa = c.last_at, c.first_at
    n = c.n_orders if c.n_orders > 0 else (2 if fa and la and la > fa else 1 if (la or fa) else 0)
    since = max(0, (today - la).days) if la else None
    if not c.emailable:
        return Analysis("", "unsub", 0, None, since, None)
    if la is None and not n:
        return Analysis("alone", "nohist", 10, None, since, None)
    if n <= 1:
        return Analysis("alone", "once", 18 if since is not None and since < 21 else 6, None, since, None)
    gap = max(3, jsround((la - fa).days / (n - 1))) if fa and la and la > fa else None
    if gap is None:
        if since is not None and since > 90:
            return Analysis("ra", "dormant", 7, None, since, None)
        return Analysis("alone", "nohist", 12, None, since, None)
    due = gap - since
    reg = min(1.0, n / 8)
    if since > max(3 * gap, 60):
        return Analysis("ra", "dormant", 6 + jsround(reg * 6), gap, since, due)
    if since > gap * 1.5 + 2:
        return Analysis("re", "risk", 18 + jsround(reg * 14), gap, since, due)
    if due <= lead:
        return Analysis(slot_for(ws, gap), "due", 55 + jsround(reg * 37), gap, since, due)
    return Analysis("alone", "wait", 55 + jsround(reg * 37), gap, since, due)


def in_holdout(contact_id: int, day: date, pct: float) -> bool:
    if pct <= 0:
        return False
    h = int(hashlib.sha256(f"{contact_id}:{day.isoformat()}".encode()).hexdigest()[:8], 16)
    return (h % 10000) < pct * 100


@dataclass
class Pick:
    contact: Contact
    a: Analysis
    seg: str
    hour: int
    minute: int
    holdout: bool
    due_date: date | None
    moved_for: str = ""


class TimingAgent:
    name = "timing"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        ws = ctx.ws
        self.thr = int(rule(ws, "timing", "certainty"))
        self.lead = int(rule(ws, "timing", "lead"))
        self.from_hm = clock.hhmm(rule(ws, "timing", "from"), (7, 0))
        self.to_hm = clock.hhmm(rule(ws, "timing", "to"), (20, 0))
        self.weekend = bool(rule(ws, "timing", "weekend"))
        self.holidays = bool(rule(ws, "timing", "holidays"))
        self.tzmode = rule(ws, "timing", "tz")
        self._first_send = None

    # ---------------------------------------------------------------
    def allowed(self, d: date) -> bool:
        return clock.is_workday(d, self.ctx.country, self.weekend, self.holidays)

    def select(self, day: date) -> tuple[list[Pick], dict]:
        """Build the list of customers for `day`. Returns picks and counts for the plan summary."""
        ctx, ws, db = self.ctx, self.ctx.ws, self.ctx.db
        counts: Counter = Counter()
        if not self.allowed(day):
            why = clock.holiday_name(day, ctx.country) if self.holidays and clock.holiday_name(day, ctx.country) else "the weekend"
            ctx.decide("timing", "holiday", f"No reminders today because of {why}",
                       f"Customers due today move to the next working day. Rule: {'send at weekends: off' if why == 'the weekend' else 'move reminders before bank holidays: on'}.",
                       100, "Holidays and weekends")
            return [], {"blocked_day": why}

        # Days after `day` that are not sendable: reminders due then go out today.
        ahead = 0
        while ahead < 7 and not self.allowed(day + timedelta(days=ahead + 1)):
            ahead += 1
        hol_ahead = [clock.holiday_name(day + timedelta(days=i + 1), ctx.country) for i in range(ahead)]
        hol_ahead = [h for h in hol_ahead if h]
        moving = ahead and self._lvl("holiday") != "off"

        contacts = list(db.scalars(select(Contact).where(Contact.workspace_id == ws.id)))
        last_send = dict(db.execute(select(Send.contact_id, func.max(func.coalesce(Send.sent_at, Send.scheduled_at)))
                                    .where(Send.workspace_id == ws.id, Send.status.notin_(("skipped",)))
                                    .group_by(Send.contact_id)).all())
        ever_ra = set(db.scalars(select(Send.contact_id).where(Send.workspace_id == ws.id, Send.seg == "ra")))
        paused_segs = set(ctx.state("paused_segs", []))
        hold_pct = float((ws.opt or {}).get("holdout", 5))
        hour_on = self._lvl("hour") != "off"
        skip_on = self._lvl("skip") != "off"
        picks: list[Pick] = []
        low_examples, skip_examples = [], []
        for c in contacts:
            lead = self.lead + (ahead if moving else 0)
            a = analyse(ws, c, day, lead)
            counts[a.seg or "unsub"] += 1
            if a.seg in ("", "alone"):
                continue
            if a.seg in paused_segs:
                counts["paused_seg"] += 1
                continue
            last = last_send.get(c.id)
            # one email per cycle: nothing since their latest purchase
            if a.seg in REMIND and last and c.last_at and last.date() >= c.last_at:
                counts["already_reminded"] += 1
                continue
            if a.seg == "re" and last and (day - last.date()).days < max(a.gap or 30, 30):
                counts["already_reminded"] += 1
                continue
            if a.seg == "ra" and c.id in ever_ra:
                counts["already_reminded"] += 1
                continue
            if a.seg in REMIND and a.prob < self.thr:
                counts["below_certainty"] += 1
                if len(low_examples) < 2:
                    low_examples.append((c, a))
                continue
            if a.seg in REMIND and skip_on and self._returns_alone(c):
                counts["skipped_likely"] += 1
                if len(skip_examples) < 3:
                    skip_examples.append(c)
                continue
            h, m = self._hour(c) if hour_on else self._default()
            due_date = day + timedelta(days=max(0, a.due)) if a.due is not None and a.seg in REMIND else None
            moved = ""
            if moving and a.seg in REMIND and a.due is not None and a.due > self.lead:
                moved = hol_ahead[0] if hol_ahead else "the weekend"
            picks.append(Pick(c, a, a.seg, h, m, in_holdout(c.id, day, hold_pct), due_date, moved))

        self._explain(picks, counts, low_examples, skip_examples, hol_ahead if moving else [], ahead)
        return picks, dict(counts)

    # ---------------------------------------------------------------
    def _lvl(self, action: str) -> str:
        from .spec import level_for
        return level_for(self.ctx.ws, "timing", action)

    def _default(self) -> tuple[int, int]:
        lo, hi = self.from_hm, self.to_hm
        h = 9 if lo[0] <= 9 < hi[0] else lo[0]
        return h, (lo[1] if h == lo[0] else 0)

    def _hour(self, c: Contact) -> tuple[int, int]:
        if c.send_hour is None:
            return self._default()
        lo, hi = self.from_hm, self.to_hm
        h = max(lo[0], min(hi[0] - 1 if hi[0] > lo[0] else hi[0], c.send_hour))
        return h, (lo[1] if h == lo[0] else 0)

    def _returns_alone(self, c: Contact) -> bool:
        """True when the last 3 purchases each happened without an email in the days before."""
        db = self.ctx.db
        buys = db.scalars(select(Event.at).where(Event.contact_id == c.id, Event.kind == "purchase")
                          .order_by(Event.at.desc()).limit(3)).all()
        if len(buys) < 3:
            return False
        # Only meaningful once LOOPX has been emailing: before that, every return happened "without a reminder".
        if self._first_send is None:
            self._first_send = db.scalar(select(func.min(Send.sent_at)).where(
                Send.workspace_id == self.ctx.ws.id, Send.status == "sent")) or False
        if not self._first_send or buys[-1] <= self._first_send:
            return False
        win = timedelta(days=settings.attribution_days)
        for at in buys:
            hit = db.scalar(select(func.count(Send.id)).where(Send.contact_id == c.id, Send.status == "sent",
                                                              Send.sent_at <= at, Send.sent_at >= at - win))
            if hit:
                return False
        return True

    def _explain(self, picks, counts, low_examples, skip_examples, hol_ahead, ahead) -> None:
        ctx = self.ctx
        segs = Counter(p.seg for p in picks)
        if picks:
            ex = next((p for p in picks if p.seg in REMIND), picks[0])
            ctx.decide("timing", "pick", f"Added {len(picks)} customers to today's plan",
                       ", ".join(f"{n} in {s}" for s, n in segs.most_common()) +
                       f". Example: {first_of(ex.contact)} usually comes back every {ex.a.gap or '?'} days and was last seen {ex.a.since} days ago.",
                       ex.a.prob, f"Certainty above {self.thr}%", contact=ex.contact,
                       payload={"op": "plan_pick", "count": len(picks)})
            learned = [p for p in picks if p.contact.send_hour is not None]
            if learned:
                p = learned[0]
                ctx.decide("timing", "hour", f"Send hours set from opening habits for {len(learned)} customers",
                           f"{first_of(p.contact)} usually opens email around {p.hour}:00, so their reminder goes out then.",
                           88, f"Inside {rule(ctx.ws, 'timing', 'from')} to {rule(ctx.ws, 'timing', 'to')}", contact=p.contact)
        for c, a in low_examples[:1]:
            ctx.decide("timing", "info", "Waiting until the pattern is clearer",
                       f"Only {c.n_orders} earlier purchases, so the gap between them is not reliable yet. Certainty {a.prob}%.",
                       a.prob, f"Below {self.thr}%", contact=c, status="info")
        if counts.get("skipped_likely"):
            c = skip_examples[0]
            ctx.decide("timing", "skip", f"Left {counts['skipped_likely']} customers out who return without reminders",
                       f"{first_of(c)} came back without a reminder the last 3 times, so an email would not change much.",
                       81, "Likely to return anyway", contact=c, payload={"op": "noop"})
        moved = [p for p in picks if p.moved_for]
        if moved:
            ctx.decide("timing", "holiday", f"Moved {len(moved)} reminders ahead of {moved[0].moved_for}",
                       f"The next {ahead} day(s) are not send days, so customers due then are reminded today.",
                       100, "Move reminders before bank holidays", payload={"op": "noop"})
        elif not hol_ahead and ctx.country:
            nxt = clock.upcoming_holidays(ctx.today, 14, ctx.country)
            if not nxt:
                ctx.decide("timing", "info", "Checked the next 14 days for bank holidays. None found.", "", 100,
                           "Holidays", status="info")


def learn_send_hours(ctx: Ctx) -> int:
    """Set each contact's send hour to the hour they most often open (needs 3+ opens)."""
    db, ws = ctx.db, ctx.ws
    rows = db.execute(select(Send.contact_id, Send.opened_at).where(Send.workspace_id == ws.id, Send.opened_at.isnot(None))).all()
    by: dict[int, list[int]] = {}
    tz = clock.zone(ctx.tz)
    for cid, at in rows:
        local = at.replace(tzinfo=__import__("datetime").timezone.utc).astimezone(tz)
        by.setdefault(cid, []).append(local.hour)
    n = 0
    for cid, hours in by.items():
        if len(hours) >= 3:
            h = Counter(hours).most_common(1)[0][0]
            c = db.get(Contact, cid)
            if c and c.send_hour != h:
                c.send_hour = h
                n += 1
    return n
