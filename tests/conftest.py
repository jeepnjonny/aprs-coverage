import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import importlib  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APRS_ENABLED", "0")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.sqlite"))
    from app import config
    importlib.reload(config)
    from app import auth, db, ingest, main, remotecfg
    from app.routes import accounts, admin, public, remote, shared
    # Fresh module state per test (rate limits, the ingester, the remote-config
    # sender). Route modules after what they import, main last.
    for m in (db, auth, ingest, remotecfg, shared, public, accounts, admin, remote, main):
        importlib.reload(m)
    with TestClient(main.app) as c:
        with db.session() as conn:
            auth.create_user(conn, "admin", "correct horse battery", role="admin", status="active")
        c.ingester = main.ingester
        yield c


H = {"X-Requested-With": "fetch"}


def login(c):
    assert c.post("/api/login", json={"username": "admin", "password": "correct horse battery"}).status_code == 200
