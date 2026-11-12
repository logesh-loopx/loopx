"""The agent rules and permissions, mirrored from the LOOPX console (agentSpec / agentPerm).

Permission levels per action:
  off      the agent never does it
  suggest  the agent records a suggestion; a person acts
  ask      the agent prepares it; it happens after a person approves
           (for plan actions this is the daily plan approval)
  auto     the agent does it on its own
Locked actions (always on, for safety) ignore the workspace settings.
"""
from __future__ import annotations

import re
from datetime import date

# action key -> (label, default level, locked)
ACTIONS: dict[str, dict[str, tuple[str, str, bool]]] = {
    "timing": {
        "pick": ("Add a customer to today's plan", "ask", False),
        "hour": ("Choose each customer's send hour", "auto", False),
        "holiday": ("Move a reminder around a holiday", "auto", False),
        "skip": ("Leave out a customer likely to return anyway", "auto", False),
    },
    "writer": {
        "fill": ("Fill in names and dates", "auto", False),
        "subject": ("Start a subject line test", "ask", False),
        "winner": ("Switch everyone to the winning subject", "auto", False),
        "draft": ("Write a new email", "suggest", False),
    },
    "guard": {
        "remove": ("Remove bounced and opted-out addresses", "auto", True),
        "pause": ("Pause a send that passes a limit", "auto", True),
        "resume": ("Resume a paused send", "ask", False),
        "hold": ("Hold a customer at the weekly limit", "auto", False),
    },
    "analyst": {
        "flag": ("Flag an unusual change", "auto", False),
        "report": ("Send the weekly report", "auto", False),
        "pausecamp": ("Pause a campaign that underperforms", "suggest", False),
        "holdoutch": ("Change the holdout size", "off", False),
    },
    "listener": {
        "unsub": ("Unsubscribe on request", "auto", True),
        "closed": ("Mark a business as closed", "ask", False),
        "update": ("Update contact details from a reply", "ask", False),
        "forward": ("Pass a complaint to a person", "auto", False),
        "answer": ("Draft an answer to a customer's question", "suggest", False),
    },
}

# rule key -> default (rules bound to cfg.s / ws.cap / opt.holdout are resolved separately)
RULE_DEFAULTS: dict[str, dict] = {
    "timing": {"from": "07:00", "to": "20:00", "weekend": True, "holidays": True, "tz": "customer"},
    "writer": {"split": 20, "winner": 1000, "tone": "plain", "maxSubj": 60, "avoid": "", "review": True},
    "guard": {"bounce": 2, "wave": 2000, "unopened": 4},
    "analyst": {"window": "14", "wReturns": True, "wUnsubs": True, "day": "Mon"},
    "listener": {"closed": True, "ooo": True, "forward": ""},
}

AGENT_NAMES = {"timing": "Timing agent", "writer": "Writer agent", "guard": "Guard agent",
               "analyst": "Analyst agent", "listener": "Listener agent"}

PRESETS = {
    "store": dict(label="Online store", noun="order", anoun="an order", make="place", makes="places", made="placed", p1="Single order", p2="Subscription plan", p3="Bundle pack"),
    "saas": dict(label="Software subscription", noun="renewal", anoun="a renewal", make="complete", makes="completes", made="completed", p1="Monthly plan", p2="Annual plan", p3="Extra seats"),
    "service": dict(label="Services and bookings", noun="booking", anoun="a booking", make="make", makes="makes", made="made", p1="Single visit", p2="Visit package", p3="Membership"),
    "tax": dict(label="Tax filing", noun="return", anoun="a return", make="file", makes="files", made="filed", p1="Single return", p2="Volume filing", p3="Extension"),
    "paystub": dict(label="Payroll and paystubs", noun="paystub", anoun="a paystub", make="create", makes="creates", made="created", p1="Pay Stub", p2="Credit Bundle", p3="Multi Pay Stub"),
    "travel": dict(label="Travel and ticketing", noun="trip", anoun="a trip", make="book", makes="books", made="booked", p1="One-way ticket", p2="Round trip", p3="Sleeper seat"),
    "itsvc": dict(label="IT and software services", noun="project", anoun="a project", make="start", makes="starts", made="started", p1="Project", p2="Support plan", p3="Dedicated team"),
    "edu": dict(label="Education and courses", noun="enrolment", anoun="an enrolment", make="complete", makes="completes", made="completed", p1="Single course", p2="Course bundle", p3="Membership"),
    "nonprofit": dict(label="Nonprofit and fundraising", noun="donation", anoun="a donation", make="make", makes="makes", made="made", p1="One-time gift", p2="Monthly giving", p3="Project sponsorship"),
    "other": dict(label="Other", noun="purchase", anoun="a purchase", make="make", makes="makes", made="made", p1="Core product", p2="Bundle", p3="Add-on"),
}

# Fallback segment emails when a workspace has no playbook yet (same wording as the console).
DEFAULT_SEGS = {
    "wk": dict(name="Repeat reminder, weekly", subject="{first}, this week's [[noun]] is due", alt="{first}, time for this week's [[noun]]",
               cta="[[Make]] this week's [[noun]]", body="You come back every week, and the next [[noun]] looks due around {date}.\n\nLast week's details are saved. Confirm them and it is done."),
    "bi": dict(name="Repeat reminder, every 14 days", subject="{first}, your next [[noun]] is due soon", alt="{first}, ready for your next [[noun]]?",
               cta="[[Make]] the next [[noun]]", body="You usually [[make]] [[anoun]] about every two weeks, and the next one looks due around {date}.\n\nYour details are saved, so the next one takes only a moment."),
    "mo": dict(name="Repeat reminder, monthly", subject="{first}, your monthly [[noun]] is coming up", alt="{first}, month-end is close",
               cta="[[Make]] this month's [[noun]]", body="You [[make]] [[anoun]] about once a month, and the next one looks due around {date}.\n\nEverything from last month is saved, so it takes only a moment."),
    "re": dict(name="Re-engagement, missed their usual date", subject="{first}, your details are still saved", alt="{first}, pick up where you left off",
               cta="[[Make]] [[anoun]]", body="It has been a little while since your last [[noun]].\n\nYour details are still here, so the next one takes only a moment."),
    "ra": dict(name="Reactivation, inactive 60 days or more", subject="{first}, do you still need us?", alt="{first}, still with us?",
               cta="[[Make]] [[anoun]]", body="If you still need us, your details are saved and ready.\n\nIf not, there is nothing to do, and we will not keep emailing you."),
}
SEG_ORDER = ["wk", "bi", "mo", "re", "mp", "cb", "ra"]


def level_for(ws, agent: str, action: str) -> str:
    """Python port of the console's agentPerm()."""
    label, default, locked = ACTIONS[agent][action]
    if locked:
        return default
    cfg = (ws.agents or {}).get(agent, {})
    if not cfg.get("on", True) or ws.agents_paused:
        return "off"
    p = (cfg.get("perms") or {}).get(action)
    if p:
        return p
    lv = {"suggest": "suggest", "approve": "ask", "auto": "auto"}.get(cfg.get("level", "approve"), "ask")
    if default == "off":
        return "off"
    if lv == "auto":
        return default
    if lv == "suggest":
        return "suggest"
    return "ask" if default == "auto" else default


def rule(ws, agent: str, key: str):
    """Current value of an agent rule, falling back to the console defaults."""
    cfg = (ws.agents or {}).get(agent, {})
    bound_s = {"timing": "certainty", "writer": "tests", "guard": "unsub", "analyst": "flag", "listener": "certainty"}
    if bound_s.get(agent) == key:
        return cfg.get("s", {"timing": 70, "writer": 2, "guard": 0.5, "analyst": 15, "listener": 80}[agent])
    if agent == "guard" and key == "cap":
        return ws.cap or 2
    if agent == "analyst" and key == "holdout":
        return (ws.opt or {}).get("holdout", 5)
    if agent == "timing" and key == "lead":
        r = (cfg.get("rules") or {}).get("lead")
        return int(r) if r is not None else int((ws.playbook or {}).get("lead", 3))
    r = (cfg.get("rules") or {}).get(key)
    return r if r is not None else RULE_DEFAULTS.get(agent, {}).get(key)


def is_on(ws, agent: str) -> bool:
    return not ws.agents_paused and bool((ws.agents or {}).get(agent, {}).get("on", True))


# ---------- wording ----------
def terms(ws) -> dict:
    p = dict(PRESETS.get(ws.industry, PRESETS["store"]))
    p.update({k: v for k, v in ((ws.playbook or {}).get("terms") or {}).items() if isinstance(v, str) and v})
    p.setdefault("nouns", p["noun"] + "s")
    if "nouns" not in ((ws.playbook or {}).get("terms") or {}):
        p["nouns"] = p["noun"] + "s"
    p["company"] = ws.name
    for k in ["noun", "nouns", "anoun", "make", "makes", "made"]:
        p[k[0].upper() + k[1:]] = p[k][:1].upper() + p[k][1:]
    return p


def tx(text: str, t: dict) -> str:
    return re.sub(r"\[\[(\w+)\]\]", lambda m: str(t.get(m.group(1), m.group(0))), text or "")


def seg_def(ws, seg: str) -> dict:
    base = dict(DEFAULT_SEGS.get(seg, DEFAULT_SEGS["bi"]))
    pb = ((ws.playbook or {}).get("segs") or {}).get(seg) or {}
    base.update({k: v for k, v in pb.items() if isinstance(v, (str, int, float)) and v != ""})
    return base


def fmt_day(d: date) -> str:
    return d.strftime("%A, %B ") + str(d.day)


def merge(text: str, contact, due: date | None, t: dict) -> str:
    first = (contact.first or "").strip() or "there"
    biz = (contact.company or "").strip() or (f"{contact.first} {contact.last}".strip() or contact.email)
    out = (text or "").replace("{first}", first).replace("{biz}", biz)
    out = out.replace("{date}", fmt_day(due) if due else "this week")
    out = out.replace("{route}", contact.extra.get("route", "your usual route") if contact.extra else "your usual route")
    return tx(out, t)
