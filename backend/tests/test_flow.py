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
    w = r.json()["workspace"]
    # send window 00:00-01:00 so emails are due whatever time the tests run
    c.patch(f"/api/workspaces/{w['id']}", json={"agents": {"timing": {"rules": {"from": "00:00", "to": "01:00"}}}})
    return w


def _due_customers(c, ws, n=40, domain="acme-shop.co"):
    key = ws["api_key"]
    evs = []
    for i in range(n):  # every 14 days, last order 13 days ago -> due tomorrow
        evs += _history(f"due{i}@{domain}", 14, 6, 13, first=f"Cust{i}")
    evs += _history(f"once@{domain}", 14, 1, 5)          # one-time buyer: left alone
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
    assert c.post("/api/v1/events", json=_history("due0@acme-shop.co", 14, 2, 13), headers={"X-API-Key": ws["api_key"]}).json()["duplicates"] == 2

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

    assert reply("due0@acme-shop.co", "Please take me off this list.")["category"] == "unsubscribe"
    assert reply("due1@acme-shop.co", "I am out of office until Monday with limited access to email.")["category"] == "out_of_office"
    r = reply("due2@acme-shop.co", "We closed the business at the end of last month.")
    assert r["category"] == "closed"
    assert reply("due3@acme-shop.co", "I was charged twice, I want a refund now.")["category"] == "complaint"
    assert reply("due4@acme-shop.co", "Please use my new address priya.new@acme-shop.co from now on.")["category"] == "address_change"
    db = SessionLocal()
    by = {x.email: x for x in db.scalars(select(Contact).where(Contact.workspace_id == wid))}
    assert by["due0@acme-shop.co"].status == "unsubscribed"          # locked: always auto
    assert by["due2@acme-shop.co"].status == "active"                # 'closed' needs approval (ask) by default
    pend = {d.action: d for d in db.scalars(select(Decision).where(Decision.workspace_id == wid, Decision.status.in_(("pending", "suggested"))))}
    assert {"closed", "update"} <= set(pend)   # Listener's default level is "suggest only"
    # approve closing, then undo it
    did = pend["closed"].id
    assert c.post(f"/api/workspaces/{wid}/decisions/{did}/approve").status_code == 200
    db.expire_all()
    assert db.get(Contact, by["due2@acme-shop.co"].id).status == "closed"
    assert c.post(f"/api/workspaces/{wid}/decisions/{did}/undo").status_code == 200   # a person can reverse it
    db.expire_all()
    assert db.get(Contact, by["due2@acme-shop.co"].id).status == "active"
    # approve the address change
    assert c.post(f"/api/workspaces/{wid}/decisions/{pend['update'].id}/approve").status_code == 200
    db.expire_all()
    assert db.get(Contact, by["due4@acme-shop.co"].id).email == "priya.new@acme-shop.co"
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
                "a1@acme-shop.co,Asha,Asha Co,2026-01-01,%s,10,yes\n"
                "a2@acme-shop.co,Bala,,2026-01-01,%s,10,no\n" % ((datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)).date(),
                                                               (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)).date()))
    r = c.post(f"/api/workspaces/{wid}/import", content=csv_text, headers={"content-type": "text/csv"})
    assert r.status_code == 200 and r.json()["kind"] == "customers" and r.json()["added"] == 2
    cts = {x["e"]: x for x in c.get(f"/api/workspaces/{wid}/contacts").json()["contacts"]}
    assert cts["a1@acme-shop.co"]["n"] == 10 and cts["a1@acme-shop.co"]["co"] == "Asha Co"
    assert cts["a2@acme-shop.co"]["sub"] is False   # no marketing consent -> never emailed


def test_connectors_with_mock_http(admin):
    from app.services import connectors

    c = admin
    ws = _make_ws(c)
    wid = ws["id"]

    def handler(req: httpx.Request):
        if "myshopify" in req.url.host:
            assert req.headers["X-Shopify-Access-Token"] == "shpat_test"
            return httpx.Response(200, json={"orders": [{"id": 1, "email": "S@Acme-Shop.co", "created_at": "2026-10-01T10:00:00Z",
                                                         "total_price": "42.00", "financial_status": "paid",
                                                         "customer": {"first_name": "Sam"}, "line_items": [{"title": "Tea"}]}]})
        if "stripe" in req.url.host:
            return httpx.Response(200, json={"has_more": False, "data": [{"id": "ch_1", "status": "succeeded", "refunded": False,
                                                                          "created": 1760000000, "amount": 5000,
                                                                          "billing_details": {"email": "t@acme-shop.co", "name": "Tara K"}}]})
        if "shop.example" in req.url.host:
            return httpx.Response(200, json=[{"id": 9, "date_created_gmt": "2026-10-02T09:00:00", "total": "10.00",
                                              "billing": {"email": "w@acme-shop.co", "first_name": "Wen"}, "line_items": [{"name": "Mug"}]}])
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
    assert emails == {"s@acme-shop.co", "t@acme-shop.co", "w@acme-shop.co"}
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


def test_office_test_people_and_demo_guard(admin):
    """Real test people get emailed; example.com demo addresses are held; finished plans can be topped up."""
    c = admin
    ws = _make_ws(c)
    wid = ws["id"]
    _due_customers(c, ws, n=6, domain="example.com")  # must never be emailed
    c.patch(f"/api/workspaces/{wid}", json={"agents": {"writer": {"rules": {"review": False}}}})
    plan = c.post(f"/api/workspaces/{wid}/plan/build").json()["plan"]
    c.post(f"/api/workspaces/{wid}/plan/approve")
    c.post(f"/api/workspaces/{wid}/tick")
    before = len(mailer.read_outbox())
    live = c.get(f"/api/workspaces/{wid}/live").json()
    assert live["rs"]["sent"] == 0 and live["plan"]["status"] == "done"
    assert any("demo addresses" in d["title"] for d in live["decisions"])

    r = c.post(f"/api/workspaces/{wid}/test-people", json={"emails": "Priya Nair <priya@acme-office.co>\nrahul@acme-office.co, bad-address, x@example.com"}).json()
    assert [a["email"] for a in r["added"]] == ["priya@acme-office.co", "rahul@acme-office.co"]
    assert set(r["rejected"]) == {"bad-address", "x@example.com"}
    plan = c.post(f"/api/workspaces/{wid}/plan/build").json()["plan"]   # top-up of a finished plan
    assert plan["status"] == "draft" and plan["summary"]["emails"] == 2 and plan["summary"]["holdout"] == 0
    c.post(f"/api/workspaces/{wid}/plan/approve")
    for _ in range(3):
        c.post(f"/api/workspaces/{wid}/tick")
    out = mailer.read_outbox()[before:]
    assert sorted(m["to"] for m in out) == ["priya@acme-office.co", "rahul@acme-office.co"]
    assert next(m for m in out if m["to"] == "priya@acme-office.co")["text"].startswith("Hi Priya,")


def test_listener_leaves_other_mail_unread(admin, monkeypatch):
    """With a personal inbox, only customers' replies and bounces are read and marked as read."""
    import imaplib
    from app.agents.base import Ctx
    from app.agents.listener import ListenerAgent

    c = admin
    ws = _make_ws(c)
    c.post(f"/api/workspaces/{ws['id']}/test-people", json={"emails": "priya@acme-office.co"})
    c.patch(f"/api/workspaces/{ws['id']}", json={"settings": {"imap": {"host": "imap.test", "user": "me", "password": "pw"}}})
    msgs = {b"1": (b"friend@gmail.com", b"Lunch?", b"See you at 1."),
            b"2": (b"priya@acme-office.co", b"Re: your reminder", b"Please remove me from this list.")}
    seen = []

    class FakeIMAP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def select(self, *a): pass
        def search(self, *a): return "OK", [b"1 2"]
        def store(self, num, *a): seen.append(num)
        def fetch(self, num, what):
            frm, subj, body = msgs[num]
            if "HEADER.FIELDS" in what:
                return "OK", [(b"h", b"From: " + frm + b"\r\n\r\n")]
            raw = b"From: " + frm + b"\r\nSubject: " + subj + b"\r\nMessage-ID: <m" + num + b"@x>\r\n\r\n" + body
            return "OK", [(b"m", raw)]

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    db = SessionLocal()
    w = db.get(Workspace, ws["id"])
    assert ListenerAgent(Ctx(db, w)).poll() == 1
    db.commit()
    assert seen == [b"2"]                      # the friend's mail stays unread
    assert db.scalar(select(Contact).where(Contact.email == "priya@acme-office.co")).status == "unsubscribed"
    db.close()


def test_smtp_sender(monkeypatch):
    """The SMTP path (e.g. Gmail smtp.gmail.com:587): STARTTLS, login, headers."""
    import smtplib
    from app.config import settings
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None): sent["host"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context=None): sent["tls"] = True
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, msg): sent["msg"] = msg

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    for k, v in {"mail_provider": "smtp", "smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_user": "me@gmail.com",
                 "smtp_password": "app-pass", "smtp_ssl": False}.items():
        monkeypatch.setattr(settings, k, v)
    mid = mailer.send(mailer.Mail(to="priya@acme-office.co", subject="Hi", text="t", html="<p>t</p>",
                                  from_email="me@gmail.com", from_name="LOOPX", reply_to="me@gmail.com",
                                  headers={"List-Unsubscribe": "<http://x/u/1>"}))
    m = sent["msg"]
    assert sent["host"] == ("smtp.gmail.com", 587) and sent["tls"] and sent["login"] == ("me@gmail.com", "app-pass")
    assert m["To"] == "priya@acme-office.co" and m["Reply-To"] == "me@gmail.com" and m["List-Unsubscribe"] == "<http://x/u/1>"
    assert mid and m["Message-ID"] == mid
