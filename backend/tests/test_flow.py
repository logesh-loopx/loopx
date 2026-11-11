"""End-to-end: sign up, import customers, build and approve a plan, send, track, reply, report."""
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Contact, Decision, Send, Workspace
from app.services import mailer


def _history(email, gap, count, last_days_ago, first="Test"):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    last = now - timedelta(days=last_days_ago)
    return [{"email": email, "first": first, "at": (last - timedelta(days=gap * k)).isoformat(),
             "amount": 100, "external_id": f"{email}-{k}"} for k in range(count)]


def _make_ws(c, **extra):
    r = c.post("/api/workspaces", json={"name": "Acme Shop", "industry": "store",
                                         "settings": {"timezone": "UTC", "website": "https://acme.example"}, **extra})
    assert r.status_code == 200, r.text
    return r.json()["workspace"]


def _due_customers(c, ws, n=40):
    key = ws["api_key"]
    evs = []
    for i in range(n):  # every 14 days, last order 13 days ago -> due tomorrow
        evs += _history(f"due{i}@example.com", 14, 6, 13, first=f"Cust{i}")
    evs += _history("once@example.com", 14, 1, 5)          # one-time buyer: left alone
    r = c.post("/api/v1/events", json={"events": evs}, headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()


def test_auth_and_roles(client):
    assert client.get("/api/workspaces").status_code == 401
    r = client.post("/api/auth/signup", json={"email": "a@b.co", "password": "short"})
    assert r.status_code == 400
    r = client.post("/api/auth/signup", json={"email": "a@b.co", "password": "longenough1", "name": "A"})
    assert r.json()["user"]["role"] == "Admin"  # first user is admin
    assert client.get("/api/auth/me").json()["user"]["email"] == "a@b.co"
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"email": "a@b.co", "password": "wrong-pass"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "a@b.co", "password": "longenough1"}).status_code == 200


def test_full_cycle(admin):
    c = admin
    ws = _make_ws(c)
    res = _due_customers(c, ws)
    assert res["added"] == 241 and res["skipped"] == 0
    # duplicates are ignored (idempotent Events API)
    assert c.post("/api/v1/events", json=_history("due0@example.com", 14, 2, 13), headers={"X-API-Key": ws["api_key"]}).json()["duplicates"] == 2

    # Timing + Writer + Guard build the plan
    plan = c.post(f"/api/workspaces/{ws['id']}/plan/build").json()["plan"]
    assert plan["status"] == "draft"
    assert plan["summary"]["emails"] + plan["summary"]["holdout"] == 40   # one-time buyer left alone
    decs = c.get(f"/api/workspaces/{ws['id']}/decisions").json()["decisions"]
    agents = {d["agent"] for d in decs}
    assert {"timing", "writer", "guard"} <= agents
    assert any(d["status"] == "pending" for d in decs)

    # nothing sent before approval
    assert c.get(f"/api/workspaces/{ws['id']}/live").json()["rs"]["sent"] == 0
    before = len(mailer.read_outbox())
    plan = c.post(f"/api/workspaces/{ws['id']}/plan/approve").json()["plan"]
    for _ in range(4):
        c.post(f"/api/workspaces/{ws['id']}/tick")
    live = c.get(f"/api/workspaces/{ws['id']}/live").json()
    sent = live["rs"]["sent"]
    assert sent == plan["summary"]["emails"] and live["plan"]["status"] == "done"
    out = mailer.read_outbox()[before:]
    assert len(out) == sent
    m = out[0]
    assert m["subject"].startswith("Cust") and "List-Unsubscribe" in m["headers"]
    assert "Unsubscribe: http://testserver/u/" in m["text"]

    # tracking: open, click, unsubscribe
    db = SessionLocal()
    s1, s2 = db.scalars(select(Send).where(Send.status == "sent").limit(2)).all()
    assert c.get(f"/t/o/{s1.token}.gif").headers["content-type"] == "image/gif"
    from app.services.tracking import click_url
    url = click_url(s1.token, "https://acme.example").replace("http://testserver", "")
    r = c.get(url, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://acme.example"
    assert c.get(url.replace("&s=", "&s=bad"), follow_redirects=False).status_code == 400
    assert "Stop emails" in c.get(f"/u/{s2.token}").text
    assert "unsubscribed" in c.post(f"/u/{s2.token}").text
    db.expire_all()
    s1 = db.get(Send, s1.id)
    assert s1.opened_at and s1.clicked_at
    assert db.get(Contact, s2.contact_id).status == "unsubscribed"

    # a purchase after the email counts as a return
    email = db.get(Contact, s1.contact_id).email
    c.post("/api/v1/events", json={"email": email, "at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), "amount": 120, "external_id": "ret-1"},
           headers={"X-API-Key": ws["api_key"]})
    db.expire_all()
    assert db.get(Send, s1.id).returned_at is not None
    rep = c.get(f"/api/workspaces/{ws['id']}/report?days=7").json()
    assert rep["lift"]["contacted"] == sent and rep["lift"]["returned"] == 1 and rep["lift"]["opened"] >= 1
    db.close()


def test_listener_rules(admin):
    c = admin
    ws = _make_ws(c)
    _due_customers(c, ws, n=6)
    wid = ws["id"]

    def reply(frm, body, subject="Re: your order"):
        return c.post(f"/api/workspaces/{wid}/replies", json={"from": frm, "subject": subject, "body": body}).json()["reply"]

    assert reply("due0@example.com", "Please take me off this list.")["category"] == "unsubscribe"
    assert reply("due1@example.com", "I am out of office until Monday with limited access to email.")["category"] == "out_of_office"
    r = reply("due2@example.com", "We closed the business at the end of last month.")
    assert r["category"] == "closed"
    assert reply("due3@example.com", "I was charged twice, I want a refund now.")["category"] == "complaint"
    assert reply("due4@example.com", "Please use my new address priya.new@example.com from now on.")["category"] == "address_change"
    db = SessionLocal()
    by = {x.email: x for x in db.scalars(select(Contact).where(Contact.workspace_id == wid))}
    assert by["due0@example.com"].status == "unsubscribed"          # locked: always auto
    assert by["due2@example.com"].status == "active"                # 'closed' needs approval (ask) by default
    pend = {d.action: d for d in db.scalars(select(Decision).where(Decision.workspace_id == wid, Decision.status.in_(("pending", "suggested"))))}
    assert {"closed", "update"} <= set(pend)   # Listener's default level is "suggest only"
    # approve closing, then undo it
    did = pend["closed"].id
    assert c.post(f"/api/workspaces/{wid}/decisions/{did}/approve").status_code == 200
    db.expire_all()
    assert db.get(Contact, by["due2@example.com"].id).status == "closed"
    assert c.post(f"/api/workspaces/{wid}/decisions/{did}/undo").status_code == 200   # a person can reverse it
    db.expire_all()
    assert db.get(Contact, by["due2@example.com"].id).status == "active"
    # approve the address change
    assert c.post(f"/api/workspaces/{wid}/decisions/{pend['update'].id}/approve").status_code == 200
    db.expire_all()
    assert db.get(Contact, by["due4@example.com"].id).email == "priya.new@example.com"
    # unsubscribes can never be undone
    unsub = db.scalar(select(Decision).where(Decision.workspace_id == wid, Decision.action == "unsub"))
    assert c.post(f"/api/workspaces/{wid}/decisions/{unsub.id}/undo").status_code == 400
    db.close()


def test_guard_pauses_bad_wave(admin):
    c = admin
    ws = _make_ws(c)
    _due_customers(c, ws, n=30)
    wid = ws["id"]
    c.post(f"/api/workspaces/{wid}/plan/build")
    db = SessionLocal()
    # simulate bounces on the wave by making the provider reject the wave addresses
    orig = mailer.send

    def flaky(mail):
        raise mailer.SendError("550 no such user", permanent=True)
    mailer.send = flaky
    import app.services.sending as snd
    snd.mailer.send = flaky
    try:
        c.post(f"/api/workspaces/{wid}/plan/approve")
    finally:
        mailer.send = orig
        snd.mailer.send = orig
    c.post(f"/api/workspaces/{wid}/tick")
    live = c.get(f"/api/workspaces/{wid}/live").json()
    assert live["plan"]["status"] == "paused"
    titles = [d["title"] for d in live["decisions"]]
    assert "Paused today's send after the test wave" in titles
    resume = next(d for d in live["decisions"] if d["action"] == "resume")
    assert resume["status"] == "pending"
    c.post(f"/api/workspaces/{wid}/decisions/{resume['id']}/approve")
    c.post(f"/api/workspaces/{wid}/tick")
    assert c.get(f"/api/workspaces/{wid}/live").json()["plan"]["status"] in ("sending", "done")
    # bounced addresses are suppressed for good
    assert db.scalar(select(Contact).where(Contact.workspace_id == wid, Contact.status == "bounced")) is not None
    db.close()


def test_permissions_auto_and_off(admin):
    c = admin
    ws = _make_ws(c)
    _due_customers(c, ws, n=10)
    wid = ws["id"]
    # Timing allowed to add customers on its own -> plan approves itself
    c.patch(f"/api/workspaces/{wid}", json={"agents": {"timing": {"perms": {"pick": "auto"}}, "writer": {"rules": {"review": False}}}})
    plan = c.post(f"/api/workspaces/{wid}/plan/build").json()["plan"]
    assert plan["status"] in ("wave", "sending")
    # pausing all agents stops everything
    c.patch(f"/api/workspaces/{wid}", json={"agents_paused": True})
    assert c.post(f"/api/workspaces/{wid}/tick").json()["paused"] is True


def test_weekly_cap_and_csv(admin):
    c = admin
    ws = _make_ws(c)
    wid = ws["id"]
    csv_text = ("Email,First Name,Company,First order date,Last order date,Orders count,Accepts marketing\n"
                "a1@example.com,Asha,Asha Co,2026-01-01,%s,10,yes\n"
                "a2@example.com,Bala,,2026-01-01,%s,10,no\n" % ((datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)).date(),
                                                               (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)).date()))
    r = c.post(f"/api/workspaces/{wid}/import", content=csv_text, headers={"content-type": "text/csv"})
    assert r.status_code == 200 and r.json()["kind"] == "customers" and r.json()["added"] == 2
    cts = {x["e"]: x for x in c.get(f"/api/workspaces/{wid}/contacts").json()["contacts"]}
    assert cts["a1@example.com"]["n"] == 10 and cts["a1@example.com"]["co"] == "Asha Co"
    assert cts["a2@example.com"]["sub"] is False   # no marketing consent -> never emailed


def test_connectors_with_mock_http(admin):
    from app.services import connectors

    c = admin
    ws = _make_ws(c)
    wid = ws["id"]

    def handler(req: httpx.Request):
        if "myshopify" in req.url.host:
            assert req.headers["X-Shopify-Access-Token"] == "shpat_test"
            return httpx.Response(200, json={"orders": [{"id": 1, "email": "S@Example.com", "created_at": "2026-10-01T10:00:00Z",
                                                         "total_price": "42.00", "financial_status": "paid",
                                                         "customer": {"first_name": "Sam"}, "line_items": [{"title": "Tea"}]}]})
        if "stripe" in req.url.host:
            return httpx.Response(200, json={"has_more": False, "data": [{"id": "ch_1", "status": "succeeded", "refunded": False,
                                                                          "created": 1760000000, "amount": 5000,
                                                                          "billing_details": {"email": "t@example.com", "name": "Tara K"}}]})
        if "shop.example" in req.url.host:
            return httpx.Response(200, json=[{"id": 9, "date_created_gmt": "2026-10-02T09:00:00", "total": "10.00",
                                              "billing": {"email": "w@example.com", "first_name": "Wen"}, "line_items": [{"name": "Mug"}]}])
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    c.put(f"/api/workspaces/{wid}/connectors/shopify", json={"shop": "demo.myshopify.com", "token": "shpat_test"})
    c.put(f"/api/workspaces/{wid}/connectors/stripe", json={"key": "rk_test"})
    c.put(f"/api/workspaces/{wid}/connectors/woocommerce", json={"url": "https://shop.example", "key": "ck", "secret": "cs"})
    db = SessionLocal()
    w = db.get(Workspace, wid)
    for name in ("shopify", "stripe", "woocommerce"):
        res = connectors.sync(db, w, name, http=http)
        assert res["ok"] and res["added"] == 1, (name, res)
    emails = set(db.scalars(select(Contact.email).where(Contact.workspace_id == wid)))
    assert emails == {"s@example.com", "t@example.com", "w@example.com"}
    # secrets are masked when read back
    got = c.get(f"/api/workspaces/{wid}").json()["workspace"]["settings"]["connectors"]["shopify"]["token"]
    assert got.startswith("••••") and "shpat_test" not in got
    db.close()


def test_cron_and_brevo_webhook(admin):
    c = admin
    assert c.get("/cron/tick").status_code == 401
    assert c.get("/cron/tick?secret=cron-test").json()["ok"] is True
    ws = _make_ws(c)
    _due_customers(c, ws, n=5)
    wid = ws["id"]
    c.patch(f"/api/workspaces/{wid}", json={"agents": {"writer": {"rules": {"review": False}}}})
    c.post(f"/api/workspaces/{wid}/plan/build")
    c.post(f"/api/workspaces/{wid}/plan/approve")
    c.post(f"/api/workspaces/{wid}/tick")
    db = SessionLocal()
    s = db.scalar(select(Send).where(Send.workspace_id == wid, Send.status == "sent"))
    r = c.post("/webhooks/brevo?secret=cron-test", json={"event": "spam", "message-id": s.message_id})
    assert r.json()["handled"] == 1
    db.expire_all()
    assert db.get(Contact, s.contact_id).status == "complained"
    db.close()


def test_ai_bridge_without_key(admin):
    r = admin.post("/api/ai/sample", json={"input": "hi"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "not_granted"
