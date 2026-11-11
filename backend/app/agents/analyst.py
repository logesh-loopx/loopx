"""Analyst agent: daily numbers, holdout comparison, alerts, weekly report, weak-campaign suggestions."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select

from ..models import DailyMetric, Send
from ..services import ai, clock
from .base import Ctx
from .spec import level_for, rule, seg_def, terms, tx

KEYS = ["contacted", "opened", "clicked", "replied", "returned", "unsubs", "bounces", "holdout", "holdout_returned"]


def compute_days(ctx: Ctx, days: int = 60) -> dict[date, dict]:
    """Per local day: emails sent and what happened to them, plus the holdout group."""
    db, ws = ctx.db, ctx.ws
    tz = clock.zone(ctx.tz)
    since = ctx.now - timedelta(days=days + 1)
    out: dict[date, dict] = defaultdict(lambda: {k: 0 for k in KEYS} | {"segs": {}})
    q = select(Send).where(Send.workspace_id == ws.id, Send.status.in_(("sent", "bounced", "holdout")),
                           (Send.sent_at >= since) | (Send.scheduled_at >= since))
    for s in db.scalars(q):
        ref = s.sent_at or s.scheduled_at
        if not ref:
            continue
        d = ref.replace(tzinfo=__import__("datetime").timezone.utc).astimezone(tz).date()
        m = out[d]
        if s.status == "holdout":
            m["holdout"] += 1
            m["holdout_returned"] += 1 if s.returned_at else 0
            continue
        m["contacted"] += 1
        m["opened"] += 1 if s.opened_at else 0
        m["clicked"] += 1 if s.clicked_at else 0
        m["replied"] += 1 if s.replied_at else 0
        m["returned"] += 1 if s.returned_at else 0
        m["unsubs"] += 1 if (s.unsub_at or s.complained_at) else 0
        m["bounces"] += 1 if s.status == "bounced" else 0
        sg = m["segs"].setdefault(s.seg, {"contacted": 0, "opened": 0, "returned": 0})
        sg["contacted"] += 1
        sg["opened"] += 1 if s.opened_at else 0
        sg["returned"] += 1 if s.returned_at else 0
    return dict(out)


def lift(days: dict[date, dict], last_n: int, today: date) -> dict:
    tot = {k: 0 for k in KEYS}
    for d, m in days.items():
        if (today - d).days < last_n:
            for k in KEYS:
                tot[k] += m[k]
    w = tot["returned"] / tot["contacted"] if tot["contacted"] else 0
    h = tot["holdout_returned"] / tot["holdout"] if tot["holdout"] else None
    extra = round((w - h) * tot["contacted"]) if h is not None else None
    return {"with_rate": round(w * 100, 1), "without_rate": round(h * 100, 1) if h is not None else None,
            "extra_returns": extra, **tot}


class AnalystAgent:
    name = "analyst"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    def run(self) -> None:
        ctx, ws, db = self.ctx, self.ctx.ws, self.ctx.db
        days = compute_days(ctx)
        for d, m in days.items():  # store for Reports
            row = db.scalar(select(DailyMetric).where(DailyMetric.workspace_id == ws.id, DailyMetric.day == d))
            if row is None:
                db.add(DailyMetric(workspace_id=ws.id, day=d, data=m))
            elif row.data != m:
                row.data = m
        self._alerts(days)
        self._weekly_report(days)
        self._weak_campaigns(days)

    # ---------------------------------------------------------------
    def _alerts(self, days):
        """Once a day, compare yesterday with the average of the comparison window."""
        ctx, ws = self.ctx, self.ctx.ws
        today = ctx.today
        if ctx.state("analyst_alert_day") == today.isoformat():
            return
        ctx.set_state("analyst_alert_day", today.isoformat())
        y = today - timedelta(days=1)
        win = int(rule(ws, "analyst", "window") or 14)
        thr = float(rule(ws, "analyst", "flag"))
        ym = days.get(y)
        if not ym or ym["contacted"] < 30:
            return
        past = [m for d, m in days.items() if 1 < (today - d).days <= win + 1 and m["contacted"] >= 30]
        if len(past) < 3:
            return
        checks = []
        if rule(ws, "analyst", "wReturns"):
            checks.append(("returns", "returned", -1))
        if rule(ws, "analyst", "wUnsubs"):
            checks.append(("unsubscribes", "unsubs", +1))
        for label, key, bad_dir in checks:
            r_y = ym[key] / ym["contacted"]
            avg = sum(m[key] / m["contacted"] for m in past) / len(past)
            if avg <= 0:
                continue
            ch = (r_y - avg) / avg * 100
            if abs(ch) > thr:
                word = "rose" if ch > 0 else "fell"
                good = (ch < 0) == (bad_dir > 0)
                ctx.decide("analyst", "flag", f"{label.capitalize()} {word} {abs(round(ch))}% yesterday",
                           f"Yesterday {round(r_y * 100, 1)}% against an average of {round(avg * 100, 1)}% over the last {win} days"
                           + (". Worth a look." if not good else "."), 86, f"Alert above {thr:g}%", payload={"op": "noop"})
                ctx.notify(f"Analyst: {label} {word} {abs(round(ch))}% yesterday ({round(r_y * 100, 1)}% vs {round(avg * 100, 1)}%).",
                           "alert" if not good else "info")

    def _weekly_report(self, days):
        ctx, ws = self.ctx, self.ctx.ws
        if level_for(ws, "analyst", "report") == "off":
            return
        today = ctx.today
        want = 0 if rule(ws, "analyst", "day") == "Mon" else 4
        if today.weekday() != want or ctx.state("last_report") == today.isoformat():
            return
        ctx.set_state("last_report", today.isoformat())
        L = lift(days, 7, today)
        L30 = lift(days, 30, today)
        if not L["contacted"] and not L30["contacted"]:
            return
        t = terms(ws)
        segs = defaultdict(lambda: {"contacted": 0, "opened": 0, "returned": 0})
        for d, m in days.items():
            if (today - d).days < 7:
                for s, v in m["segs"].items():
                    for k in v:
                        segs[s][k] += v[k]
        lines = [f"Last 7 days: {L['contacted']} emails, {L['opened']} opened, {L['clicked']} clicked, "
                 f"{L['replied']} replies, {L['returned']} returns, {L['unsubs']} unsubscribes, {L['bounces']} bounces."]
        if L30["without_rate"] is not None:
            lines.append(f"Last 30 days: {L30['with_rate']}% returned after an email and {L30['without_rate']}% without one "
                         f"(holdout), so the emails caused about {L30['extra_returns']} extra returns.")
        for s, v in sorted(segs.items(), key=lambda x: -x[1]["contacted"]):
            nm = tx(seg_def(ws, s).get("name", s), t)
            lines.append(f"{nm}: {v['contacted']} sent, {round(100 * v['opened'] / max(1, v['contacted']))}% opened, "
                         f"{v['returned']} returned.")
        facts = "\n".join(lines)
        text = ai.text(f"Write a short weekly email-results note (max 120 words, plain text, no headings) for {ws.name}'s "
                       f"marketing team from these figures only. Say what went well, what to watch, and one next step.\n\n{facts}",
                       tier="default", max_tokens=400) or facts
        ctx.decide("analyst", "report", "Weekly report", text, 100, f"Report day: {rule(ws, 'analyst', 'day')}",
                   payload={"op": "noop", "facts": facts})
        ctx.notify("Weekly report: " + text[:400], "report")

    def _weak_campaigns(self, days):
        """Suggest pausing a campaign opened far less than the rest, over its last 3 send days."""
        ctx, ws = self.ctx, self.ctx.ws
        if level_for(ws, "analyst", "pausecamp") == "off":
            return
        today = ctx.today
        if ctx.state("weak_check_day") == today.isoformat():
            return
        ctx.set_state("weak_check_day", today.isoformat())
        per = defaultdict(list)
        for d in sorted(days):
            for s, v in days[d]["segs"].items():
                if v["contacted"] >= 20:
                    per[s].append(v["opened"] / v["contacted"])
        rates = {s: sum(v[-3:]) / len(v[-3:]) for s, v in per.items() if len(v) >= 3}
        if len(rates) < 2:
            return
        paused = set(ctx.state("paused_segs", []))
        for s, r in rates.items():
            rest = [x for k, x in rates.items() if k != s]
            avg = sum(rest) / len(rest)
            if s not in paused and avg > 0 and r < avg / 2:
                nm = tx(seg_def(ws, s).get("name", s), terms(ws))
                ctx.decide("analyst", "pausecamp", f"Pause “{nm}”?",
                           f"Opened by {round(r * 100)}% over its last 3 sends, against {round(avg * 100)}% for the rest of the plan.",
                           78, "Suggest only", payload={"op": "pause_seg", "seg": s})
