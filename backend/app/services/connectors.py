"""Pull customers' orders from Shopify, Stripe and WooCommerce into LOOPX events.

Credentials live in workspace.settings["connectors"][<name>]:
  shopify:     {"shop": "mystore.myshopify.com", "token": "shpat_..."}   (Admin API access token, read_orders + read_customers)
  stripe:      {"key": "rk_live_..."}                                       (restricted key, read-only charges + customers)
  woocommerce: {"url": "https://shop.example.com", "key": "ck_...", "secret": "cs_..."}
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from ..models import Workspace
from .ingest import add_events

log = logging.getLogger(__name__)
TIMEOUT = 30
MAX_PAGES = 40  # safety cap per sync run


def _since(cfg: dict, days_first_sync: int = 730) -> datetime:
    last = cfg.get("last_sync")
    if last:
        try:
            return datetime.fromisoformat(last) - timedelta(hours=1)
        except ValueError:
            pass
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_first_sync)


# ---------- Shopify ----------
def fetch_shopify(cfg: dict, since: datetime, http: httpx.Client) -> list[dict]:
    shop = re.sub(r"^https?://", "", cfg["shop"]).strip("/")
    url = f"https://{shop}/admin/api/2025-01/orders.json"
    params = {"status": "any", "limit": 250, "updated_at_min": since.isoformat() + "Z",
              "fields": "id,email,created_at,total_price,financial_status,customer,line_items"}
    out: list[dict] = []
    for _ in range(MAX_PAGES):
        r = http.get(url, params=params, headers={"X-Shopify-Access-Token": cfg["token"]})
        r.raise_for_status()
        for o in r.json().get("orders", []):
            if o.get("financial_status") in ("voided", "refunded"):
                continue
            cust = o.get("customer") or {}
            out.append({"email": o.get("email") or cust.get("email"), "at": o.get("created_at"),
                        "amount": o.get("total_price"), "external_id": f"shopify:{o['id']}",
                        "first": cust.get("first_name") or "", "last": cust.get("last_name") or "",
                        "product": ((o.get("line_items") or [{}])[0] or {}).get("title", "")})
        m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("link", ""))
        if not m:
            break
        url, params = m.group(1), None
    return out


# ---------- Stripe ----------
def fetch_stripe(cfg: dict, since: datetime, http: httpx.Client) -> list[dict]:
    out: list[dict] = []
    params = {"limit": 100, "created[gte]": int(since.replace(tzinfo=timezone.utc).timestamp()), "expand[]": "data.customer"}
    for _ in range(MAX_PAGES):
        r = http.get("https://api.stripe.com/v1/charges", params=params, auth=(cfg["key"], ""))
        r.raise_for_status()
        data = r.json()
        for ch in data.get("data", []):
            if ch.get("status") != "succeeded" or ch.get("refunded"):
                continue
            cust = ch.get("customer") if isinstance(ch.get("customer"), dict) else {}
            email = (ch.get("billing_details") or {}).get("email") or ch.get("receipt_email") or cust.get("email")
            name = (ch.get("billing_details") or {}).get("name") or cust.get("name") or ""
            first, _, last = name.partition(" ")
            out.append({"email": email, "at": datetime.fromtimestamp(ch["created"], timezone.utc).replace(tzinfo=None).isoformat(),
                        "amount": (ch.get("amount") or 0) / 100, "external_id": f"stripe:{ch['id']}",
                        "first": first, "last": last, "product": ch.get("description") or ""})
        if not data.get("has_more") or not data.get("data"):
            break
        params["starting_after"] = data["data"][-1]["id"]
    return out


# ---------- WooCommerce ----------
def fetch_woocommerce(cfg: dict, since: datetime, http: httpx.Client) -> list[dict]:
    base = cfg["url"].rstrip("/") + "/wp-json/wc/v3/orders"
    out: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        r = http.get(base, params={"per_page": 100, "page": page, "modified_after": since.isoformat(),
                                   "status": "completed,processing"},
                     auth=(cfg["key"], cfg["secret"]))
        r.raise_for_status()
        orders = r.json()
        for o in orders:
            b = o.get("billing") or {}
            out.append({"email": b.get("email"), "at": o.get("date_created_gmt") or o.get("date_created"),
                        "amount": o.get("total"), "external_id": f"woo:{o['id']}",
                        "first": b.get("first_name", ""), "last": b.get("last_name", ""), "company": b.get("company", ""),
                        "product": ((o.get("line_items") or [{}])[0] or {}).get("name", "")})
        if len(orders) < 100:
            break
    return out


FETCHERS = {"shopify": (fetch_shopify, ("shop", "token")), "stripe": (fetch_stripe, ("key",)),
            "woocommerce": (fetch_woocommerce, ("url", "key", "secret"))}


def sync(db: Session, ws: Workspace, name: str, http: httpx.Client | None = None) -> dict:
    conns = dict((ws.settings or {}).get("connectors") or {})
    cfg = dict(conns.get(name) or {})
    fetch, needed = FETCHERS[name]
    missing = [k for k in needed if not cfg.get(k)]
    if missing:
        return {"ok": False, "error": f"Missing {', '.join(missing)} for {name}."}
    own = http is None
    http = http or httpx.Client(timeout=TIMEOUT)
    started = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        events = fetch(cfg, _since(cfg), http)
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"{name} answered {e.response.status_code}: {e.response.text[:200]}"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Could not reach {name}: {e}"}
    finally:
        if own:
            http.close()
    res = add_events(db, ws, events, source=name)
    cfg["last_sync"] = started.isoformat()
    conns[name] = cfg
    ws.settings = {**(ws.settings or {}), "connectors": conns}
    db.commit()
    return {"ok": True, "fetched": len(events), **res}


def sync_all(db: Session, ws: Workspace) -> dict:
    out = {}
    for name in ((ws.settings or {}).get("connectors") or {}):
        if name in FETCHERS and ((ws.settings or {}).get("connectors") or {}).get(name, {}).get("on", True):
            out[name] = sync(db, ws, name)
    return out
