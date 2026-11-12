"""Local demo data: one workspace with 400 customers and 2 years of purchases.

Only runs when SEED_DEMO=true and the database has no workspaces yet. All addresses
are @example.com, so nothing real can be emailed.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .db import session_scope
from .models import Workspace
from .services import ingest

FIRST = ["Aarav", "Priya", "Rahul", "Ananya", "Vikram", "Meera", "Arjun", "Divya", "Karthik", "Sneha", "Rohan", "Kavya",
         "Aditya", "Isha", "Sanjay", "Neha", "Logesh", "Deepa", "Varun", "Nisha", "James", "Emma", "Liam", "Olivia"]
LAST = ["Sharma", "Iyer", "Nair", "Reddy", "Patel", "Menon", "Kumar", "Rao", "Singh", "Das", "Smith", "Brown"]
BIZ = ["Bright Retail", "Metro Works", "Quick Cart", "Acme Logistics", "Health Plus", "North Star Traders", "Blue Leaf Cafe",
       "Urban Threads", "Sunrise Pharmacy", "Pixel Studio", "Green Basket", "Swift Movers"]


def demo_playbook() -> dict:
    return {"industry": "store", "url": "https://demo-store.example.com", "lead": 3,
            "cycles": [["wk", 10], ["bi", 21], ["mo", 1e9]],
            "terms": {"p1": "Single order", "p2": "Subscription plan", "p3": "Bundle pack"},
            "footer": "You get this because you bought from Demo Store."}


def seed_demo(n: int = 400, seed: int = 7) -> int | None:
    with session_scope() as db:
        if db.scalar(select(func.count(Workspace.id))):
            return None
        ws = Workspace(name="Demo Store (local)", industry="store", theme="violet", playbook=demo_playbook(),
                       settings={"timezone": "Asia/Kolkata", "country": "IN", "website": "https://demo-store.example.com",
                                 "from_name": "Demo Store"})
        db.add(ws)
        db.flush()
        rnd = random.Random(seed)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        events = []
        for i in range(n):
            f, l = rnd.choice(FIRST), rnd.choice(LAST)
            email = f"{f.lower()}.{l.lower()}{i}@example.com"
            biz = rnd.choice(BIZ) if rnd.random() < 0.6 else ""
            kind = rnd.random()
            if kind < 0.25:      # one-time buyers
                buys = [now - timedelta(days=rnd.randint(5, 400))]
            else:
                gap = rnd.choice([7, 14, 14, 30, 30])
                count = rnd.randint(3, 12)
                # where in the cycle are they today? spread so some are due now
                offset = rnd.choice([gap - rnd.randint(0, 3), rnd.randint(0, gap), gap * 2 + rnd.randint(0, 5), gap * 4])
                last = now - timedelta(days=max(1, offset))
                buys = [last - timedelta(days=gap * k + rnd.randint(-1, 1)) for k in range(count)]
            for j, at in enumerate(sorted(buys)):
                events.append({"email": email, "at": at.replace(hour=rnd.randint(8, 20)).isoformat(),
                               "amount": rnd.choice([499, 799, 1299, 1999]), "external_id": f"demo-{i}-{j}",
                               "first": f, "last": l, "company": biz, "product": rnd.choice(["Single order", "Bundle pack"])})
        ingest.add_events(db, ws, events, source="demo")
        return ws.id
