"""Web research for the agents with Tavily (free plan: 1,000 credits a month).

Used by:
  * the Analyst agent: email benchmarks for the client's industry (cached for 30 days)
  * playbook drafting: reads the client's website so the drafted emails match the business
Results are cached so the monthly free credits last.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import date

import httpx

log = logging.getLogger(__name__)
API = "https://api.tavily.com"
CACHE_DAYS = 30


def _cfg() -> dict:
    from .ai import config

    return config()["tavily"]


def available() -> bool:
    return bool(_cfg()["key"])


def _month() -> str:
    return date.today().strftime("%Y-%m")


def _usage() -> dict:
    from .ai import _db_setting

    return _db_setting("tavily_usage")


def credits_used() -> int:
    return max(int(_usage().get(_month(), 0)), _spent_mem.get(_month(), 0))


_spent_mem: dict = {}


def _spend(n: int) -> None:
    from .ai import save_setting_later

    m = _month()
    _spent_mem[m] = _spent_mem.get(m, 0) + n
    save_setting_later("tavily_usage", lambda u: {**{k: v for k, v in u.items() if k >= m[:4]}, m: int(u.get(m, 0)) + n})


def _cache_get(kind: str, key: str):
    from .ai import _db_setting

    row = _db_setting(f"tavily:{kind}:{hashlib.sha1(key.encode()).hexdigest()[:16]}")
    if row and time.time() - row.get("at", 0) < CACHE_DAYS * 86400:
        return row.get("data")
    return None


def _cache_put(kind: str, key: str, data) -> None:
    from .ai import save_setting_later

    row = {"at": time.time(), "data": data}
    save_setting_later(f"tavily:{kind}:{hashlib.sha1(key.encode()).hexdigest()[:16]}", lambda _old: row)


def _post(path: str, body: dict, credits: int) -> dict | None:
    cfg = _cfg()
    if not cfg["key"]:
        return None
    if credits_used() + credits > cfg["monthly"]:
        log.info("Tavily monthly budget reached (%s credits)", cfg["monthly"])
        return None
    try:
        r = httpx.post(API + path, json=body, timeout=40, headers={"Authorization": f"Bearer {cfg['key']}"})
    except httpx.HTTPError as e:
        log.warning("Tavily unreachable: %s", e)
        return None
    if r.status_code >= 400:
        log.warning("Tavily %s answered %s: %s", path, r.status_code, r.text[:200])
        return None
    _spend(credits)
    return r.json()


def search(query: str, max_results: int = 5) -> list[dict]:
    """Basic search (1 credit). Returns [{title, url, content}]."""
    hit = _cache_get("search", query)
    if hit is not None:
        return hit
    j = _post("/search", {"query": query, "max_results": max_results, "search_depth": "basic", "include_answer": False}, 1)
    if not j:
        return []
    res = [{"title": x.get("title", ""), "url": x.get("url", ""), "content": (x.get("content") or "")[:700]}
           for x in j.get("results", [])][:max_results]
    _cache_put("search", query, res)
    return res


def extract(url: str) -> str:
    """Readable text of one page (1 credit per 5 pages). Returns '' if unavailable."""
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url
    hit = _cache_get("extract", url)
    if hit is not None:
        return hit
    j = _post("/extract", {"urls": [url], "extract_depth": "basic"}, 1)
    if not j:
        return ""
    res = ((j.get("results") or [{}])[0] or {}).get("raw_content") or ""
    res = res[:6000]
    _cache_put("extract", url, res)
    return res


def status() -> dict:
    cfg = _cfg()
    return {"on": bool(cfg["key"]), "keyHint": ("••••" + cfg["key"][-4:]) if cfg["key"] else "",
            "monthlyBudget": cfg["monthly"], "usedThisMonth": credits_used(), "signup": "https://app.tavily.com"}
