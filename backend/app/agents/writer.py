"""Writer agent: fills each email, runs subject line tests, picks winners, suggests new drafts."""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict

from sqlalchemy import select

from ..models import Send
from ..services import ai
from .base import Ctx
from .spec import level_for, merge, rule, seg_def, terms, tx

SIGN = "\n\nThanks,\nThe [[company]] team"


def template_hash(ws, seg: str) -> str:
    d = seg_def(ws, seg)
    raw = "|".join(str(d.get(k, "")) for k in ("subject", "alt", "body", "cta"))
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def bucket(contact_id: int, seg: str, n: int) -> int:
    return int(hashlib.md5(f"{contact_id}:{seg}".encode()).hexdigest()[:6], 16) % n


class WriterAgent:
    name = "writer"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        ws = ctx.ws
        self.t = terms(ws)
        self.tests = int(rule(ws, "writer", "tests"))
        self.split = int(rule(ws, "writer", "split"))
        self.max_subj = int(rule(ws, "writer", "maxSubj"))
        self.avoid = [w.strip().lower() for w in str(rule(ws, "writer", "avoid") or "").split(",") if w.strip()]
        self.review = bool(rule(ws, "writer", "review"))

    # ---------------------------------------------------------------
    def variants(self, seg: str) -> list[str]:
        d = seg_def(self.ctx.ws, seg)
        vs = [d["subject"], d.get("alt") or d["subject"]]
        extra = (self.ctx.state("extra_subjects", {}) or {}).get(seg, [])
        return (vs + extra)[: max(2, self.tests)]

    def compose(self, picks) -> tuple[list[dict], dict]:
        """Turn Timing's picks into email rows. Returns (rows, stats)."""
        ctx, ws = self.ctx, self.ctx.ws
        subj_lvl = level_for(ws, "writer", "subject")
        testing = self.tests > 1 and subj_lvl in ("auto", "ask")
        winners = ctx.state("winners", {}) or {}
        approved = ctx.state("approved_tpl", {}) or {}
        tests_state = dict(ctx.state("tests", {}) or {})
        stats = Counter()
        rows = []
        held_review_segs, avoid_segs = set(), set()
        for p in picks:
            d = seg_def(ws, p.seg)
            vs = self.variants(p.seg)
            best = winners.get(p.seg, "A")
            letter = best
            if testing and p.seg not in winners:
                b = bucket(p.contact.id, p.seg, 100)
                if b < self.split:
                    letter = "ABCD"[b % len(vs)]
                    stats["in_test"] += 1
                    tests_state.setdefault(p.seg, {"since": ctx.now.isoformat(), "n": len(vs)})
            subj_tpl = vs["ABCD".index(letter)] if "ABCD".index(letter) < len(vs) else vs[0]
            subject = merge(subj_tpl, p.contact, p.due_date, self.t)
            if len(subject) > self.max_subj:
                subject = subject[: self.max_subj].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
                stats["shortened"] += 1
            body = merge("Hi {first},\n\n" + d["body"] + SIGN, p.contact, p.due_date, self.t)
            if not (p.contact.first or "").strip():
                stats["no_first_name"] += 1
            leftovers = re.findall(r"\{\w+\}|\[\[\w+\]\]", subject + body)
            status, hold = "planned", ""
            if leftovers:
                status, hold = "held", "Empty merge field: " + ", ".join(sorted(set(leftovers)))
                stats["merge_error"] += 1
            text = (subject + " " + body).lower()
            hit = next((w for w in self.avoid if re.search(r"\b" + re.escape(w) + r"\b", text)), None)
            if hit:
                status, hold = "held", f"Contains a word on the avoid list: {hit}"
                avoid_segs.add(p.seg)
            if self.review and approved.get(p.seg) != template_hash(ws, p.seg):
                held_review_segs.add(p.seg)
                if status == "planned":
                    status, hold = "held", "review"
            rows.append({"pick": p, "subject": subject, "variant": letter, "body": body, "status": status,
                         "hold": hold, "campaign": tx(d.get("name", p.seg), self.t), "cta": tx(d.get("cta", ""), self.t)})
        ctx.set_state("tests", tests_state)

        if rows:
            ctx.decide("writer", "fill", f"Filled names and dates in {len(rows)} emails",
                       "Each email was checked for an empty merge field before it was queued."
                       + (f" {stats['no_first_name']} customers have no first name, so they are greeted with 'there'." if stats["no_first_name"] else ""),
                       100, "Every merge field filled", payload={"op": "noop"})
        if stats["shortened"]:
            ctx.decide("writer", "fill", f"Shortened {stats['shortened']} subject lines",
                       f"They were longer than {self.max_subj} characters, so the last words were trimmed.", 100,
                       f"Longest subject: {self.max_subj}", payload={"op": "noop"})
        if testing and stats["in_test"]:
            seg = next(iter(tests_state))
            vs = self.variants(seg)
            ctx.decide("writer", "subject", f"Subject line test on {len(tests_state)} campaign(s)",
                       " / ".join(f"{'ABCD'[i]}: “{tx(v, self.t)}”" for i, v in enumerate(vs)) +
                       f". {self.split}% of each campaign sees a test version.", 90,
                       f"{self.tests} versions, {self.split}% of the segment", payload={"op": "noop"})
        elif self.tests > 1 and subj_lvl == "suggest" and rows:
            ctx.decide("writer", "subject", "Suggest a subject line test",
                       f"Testing {self.tests} subject lines would show which one gets more opens.", 70,
                       "Suggest only", payload={"op": "noop"})
        for seg in held_review_segs:
            ctx.decide("writer", "draft", f"Held the “{tx(seg_def(ws, seg).get('name', seg), self.t)}” email for review",
                       "This email is new or was edited. Your rule holds changed emails until a person approves them. "
                       "Approving today's plan approves it.", 100, "Review new or changed emails: on",
                       payload={"op": "approve_template", "seg": seg, "hash": template_hash(ws, seg)}, level="ask")
        for seg in avoid_segs:
            ctx.decide("writer", "draft", f"Held emails in “{seg}” that use a word on your avoid list",
                       "Edit the playbook email, then the next plan uses the new wording.", 100,
                       "Words never to use", payload={"op": "noop"}, level="ask")
        return rows, dict(stats)

    # ---------------------------------------------------------------
    def check_winners(self) -> None:
        """Pick a winner once a test has enough opens."""
        ctx, ws, db = self.ctx, self.ctx.ws, self.ctx.db
        tests = dict(ctx.state("tests", {}) or {})
        need = int(rule(ws, "writer", "winner"))
        winners = dict(ctx.state("winners", {}) or {})
        changed = False
        for seg, info in list(tests.items()):
            rows = db.execute(select(Send.variant, Send.opened_at).where(
                Send.workspace_id == ws.id, Send.seg == seg, Send.status == "sent")).all()
            sent, opens = Counter(), Counter()
            for v, op in rows:
                sent[v] += 1
                if op:
                    opens[v] += 1
            if sum(opens.values()) < need or len(sent) < 2:
                continue
            rates = {v: opens[v] / sent[v] for v in sent if sent[v]}
            best = max(rates, key=rates.get)
            others = ", ".join(f"{v} {round(r * 100)}%" for v, r in sorted(rates.items()) if v != best)
            d = ctx.decide("writer", "winner", f"Subject {best} won on “{seg}”",
                           f"{best} was opened by {round(rates[best] * 100)}% against {others} after {sum(opens.values())} opens.",
                           95, f"Winner after {need} opens", payload={"op": "set_winner", "seg": seg, "variant": best})
            if d and d.status == "done":
                winners[seg] = best
            tests.pop(seg, None)
            changed = True
        if changed:
            ctx.set_state("tests", tests)
            ctx.set_state("winners", winners)

    def suggest_draft(self) -> None:
        """Weekly: ask Claude for a better subject on the weakest campaign (suggestion only)."""
        ctx, ws, db = self.ctx, self.ctx.ws, self.ctx.db
        if level_for(ws, "writer", "draft") == "off" or not ai.available():
            return
        last = ctx.state("last_draft_suggest")
        if last and (ctx.now - __import__("datetime").datetime.fromisoformat(last)).days < 7:
            return
        rows = db.execute(select(Send.seg, Send.opened_at).where(Send.workspace_id == ws.id, Send.status == "sent")).all()
        sent, opens = defaultdict(int), defaultdict(int)
        for seg, op in rows:
            sent[seg] += 1
            opens[seg] += 1 if op else 0
        cand = [(opens[s] / sent[s], s) for s in sent if sent[s] >= 50]
        if not cand:
            return
        rate, seg = min(cand)
        d = seg_def(ws, seg)
        out = ai.structured(
            f"Company: {ws.name}. Campaign: {tx(d.get('name', seg), self.t)}. Current subject: {tx(d['subject'], self.t)} "
            f"(opened by {round(rate * 100)}%). Body:\n{tx(d['body'], self.t)}\n\nWrite one better subject line. Start with "
            "'{first}, '. Only merge fields {first}, {biz}, {date}. Plain, no hype, no emoji, under 60 characters. "
            "Never invent discounts or facts.",
            {"type": "object", "properties": {"subject": {"type": "string"}, "why": {"type": "string"}},
             "required": ["subject", "why"]}, tier="default")
        ctx.set_state("last_draft_suggest", ctx.now.isoformat())
        if out and out.get("subject", "").startswith("{first}"):
            ctx.decide("writer", "draft", f"New subject line for “{tx(d.get('name', seg), self.t)}”",
                       f"“{out['subject']}”. {out.get('why', '')} The current one is opened by {round(rate * 100)}%.",
                       70, "New drafts are held for review",
                       payload={"op": "add_subject", "seg": seg, "subject": out["subject"]})
