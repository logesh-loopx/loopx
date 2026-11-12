"""The free AI providers, with a fake OpenAI-compatible API (no network)."""
import json
import re

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Decision, Reply, Send
from app.services import ai

from .test_flow import _due_customers, _make_ws


class FakeResp:
    def __init__(self, status, data=None, text="", headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}
        self.text = text or json.dumps(data or {})

    def json(self):
        return self._data


def _answer(body: dict) -> str:
    sys = next((m["content"] for m in body["messages"] if m["role"] == "system"), "")
    user = body["messages"][-1]["content"]
    ids = [int(x) for x in re.findall(r'"id": (\d+)', user)]
    if "Draft a short" in user:
        return json.dumps({"answer": "Thanks for asking! Orders usually arrive in a few days. The Demo team", "needs_person": False})
    if "sort replies" in sys:
        return json.dumps({"category": "question", "confidence": 93, "summary": "Asks about delivery times.", "new_email": None})
    if "Writer agent" in sys:
        return json.dumps({"emails": [{"id": i, "subject": "Asha, your next order is due", "opening": "Thanks for ordering with us again."}
                                      if n % 2 == 0 else
                                      {"id": i, "subject": "Asha, 50% off today only", "opening": "Get 50% off if you order in 2 hours!"}
                                      for n, i in enumerate(ids)]})
    if "Guard agent" in sys and "personalised" in user:
        return json.dumps({"flags": [{"id": i, "risk": "low", "reason": "fine"} for i in ids]})
    if "Guard agent" in sys:
        return json.dumps({"risk": "low", "issues": [], "summary": "A plain, honest reminder."})
    if "Timing agent" in sys and ids:
        return json.dumps({"decisions": [{"id": i, "include": False, "reason": "not due yet", "confidence": 70} for i in ids]})
    return "OK"


@pytest.fixture()
def fake_ai(monkeypatch):
    calls = []
    state = {"groq_429": False}

    def post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "body": json, "headers": headers})
        if "groq" in url and state["groq_429"]:
            return FakeResp(429, {"error": "rate"}, headers={"retry-after": "30"})
        return FakeResp(200, {"choices": [{"message": {"content": _answer(json)}}], "usage": {"total_tokens": 120}})

    monkeypatch.setattr(ai.httpx, "post", post)
    for d in (ai._cool, ai._windows, ai._usage_mem, ai._last_error):
        d.clear()
    ai._cfg_cache.update(at=0.0, cfg=None)
    yield calls, state
    ai._cfg_cache.update(at=0.0, cfg=None)


def _keys(c, **provs):
    r = c.put("/api/ai/settings", json={"providers": {k: {"key": v} for k, v in provs.items()}})
    assert r.status_code == 200, r.text
    return r.json()


def test_no_key_means_rules(admin):
    ai._cfg_cache.update(at=0.0, cfg=None)
    assert admin.get("/api/health").json()["ai"] is False
    st = admin.get("/api/ai/status").json()
    assert st["available"] is False and [p["name"] for p in st["providers"]][:2] == ["groq", "gemini"]


def test_groq_key_turns_ai_on_and_is_masked(admin, fake_ai):
    calls, _ = fake_ai
    st = _keys(admin, groq="gsk_test_1234")
    assert st["available"] and st["chain"][0] == "groq"
    g = next(p for p in st["providers"] if p["name"] == "groq")
    assert g["keyHint"] == "••••1234" and "gsk_test" not in json.dumps(st)
    r = admin.post("/api/ai/test/groq").json()
    assert r["ok"] and r["answer"] == "OK"
    assert calls[-1]["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert calls[-1]["headers"]["Authorization"] == "Bearer gsk_test_1234"
    # the console's Ask LOOPX goes through the same provider
    out = admin.post("/api/ai/sample", json={"input": "hello"}).json()
    assert out["text"] == "OK" and out["provider"] == "groq"


def test_only_admin_changes_ai_settings(client, fake_ai):
    client.post("/api/auth/signup", json={"email": "boss@x.co", "password": "longenough1"})
    client.post("/api/auth/logout")
    client.post("/api/auth/signup", json={"email": "viewer@x.co", "password": "longenough1"})
    assert client.put("/api/ai/settings", json={"providers": {"groq": {"key": "k"}}}).status_code == 403


def test_falls_back_to_gemini_when_groq_is_rate_limited(admin, fake_ai):
    calls, state = fake_ai
    _keys(admin, groq="gsk_a", gemini="gem_b")
    state["groq_429"] = True
    out = ai.structured("x", {"type": "object", "required": ["risk", "issues", "summary"],
                              "properties": {"risk": {"type": "string", "enum": ["low", "medium", "high"]},
                                             "issues": {"type": "array"}, "summary": {"type": "string"}}},
                        system="You are the Guard agent")
    assert out["_provider"] == "gemini"
    assert any("generativelanguage" in c["url"] for c in calls)
    # groq is cooling down now and skipped without a call
    n = sum(1 for c in calls if "groq" in c["url"])
    ai.text("hi")
    assert sum(1 for c in calls if "groq" in c["url"]) == n


def test_daily_budget_is_respected(admin, fake_ai):
    calls, _ = fake_ai
    admin.put("/api/ai/settings", json={"providers": {"groq": {"key": "gsk_a", "rpd": 2}}})
    assert ai.text("1") and ai.text("2")
    assert ai.text("3") is None          # budget used, nothing else configured -> agents use rules
    assert len([c for c in calls if "groq" in c["url"]]) == 2


def test_agents_use_ai(admin, fake_ai):
    c = admin
    _keys(c, groq="gsk_a")
    ws = _make_ws(c)
    _due_customers(c, ws, n=12)
    wid = ws["id"]
    # Listener: the AI understands a reply the keyword rules would miss, and drafts an answer
    r = c.post(f"/api/workspaces/{wid}/replies", json={"from": "due0@acme-shop.co", "subject": "Re: reminder",
                                                       "body": "how long does shipping take to Chennai"}).json()["reply"]
    assert r["category"] == "question" and r["conf"] == 93
    db = SessionLocal()
    ans = db.scalar(select(Decision).where(Decision.workspace_id == wid, Decision.action == "answer"))
    assert ans is not None and ans.status == "suggested" and "Orders usually arrive" in ans.reason
    # Plan: Guard reviews the wording, Writer personalises, guardrails reject invented offers
    c.post(f"/api/workspaces/{wid}/plan/build")
    db.expire_all()
    sends = db.scalars(select(Send).where(Send.workspace_id == wid, Send.status != "holdout")).all()
    done = [s for s in sends if s.ai_note.startswith("Personalised")]
    kept = [s for s in sends if s.ai_note.startswith("Kept the standard email")]
    assert done and kept
    assert all("50%" not in s.subject and "50%" not in s.body_text for s in sends)
    assert "Thanks for ordering with us again." in done[0].body_text
    assert any("number" in s.ai_note or "promotional" in s.ai_note for s in kept)
    titles = [d.title for d in db.scalars(select(Decision).where(Decision.workspace_id == wid))]
    assert any(t.startswith("AI review of") for t in titles)          # Guard
    assert any(t.startswith("Personalised") for t in titles)          # Writer
    assert any(t == "Today's plan, in short" for t in titles)         # Timing briefing
    # approving the drafted answer sends it
    from app.services import mailer
    before = len(mailer.read_outbox())
    assert c.post(f"/api/workspaces/{wid}/decisions/{ans.id}/approve").status_code == 200
    out = mailer.read_outbox()[before:]
    assert out and out[-1]["to"] == "due0@acme-shop.co" and out[-1]["subject"].startswith("Re:")
    db.close()


def test_json_from_chatty_models():
    assert ai.loose_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert ai.loose_json('Sure! Here it is: {"a": 2} hope it helps') == {"a": 2}
    schema = {"type": "object", "required": ["risk"], "properties": {"risk": {"type": "string", "enum": ["low", "high"]}}}
    assert ai.validate({"risk": "medium"}, schema)
    assert ai.validate({"risk": "low"}, schema) == ""


def test_agents_page_and_settings(admin):
    c = admin
    ws = _make_ws(c)
    _due_customers(c, ws, n=5)
    wid = ws["id"]
    c.post(f"/api/workspaces/{wid}/plan/build")
    d = c.get(f"/api/workspaces/{wid}/agents").json()
    assert [a["key"] for a in d["agents"]] == ["timing", "writer", "guard", "analyst", "listener"]
    t = d["agents"][0]
    assert t["stats"][0]["value"] == 5 and t["today"] > 0
    cert = next(r for r in t["rules"] if r["k"] == "certainty")
    assert cert["value"] == 70
    # save rules (bound and plain), a permission, extras
    d = c.patch(f"/api/workspaces/{wid}/agents/timing", json={"rules": {"certainty": 85, "weekend": False, "from": "8:30"},
                                                               "perms": {"pick": "auto"}, "extras": {"country": "in"}}).json()
    t = d["agents"][0]
    vals = {r["k"]: r.get("value") for r in t["rules"]}
    assert vals["certainty"] == 85 and vals["weekend"] is False and vals["from"] == "08:30"
    assert next(a for a in t["actions"] if a["k"] == "pick")["level"] == "auto"
    assert next(e for e in t["extras"] if e["k"] == "country")["value"] == "IN"
    g = c.patch(f"/api/workspaces/{wid}/agents/guard", json={"rules": {"cap": 99, "unsub": 0.7}}).json()["agents"][2]
    assert {r["k"]: r.get("value") for r in g["rules"]}["cap"] == 7      # clamped to the allowed range
    # locked actions cannot be changed
    c.patch(f"/api/workspaces/{wid}/agents/guard", json={"perms": {"remove": "off"}})
    g = c.get(f"/api/workspaces/{wid}/agents").json()["agents"][2]
    assert next(a for a in g["actions"] if a["k"] == "remove")["level"] == "auto"
    # a new level resets the per-action overrides; switching off
    t = c.patch(f"/api/workspaces/{wid}/agents/timing", json={"level": "suggest"}).json()["agents"][0]
    assert next(a for a in t["actions"] if a["k"] == "pick")["level"] == "suggest"
    t = c.patch(f"/api/workspaces/{wid}/agents/timing", json={"on": False}).json()["agents"][0]
    assert t["on"] is False and all(a["level"] == "off" for a in t["actions"])
    audit = c.get(f"/api/workspaces/{wid}/audit").json()["audit"]
    assert any("Timing agent settings changed" in a["text"] for a in audit)
    assert c.patch(f"/api/workspaces/{wid}/agents/timing", json={"extras": {"timezone": "Mars/Base"}}).status_code == 400
