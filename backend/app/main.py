"""LOOPX server: the console UI, its API, and the agent scheduler.

Run locally:   uvicorn app.main:app --reload   (from the backend/ folder)
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse

from .config import settings
from .db import SessionLocal, init_db
from .routes import api, public

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("loopx")
_stop = threading.Event()


def _scheduler():
    """Runs the agents every TICK_SECONDS while the server is awake.
    On free hosts that sleep, the /cron/tick endpoint (called by a free cron service) does the same job."""
    from .agents.orchestrator import tick_all

    time.sleep(5)
    while not _stop.is_set():
        db = SessionLocal()
        try:
            res = tick_all(db)
            sent = sum(r.get("sent", 0) or 0 for r in res)
            if sent:
                log.info("agents tick: %s emails sent", sent)
        except Exception:
            log.exception("scheduler tick failed")
        finally:
            db.close()
        _stop.wait(settings.tick_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    import os
    if not (os.getenv("SECRET_KEY") or "").strip() or os.getenv("SECRET_KEY") in ("change-me",):
        log.warning("SECRET_KEY is not set (or still 'change-me'). Set a long random value, or tracking links break on restart.")
    if settings.seed_demo:
        from .seed import seed_demo
        seed_demo()
    t = None
    if settings.run_scheduler:
        t = threading.Thread(target=_scheduler, name="loopx-agents", daemon=True)
        t.start()
    log.info("LOOPX ready at %s (mail: %s)", settings.public_url, settings.mail_provider)
    yield
    _stop.set()


app = FastAPI(title="LOOPX", version="1.0.0", lifespan=lifespan)
app.include_router(api.router)
app.include_router(public.router)


FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#5B3DF5"/>'
           '<path d="M16 8a8 8 0 1 0 7.4 11" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round"/></svg>')


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import Response
    return Response(FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/", include_in_schema=False)
def index():
    if settings.frontend_file.exists():
        return FileResponse(settings.frontend_file, media_type="text/html", headers={"Cache-Control": "no-cache"})
    return HTMLResponse("<h1>LOOPX API is running</h1><p>Build the console with <code>python scripts/build_frontend.py</code>.</p>")
