"""Getting customers and purchases into LOOPX: CSV, the Events API and store connectors.

A contact's purchase summary (first/last purchase, number of orders, spend) is what
the Timing agent reads. It can come straight from a CSV (a "baseline"), from purchase
events, or both: events after the baseline are added on top of it.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Contact, Event, Send, Workspace

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RESERVED = ("example.com", "example.org", "example.net")
RESERVED_TLDS = (".example", ".test", ".invalid", ".localhost", ".local")


def undeliverable(email: str) -> bool:
    """Reserved demo/test domains (RFC 2606) that can never receive mail."""
    d = (email or "").rsplit("@", 1)[-1].lower()
    return d in RESERVED or d.endswith(tuple("." + r for r in RESERVED)) or d.endswith(RESERVED_TLDS)


def clean_email(e) -> str:
    e = str(e or "").strip().lower()
    return e if EMAIL_RE.match(e) else ""


def parse_date(v) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S",
                "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s[:len(datetime.now().strftime(fmt))] if "T" not in fmt else s[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo is None else v.astimezone(tz=None).replace(tzinfo=None)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, __import__('datetime').timezone.utc).replace(tzinfo=None)
    s = str(v or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo:
            d = d.astimezone(tz=__import__("datetime").timezone.utc).replace(tzinfo=None)
        return d
    except ValueError:
        dd = parse_date(s)
        return datetime(dd.year, dd.month, dd.day, 12) if dd else None


def _num(v, default=0.0) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("₹", "").strip() or default)
    except ValueError:
        return default


def _truthy(v, default=True) -> bool:
    if v in (None, ""):
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "n", "unsubscribed", "opted out", "off")


# ---------- contacts ----------
def upsert_contacts(db: Session, ws: Workspace, rows: Iterable[dict], source: str = "file") -> dict:
    """rows: dicts with email, first, last, company, first_at, last_at, n_orders, spend, subscribed, tags."""
    added = updated = skipped = 0
    existing = {c.email: c for c in db.scalars(select(Contact).where(Contact.workspace_id == ws.id))}
    for r in rows:
        email = clean_email(r.get("email"))
        if not email:
            skipped += 1
            continue
        c = existing.get(email)
        if c is None:
            c = Contact(workspace_id=ws.id, email=email, source=source, extra={})
            db.add(c)
            existing[email] = c
            added += 1
        else:
            updated += 1
        for k in ("first", "last", "company", "tags", "tz"):
            if r.get(k):
                setattr(c, k, str(r[k]).strip()[:255])
        base = dict((c.extra or {}).get("base") or {})
        fa, la = parse_date(r.get("first_at")), parse_date(r.get("last_at"))
        if fa:
            base["first_at"] = fa.isoformat()
        if la:
            base["last_at"] = la.isoformat()
        if r.get("n_orders") not in (None, ""):
            base["n"] = int(_num(r.get("n_orders")))
        if r.get("spend") not in (None, ""):
            base["spend"] = _num(r.get("spend"))
        c.extra = {**(c.extra or {}), "base": base}
        if "subscribed" in r and not _truthy(r.get("subscribed")):
            c.subscribed = False
            if c.status == "active":
                c.status = "unsubscribed"
        db.flush()
        recompute(db, c)
    db.commit()
    return {"added": added, "updated": updated, "skipped": skipped}


def recompute(db: Session, c: Contact) -> None:
    """Combine the CSV baseline with purchase events into the summary Timing reads."""
    base = (c.extra or {}).get("base") or {}
    b_first, b_last = parse_date(base.get("first_at")), parse_date(base.get("last_at"))
    b_n, b_spend = int(base.get("n") or 0), float(base.get("spend") or 0)
    evs = db.execute(select(Event.at, Event.amount).where(Event.contact_id == c.id, Event.kind == "purchase")
                     .order_by(Event.at)).all()
    new = [(at, amt) for at, amt in evs if not b_last or at.date() > b_last]
    dates = [d for d in [b_first, b_last] if d] + [at.date() for at, _ in evs]
    c.first_at = min(dates) if dates else None
    c.last_at = max(dates) if dates else None
    if b_n or b_first or b_last:
        n = max(b_n, 1 if (b_first or b_last) else 0, 2 if (b_first and b_last and b_last > b_first and not b_n) else 0)
        c.n_orders = n + len(new)
    else:
        c.n_orders = len(evs)
    c.spend = round(b_spend + sum(a for _, a in new), 2)


# ---------- events ----------
def add_events(db: Session, ws: Workspace, events: Iterable[dict], source: str = "api") -> dict:
    """events: dicts with email, at, kind (purchase|visit|refund), amount, product, external_id, first, last, company."""
    added = dup = bad = 0
    touched: dict[int, Contact] = {}
    contacts = {c.email: c for c in db.scalars(select(Contact).where(Contact.workspace_id == ws.id))}
    for e in events:
        email = clean_email(e.get("email"))
        at = parse_dt(e.get("at") or e.get("date") or e.get("created_at"))
        if not email or not at:
            bad += 1
            continue
        ext = str(e.get("external_id") or e.get("id") or "") or None
        if ext and db.scalar(select(Event.id).where(Event.workspace_id == ws.id, Event.external_id == ext)):
            dup += 1
            continue
        c = contacts.get(email)
        if c is None:
            c = Contact(workspace_id=ws.id, email=email, source=source, extra={},
                        first=str(e.get("first") or "")[:120], last=str(e.get("last") or "")[:120],
                        company=str(e.get("company") or "")[:255])
            db.add(c)
            db.flush()
            contacts[email] = c
        kind = str(e.get("kind") or "purchase").lower()
        db.add(Event(workspace_id=ws.id, contact_id=c.id, kind=kind if kind in ("purchase", "visit", "refund") else "purchase",
                     at=at, amount=_num(e.get("amount")), product=str(e.get("product") or "")[:255],
                     source=source, external_id=ext))
        touched[c.id] = c
        added += 1
    db.flush()
    for c in touched.values():
        recompute(db, c)
        attribute_return(db, c)
    db.commit()
    return {"added": added, "duplicates": dup, "skipped": bad}


def attribute_return(db: Session, c: Contact) -> None:
    """Mark the latest email (or holdout slot) before a purchase as 'returned'."""
    window = timedelta(days=settings.attribution_days)
    buys = db.scalars(select(Event.at).where(Event.contact_id == c.id, Event.kind == "purchase").order_by(Event.at.desc()).limit(5)).all()
    for at in buys:
        s = db.scalar(select(Send).where(Send.contact_id == c.id, Send.returned_at.is_(None),
                                         Send.status.in_(("sent", "holdout")),
                                         func.coalesce(Send.sent_at, Send.scheduled_at) <= at,
                                         func.coalesce(Send.sent_at, Send.scheduled_at) >= at - window)
                      .order_by(func.coalesce(Send.sent_at, Send.scheduled_at).desc()))
        if s:
            s.returned_at = at


# ---------- CSV ----------
HEADER_MAP = {
    "email": ["email", "e-mail", "email address", "customer email", "contact email", "billing email"],
    "first": ["first", "first name", "firstname", "given name", "first_name"],
    "last": ["last", "last name", "lastname", "surname", "family name", "last_name"],
    "company": ["company", "business", "business name", "organization", "organisation", "company name", "account"],
    "first_at": ["first order", "first order date", "first purchase", "first purchase date", "customer since", "created", "first_order_date", "signup date"],
    "last_at": ["last order", "last order date", "last purchase", "last purchase date", "last_order_date", "last activity", "last seen"],
    "n_orders": ["orders", "order count", "orders count", "number of orders", "total orders", "purchases", "orders_count"],
    "spend": ["total spent", "spend", "revenue", "lifetime value", "ltv", "total_spent", "amount spent"],
    "subscribed": ["subscribed", "accepts marketing", "email consent", "marketing consent", "opt in", "accepts_marketing"],
    "at": ["order date", "date", "purchase date", "created at", "created_at", "paid at", "order_date"],
    "amount": ["amount", "total", "order total", "price", "value"],
    "external_id": ["order id", "order", "id", "order number", "invoice", "transaction id", "order_id"],
    "product": ["product", "item", "sku", "plan"],
}


def map_headers(headers: list[str]) -> dict[str, str]:
    norm = {h: re.sub(r"[_\-]+", " ", h.strip().lower()) for h in headers}
    out: dict[str, str] = {}
    for field, names in HEADER_MAP.items():
        for h, n in norm.items():
            if n in names or n.replace(" ", "_") in names:
                out.setdefault(field, h)
                break
    return out


def import_csv(db: Session, ws: Workspace, text: str, source: str = "file") -> dict:
    """Accepts either a customer list (one row per customer) or an order list (one row per order)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = reader.fieldnames or []
    m = map_headers(headers)
    if "email" not in m:
        return {"error": "No email column found. Name a column 'email'.", "headers": headers}
    rows = list(reader)
    is_orders = "at" in m and "first_at" not in m and "last_at" not in m
    if is_orders:
        evs = [{"email": r.get(m["email"]), "at": r.get(m["at"]), "amount": r.get(m.get("amount", ""), 0),
                "external_id": r.get(m.get("external_id", "")) or None, "product": r.get(m.get("product", ""), ""),
                "first": r.get(m.get("first", ""), ""), "last": r.get(m.get("last", ""), ""),
                "company": r.get(m.get("company", ""), "")} for r in rows]
        res = add_events(db, ws, evs, source=source)
        return {"kind": "orders", **res, "mapped": m}
    cts = [{k: r.get(col) for k, col in m.items()} for r in rows]
    res = upsert_contacts(db, ws, cts, source=source)
    return {"kind": "customers", **res, "mapped": m}
