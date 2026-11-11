import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "RUN_SCHEDULER": "false",
    "WAVE_WAIT_MINUTES": "0",
    "PLAN_HOUR": "0",
    "SECRET_KEY": "test-secret",
    "CRON_SECRET": "cron-test",
    "MAIL_PROVIDER": "console",
    "ANTHROPIC_API_KEY": "",
    "SEED_DEMO": "false",
    "PUBLIC_URL": "http://testserver",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin(client):
    r = client.post("/api/auth/signup", json={"email": "logesh@loopx.test", "password": "supersecret1", "name": "Logesh"})
    assert r.status_code == 200, r.text
    return client
