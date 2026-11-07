import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APRS_ENABLED", "0")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.sqlite"))
    from app import config
    importlib.reload(config)
    from app import auth, db, ingest, main
    for m in (db, auth, ingest, main):
        importlib.reload(m)
    with TestClient(main.app) as c:
        db.set_setting("admin_password", auth.hash_password("correct horse battery"))
        c.ingester = main.ingester
        yield c


H = {"X-Requested-With": "fetch"}


def login(c):
    assert c.post("/api/admin/login", json={"password": "correct horse battery"}).status_code == 200


def test_admin_requires_login(client):
    assert client.get("/api/admin/stations").status_code == 401
    assert client.post("/api/admin/login", json={"password": "nope"}).status_code == 401


def test_csrf_header_required(client):
    login(client)
    r = client.post("/api/admin/stations", json={"callsign": "K7ABC-9", "kind": "mobile"})
    assert r.status_code == 403


def test_station_crud_and_coverage(client):
    login(client)
    for body in ({"callsign": "k7abc-9", "kind": "mobile"},
                 {"callsign": "K7SWI-1", "kind": "digi", "lat": 43.667, "lon": -116.25},
                 {"callsign": "KG7KMV-10", "kind": "igate"}):
        assert client.post("/api/admin/stations", json=body, headers=H).status_code == 200
    assert client.post("/api/admin/stations", json={"callsign": "K7ABC-9", "kind": "mobile"}, headers=H).status_code == 409
    assert client.post("/api/admin/stations", json={"callsign": "BAD CALL", "kind": "igate"}, headers=H).status_code == 422
    assert client.ingester.registry.aprs_filter() == "b/K7ABC-9/K7SWI-1/KG7KMV-10"

    ing = client.ingester
    assert ing.handle_line("K7ABC-9>APRS,K7SWI-1*,qAR,KG7KMV-10:=4336.66N/11612.40W>", now=2_000_000_000) == "accept"
    assert ing.handle_line("K7ABC-9>APRS,qAR,KG7KMV-10:=4337.66N/11612.40W>", now=2_000_000_100) == "accept"
    assert ing.handle_line("K7ABC-9>APRS,TCPIP*,qAC,T2X:=4337.66N/11612.40W>", now=2_000_000_200) == "reject"

    q = {"start": 1_999_999_000, "end": 2_000_001_000, "cell": 500}
    cov = client.get("/api/coverage", params={**q, "color": "heard_by"}).json()
    assert cov["packets"] == 2 and len(cov["cells"]) == 2
    keys = {k for c in cov["cells"] for k in c[4]}
    assert keys == {"K7SWI-1", "KG7KMV-10"}
    # every square also carries heard-by and mobile breakdowns, in any color mode
    single = client.get("/api/coverage", params=q).json()
    assert all(c[4] == {} for c in single["cells"])
    assert {k for c in single["cells"] for k in c[5]} == {"K7SWI-1", "KG7KMV-10"}
    assert {k for c in single["cells"] for k in c[6]} == {"K7ABC-9"}
    assert all(c[7] <= c[3] for c in single["cells"])

    only_digi = client.get("/api/coverage", params={**q, "heard_by": "K7SWI-1"}).json()
    assert only_digi["packets"] == 1
    any_igate = client.get("/api/coverage", params={**q, "heard_by": "KG7KMV-10", "match": "any"}).json()
    assert any_igate["packets"] == 2
    none = client.get("/api/coverage", params={**q, "mobiles": ""}).json()
    assert none["packets"] == 0

    stats = client.get("/api/stats", params=q).json()
    digi = next(r for r in stats["infra"] if r["callsign"] == "K7SWI-1")
    assert digi["heard_direct"] == 1 and digi["max_range_km"] is not None
    igate = next(r for r in stats["infra"] if r["callsign"] == "KG7KMV-10")
    assert igate["gated"] == 2 and igate["heard_direct"] == 1

    rejects = client.get("/api/admin/rejects").json()
    assert rejects[0]["reason"] == "not_rf"

    sid = next(s["id"] for s in client.get("/api/admin/stations").json() if s["callsign"] == "K7SWI-1")
    assert client.delete(f"/api/admin/stations/{sid}", headers=H).status_code == 200
    assert "K7SWI-1" not in client.ingester.registry.aprs_filter()


def test_purge_station_packets(client):
    login(client)
    for body in ({"callsign": "K7ABC*", "kind": "mobile"},
                 {"callsign": "K7XYZ-9", "kind": "mobile"},
                 {"callsign": "K7SWI-1", "kind": "digi"},
                 {"callsign": "KG7KMV-10", "kind": "igate"}):
        assert client.post("/api/admin/stations", json=body, headers=H).status_code == 200
    ids = {s["callsign"]: s["id"] for s in client.get("/api/admin/stations").json()}
    ing, t = client.ingester, 2_000_000_000
    assert ing.handle_line("K7ABC-9>APRS,K7SWI-1*,qAR,KG7KMV-10:=4336.66N/11612.40W>", now=t) == "accept"
    assert ing.handle_line("K7ABC-7>APRS,qAR,KG7KMV-10:=4337.66N/11612.40W>", now=t + 1) == "accept"
    assert ing.handle_line("K7XYZ-9>APRS,K7SWI-1*,qAR,KG7KMV-10:=4338.66N/11612.40W>", now=t + 2) == "accept"
    assert ing.handle_line("K7XYZ-9>APRS,qAR,KG7KMV-10:=4339.66N/11612.40W>", now=t + 60) == "accept"

    url = lambda call: f"/api/admin/stations/{ids[call]}/packets"
    assert client.get(url("K7ABC*")).json() == {"count": 2}
    assert client.get(url("K7SWI-1")).json() == {"count": 2}
    assert client.delete(url("K7ABC*")).status_code == 403  # CSRF header required

    # Wildcard mobile: both SSIDs go, other mobiles stay, last fixes forgotten.
    assert client.delete(url("K7ABC*"), headers=H).json() == {"deleted": 2}
    assert not any(m.startswith("K7ABC") for m in ing.last)
    # Digi: only what it heard directly; the igate keeps its own direct packet.
    assert client.delete(url("K7SWI-1"), headers=H).json() == {"deleted": 1}
    assert client.get(url("KG7KMV-10")).json() == {"count": 1}
    # Registrations are untouched.
    assert len(client.get("/api/admin/stations").json()) == 4
    assert client.get("/api/admin/stations/9999/packets").status_code == 404


def test_public_endpoints(client):
    assert client.get("/").status_code == 200
    assert client.get("/admin").status_code == 200
    assert client.get("/api/config").json()["cell_sizes"]
    assert client.get("/api/coverage", params={"cell": 123}).status_code == 400


def test_cookie_scoped_to_forwarded_prefix(client):
    pw = {"password": "correct horse battery"}
    r = client.post("/api/admin/login", json=pw, headers={"X-Forwarded-Prefix": "/aprs-coverage"})
    assert "Path=/aprs-coverage/" in r.headers["set-cookie"]
    r = client.post("/api/admin/login", json=pw)
    assert "Path=/;" in r.headers["set-cookie"]
    r = client.post("/api/admin/login", json=pw, headers={"X-Forwarded-Prefix": "/x; Domain=evil"})
    assert "Path=/;" in r.headers["set-cookie"]


def test_pages_and_static_revalidate(client):
    for path in ("/", "/admin", "/static/map.js"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    etag = client.get("/static/map.js").headers["etag"]
    assert client.get("/static/map.js", headers={"If-None-Match": etag}).status_code == 304
