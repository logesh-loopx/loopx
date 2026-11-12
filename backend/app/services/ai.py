"""The AI brain for the five agents and the console's AI features.

Works with free providers through their OpenAI-compatible APIs, plus Claude:
  groq        free tier, fast; the default
  gemini      Google AI Studio free tier
  openrouter  free ":free" models
  ollama      a model running on your own computer (no key, no limits)
  anthropic   Claude (paid)

Providers are tried in order. If one is rate-limited, out of its daily budget or
failing, the next one answers. If none can, callers get `None` and the agents
fall back to their rules, so LOOPX never stops.

Keys and models come from the console (Connections > AI), or from .env.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from datetime import date

import httpx

from ..config import settings

log = logging.getLogger(__name__)

PROVIDERS: dict[str, dict] = {
    "groq": {"label": "Groq (free)", "base": "https://api.groq.com/openai/v1",
             "fast": "llama-3.1-8b-instant", "smart": "openai/gpt-oss-120b",
             "rpm": 25, "tpm": 7000, "rpd": 900, "env": "GROQ_API_KEY", "signup": "https://console.groq.com/keys"},
    "gemini": {"label": "Google Gemini (free)", "base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "fast": "gemini-3.8-flash", "smart": "gemini-3.8-flash",
               "rpm": 8, "tpm": 200000, "rpd": 200, "env": "GEMINI_API_KEY", "signup": "https://aistudio.google.com/apikey"},
    "openrouter": {"label": "OpenRouter (free models)", "base": "https://openrouter.ai/api/v1",
                   "fast": "meta-llama/llama-3.3-70b-instruct:free", "smart": "meta-llama/llama-3.3-70b-instruct:free",
                   "rpm": 15, "tpm": 0, "rpd": 45, "env": "OPENROUTER_API_KEY", "signup": "https://openrouter.ai/keys"},
    "ollama": {"label": "Ollama (this computer)", "base": "http://localhost:11434/v1",
               "fast": "llama3.2", "smart": "llama3.2", "rpm": 0, "tpm": 0, "rpd": 0, "env": "OLLAMA_URL",
               "keyless": True, "signup": "https://ollama.com/download"},
    "anthropic": {"label": "Claude (paid)", "fast": "", "smart": "", "rpm": 45, "tpm": 0, "rpd": 0,
                  "env": "ANTHROPIC_API_KEY", "signup": "https://console.anthropic.com/"},
}
DEFAULT_ORDER = ["groq", "gemini", "openrouter", "ollama", "anthropic"]
MAX_WAIT = 25  # seconds a call may wait for a provider's per-minute limit before trying the next one

_lock = threading.RLock()
_windows: dict[str, deque] = {}       # provider -> deque[(time, tokens)]
_cool: dict[str, float] = {}          # provider -> time until which it is skipped
_cfg_cache: dict = {"at": 0.0, "cfg": None}
_usage_mem: dict = {}                 # day -> provider -> counters (mirrored to the DB)
_last_error: dict[str, str] = {}


class _Skip(Exception):
    pass


# ---------------------------------------------------------------- configuration
def _db_setting(key: str) -> dict:
    try:
        from ..db import SessionLocal
        from ..models import AppSetting

        with SessionLocal() as db:
            row = db.get(AppSetting, key)
            return dict(row.value or {}) if row else {}
    except Exception:
        return {}


def save_setting(key: str, value: dict) -> None:
    from ..db import SessionLocal
    from ..models import AppSetting

    with SessionLocal() as db:
        row = db.get(AppSetting, key)
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=key, value=value))
        db.commit()
    _cfg_cache["at"] = 0.0


def config() -> dict:
    """Merged AI config: console settings first, then .env."""
    now = time.time()
    if _cfg_cache["cfg"] is not None and now - _cfg_cache["at"] < 20:
        return _cfg_cache["cfg"]
    import os

    saved = _db_setting("ai")
    order = saved.get("order") or [p.strip() for p in (os.getenv("AI_ORDER") or "").split(",") if p.strip()] or DEFAULT_ORDER
    provs = {}
    for name, spec in PROVIDERS.items():
        s = dict((saved.get("providers") or {}).get(name) or {})
        env_val = (os.getenv(spec["env"]) or "").strip()
        if name == "anthropic":
            env_val = env_val or settings.anthropic_api_key
        key = s.get("key") or ("" if spec.get("keyless") else env_val)
        base = s.get("base") or (env_val if name == "ollama" and env_val else spec.get("base", ""))
        fast = s.get("fast") or (settings.fast_model if name == "anthropic" else spec["fast"])
        smart = s.get("smart") or (settings.smart_model if name == "anthropic" else spec["smart"])
        configured = bool(key) or (spec.get("keyless") and (s.get("on") or env_val))
        provs[name] = {"key": key, "base": base.rstrip("/"), "fast": fast, "smart": smart,
                       "on": bool(s.get("on", True)) and bool(configured), "configured": bool(configured),
                       "rpd": int(s.get("rpd") or spec["rpd"] or 0), "rpm": int(spec["rpm"] or 0), "tpm": int(spec["tpm"] or 0)}
    tav = saved.get("tavily") or {}
    cfg = {"order": [o for o in order if o in PROVIDERS] + [o for o in DEFAULT_ORDER if o not in order],
           "providers": provs,
           "tavily": {"key": tav.get("key") or (os.getenv("TAVILY_API_KEY") or "").strip(),
                      "monthly": int(tav.get("monthly") or 900)}}
    _cfg_cache.update(at=now, cfg=cfg)
    return cfg


def available() -> bool:
    return any(p["on"] for p in config()["providers"].values())


def active_chain() -> list[str]:
    c = config()
    return [n for n in c["order"] if c["providers"][n]["on"]]


# ---------------------------------------------------------------- usage + limits
def _today() -> str:
    return date.today().isoformat()


def usage() -> dict:
    day = _today()
    if day not in _usage_mem:
        stored = _db_setting("ai_usage")
        _usage_mem.clear()
        _usage_mem[day] = (stored.get(day) or {}) if isinstance(stored.get(day), dict) else {}
    return _usage_mem[day]


def save_setting_later(key: str, build) -> None:
    """Write a setting from a background thread, so an agent's open database transaction never waits on it
    (SQLite allows one writer at a time). `build(current_value) -> new_value`."""
    def run():
        try:
            save_setting(key, build(_db_setting(key)))
        except Exception as e:
            log.debug("background save of %s skipped: %s", key, e)
    threading.Thread(target=run, name=f"save-{key}", daemon=True).start()


def _count(provider: str, tokens: int = 0, error: bool = False) -> None:
    with _lock:
        u = usage().setdefault(provider, {"requests": 0, "tokens": 0, "errors": 0})
        u["requests"] += 1
        u["tokens"] += int(tokens or 0)
        u["errors"] += 1 if error else 0
        snapshot = {k: dict(v) for k, v in usage().items()}
    day = _today()
    keep_from = date.fromordinal(date.today().toordinal() - 14).isoformat()
    save_setting_later("ai_usage", lambda stored: {**{k: v for k, v in stored.items() if k >= keep_from}, day: snapshot})


def _acquire(name: str, p: dict, est_tokens: int) -> list:
    """Wait (briefly) for room under the provider's per-minute limits."""
    deadline = time.time() + MAX_WAIT
    while True:
        with _lock:
            if _cool.get(name, 0) > time.time():
                raise _Skip(f"{name} is cooling down after a rate limit")
            if p["rpd"] and usage().get(name, {}).get("requests", 0) >= p["rpd"]:
                raise _Skip(f"{name} used its daily budget of {p['rpd']} requests")
            w = _windows.setdefault(name, deque())
            now = time.time()
            while w and now - w[0][0] > 60:
                w.popleft()
            req_ok = not p["rpm"] or len(w) < p["rpm"]
            tok_ok = not p["tpm"] or sum(e[1] for e in w) + est_tokens <= p["tpm"] or not w
            if req_ok and tok_ok:
                entry = [now, est_tokens]
                w.append(entry)
                return entry
            wait = 60 - (now - w[0][0]) + 0.2
        if time.time() + wait > deadline:
            raise _Skip(f"{name} is at its per-minute limit")
        time.sleep(min(wait, 5))


def _est(messages: list[dict], max_tokens: int) -> int:
    """Rough token count to book against a per-minute limit; corrected with the real count after the call."""
    return sum(len(str(m.get("content", ""))) for m in messages) // 4 + min(max_tokens, 600)


# ---------------------------------------------------------------- calling providers
def _call_openai(name: str, p: dict, messages: list[dict], model: str, max_tokens: int, json_mode: bool) -> tuple[str, int]:
    headers = {"Content-Type": "application/json"}
    if p["key"]:
        headers["Authorization"] = f"Bearer {p['key']}"
    if name == "openrouter":
        headers.update({"HTTP-Referer": settings.public_url, "X-Title": "LOOPX"})
    body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.3}
    if (name == "groq" and model.startswith("openai/gpt-oss")) or name == "gemini":
        body["reasoning_effort"] = "low"  # thinking models: keep room for the actual answer
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = httpx.post(p["base"] + "/chat/completions", json=body, headers=headers, timeout=90)
    for opt in ("response_format", "reasoning_effort"):  # a model that does not support an option: drop it and retry
        if r.status_code == 400 and opt in body and opt.split("_")[0] in r.text.lower():
            body.pop(opt, None)
            r = httpx.post(p["base"] + "/chat/completions", json=body, headers=headers, timeout=90)
    if r.status_code == 429:
        ra = r.headers.get("retry-after")
        try:
            secs = float(ra) if ra else 60.0
        except ValueError:
            secs = 60.0
        _cool[name] = time.time() + min(max(secs, 5), 3600)
        raise _Skip(f"{name} rate limit: {r.text[:160]}")
    if r.status_code in (401, 403):
        _cool[name] = time.time() + 600
        raise _Skip(f"{name} rejected the key ({r.status_code})")
    if r.status_code >= 400:
        raise _Skip(f"{name} error {r.status_code}: {r.text[:200]}")
    j = r.json()
    msg = ((j.get("choices") or [{}])[0].get("message") or {})
    text = msg.get("content") or ""
    return text.strip(), int((j.get("usage") or {}).get("total_tokens") or 0)


def _call_anthropic(p: dict, messages: list[dict], model: str, max_tokens: int, system: str) -> tuple[str, int]:
    import anthropic

    c = anthropic.Anthropic(api_key=p["key"], max_retries=1, timeout=60)
    kw = dict(model=model, max_tokens=max_tokens, messages=[m for m in messages if m["role"] != "system"])
    if system:
        kw["system"] = system
    r = c.messages.create(**kw)
    text = "".join(b.text for b in r.content if b.type == "text").strip()
    return text, int(getattr(r.usage, "input_tokens", 0) + getattr(r.usage, "output_tokens", 0))


def _complete(messages: list[dict], *, tier: str, system: str, max_tokens: int, json_mode: bool,
              only: str | None = None) -> tuple[str, str] | None:
    """Try each provider in order. Returns (text, provider) or None."""
    cfg = config()
    chain = [only] if only else active_chain()
    for name in chain:
        p = cfg["providers"].get(name)
        if not p or not (p["on"] or only):
            continue
        model = p["smart"] if tier != "quick" else p["fast"]
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        try:
            slot = _acquire(name, p, _est(msgs, max_tokens))
            if name == "anthropic":
                text, toks = _call_anthropic(p, messages, model, max_tokens, system)
            else:
                text, toks = _call_openai(name, p, msgs, model, max_tokens, json_mode)
            if toks:
                with _lock:
                    slot[1] = toks  # book what the call really used
            _count(name, toks)
            if text:
                _last_error.pop(name, None)
                return text, name
            raise _Skip(f"{name} returned an empty answer")
        except _Skip as e:
            _last_error[name] = str(e)[:300]
            log.info("AI provider skipped: %s", e)
        except Exception as e:  # network errors, timeouts
            _count(name, 0, error=True)
            _last_error[name] = str(e)[:300]
            log.warning("AI provider %s failed: %s", name, e)
    return None


# ---------------------------------------------------------------- public helpers
def text(prompt: str | list, *, tier: str = "default", system: str = "", max_tokens: int = 1200) -> str | None:
    messages = prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
    out = _complete(messages, tier=tier, system=system, max_tokens=max_tokens, json_mode=False)
    return out[0] if out else None


def structured(prompt: str, schema: dict, *, tier: str = "quick", system: str = "", name: str = "submit",
               max_tokens: int = 1500) -> dict | None:
    """Ask for a JSON object that matches `schema`. Validates it and retries once."""
    sys2 = (system + "\n\n" if system else "") + (
        "Answer with ONE JSON object only (no prose, no code fences) that matches this JSON Schema:\n"
        + json.dumps(schema, ensure_ascii=False))
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(2):
        out = _complete(messages, tier=tier, system=sys2, max_tokens=max_tokens, json_mode=True)
        if not out:
            return None
        data = loose_json(out[0])
        err = "no JSON object found" if not isinstance(data, dict) else validate(data, schema)
        if not err:
            data["_provider"] = out[1]
            return data
        messages = messages + [{"role": "assistant", "content": out[0][:4000]},
                               {"role": "user", "content": f"That was not valid ({err}). Return only the corrected JSON object."}]
    return None


def validate(data, schema: dict) -> str:
    """Small JSON-schema check: required keys, types, enums (recursively for objects/arrays)."""
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    pyt = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}
    if types and not any(isinstance(data, pyt[x]) and not (x in ("integer", "number") and isinstance(data, bool)) for x in types if x in pyt):
        return f"expected {types}, got {type(data).__name__}"
    if "enum" in schema and data not in schema["enum"]:
        return f"{data!r} is not one of {schema['enum']}"
    if isinstance(data, dict):
        for k in schema.get("required", []):
            if k not in data:
                return f"missing '{k}'"
        for k, sub in (schema.get("properties") or {}).items():
            if k in data:
                e = validate(data[k], sub)
                if e:
                    return f"{k}: {e}"
    if isinstance(data, list) and "items" in schema:
        for i, v in enumerate(data):
            e = validate(v, schema["items"])
            if e:
                return f"[{i}]: {e}"
    return ""


def loose_json(s: str | None):
    """Pull the first JSON object out of a model answer."""
    if not s:
        return None
    s = re.sub(r"^```(?:json)?|```$", "", s.strip(), flags=re.M).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def status() -> dict:
    cfg = config()
    u = usage()
    out = []
    for name in cfg["order"]:
        p, spec = cfg["providers"][name], PROVIDERS[name]
        out.append({"name": name, "label": spec["label"], "on": p["on"], "configured": p["configured"],
                    "fast": p["fast"], "smart": p["smart"], "base": p["base"] if name != "anthropic" else "",
                    "dailyBudget": p["rpd"], "usedToday": (u.get(name) or {}).get("requests", 0),
                    "tokensToday": (u.get(name) or {}).get("tokens", 0),
                    "cooling": max(0, int(_cool.get(name, 0) - time.time())), "lastError": _last_error.get(name, ""),
                    "signup": spec["signup"], "keyless": bool(spec.get("keyless")),
                    "keySet": bool(p["key"]), "keyHint": ("••••" + p["key"][-4:]) if p["key"] else ""})
    from . import research

    return {"available": available(), "chain": active_chain(), "providers": out, "tavily": research.status()}


def test(provider: str) -> dict:
    t0 = time.time()
    out = _complete([{"role": "user", "content": "Reply with the single word OK."}], tier="quick", system="",
                    max_tokens=200, json_mode=False, only=provider)
    if out:
        return {"ok": True, "answer": out[0][:80], "seconds": round(time.time() - t0, 1)}
    return {"ok": False, "error": _last_error.get(provider, "No answer.")}


def client():  # kept for older callers
    return None
