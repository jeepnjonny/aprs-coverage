"""Accounts: sign-up, approval, roles, and node owners limited to their own devices."""
import sqlite3

from fastapi.testclient import TestClient

from test_api import H, client, login  # noqa: F401  (fixture)

PW = "a long enough password"


def signup(c, username="alice", callsign="K7ALC", devices=None, **extra):
    devices = devices or [{"callsign": "K7ALC-10", "kind": "igate", "label": "Home", "lat": 43.6, "lon": -116.2}]
    return c.post("/api/signup", json={"username": username, "password": PW, "callsign": callsign,
                                       "devices": devices, **extra})


def other(c):
    """A second client on the same app, so two people can be logged in at once."""
    return TestClient(c.app)


def device_id(c, call):
    return next(s["id"] for s in c.get("/api/admin/stations").json() if s["callsign"] == call)


def user_id(c, name):
    return next(u["id"] for u in c.get("/api/admin/users").json() if u["username"] == name)


def approve(admin, username, *calls):
    assert admin.put(f"/api/admin/users/{user_id(admin, username)}", json={"status": "active"}, headers=H).status_code == 200
    for call in calls:
        assert admin.post(f"/api/admin/stations/{device_id(admin, call)}/approve", headers=H).status_code == 200


def test_signup_is_pending_until_approved(client):
    alice = other(client)
    r = signup(alice)
    assert r.status_code == 200 and r.json()["user"]["status"] == "pending"
    # Logged in straight away, sees the submission, but nothing is live.
    acct = alice.get("/api/account").json()
    assert [d["status"] for d in acct["devices"]] == ["pending"]
    assert "K7ALC-10" not in client.get("/api/stations").text
    assert "K7ALC-10" not in client.ingester.registry.aprs_filter()
    # Pending users can't reach anything else.
    assert alice.get("/api/remote").status_code == 403
    assert alice.get("/api/admin/stations").status_code == 403
    assert alice.post("/api/account/devices", json={"callsign": "K7ALC-11", "kind": "digi"}, headers=H).status_code == 200

    login(client)
    approve(client, "alice", "K7ALC-10")
    assert "K7ALC-10" in [s["callsign"] for s in client.get("/api/stations").json()]
    assert "K7ALC-10" in client.ingester.registry.aprs_filter()
    assert alice.get("/api/me").json()["user"]["status"] == "active"


def test_signup_validation(client):
    c = other(client)
    assert signup(c, username="x").status_code == 422  # too short
    assert signup(c, callsign="K7ALC-5").status_code == 422  # base callsign only
    assert signup(c, devices=[{"callsign": "BAD CALL", "kind": "igate"}]).status_code == 422
    assert c.post("/api/signup", json={"username": "bob", "password": PW, "callsign": "K7BOB",
                                       "devices": []}).status_code == 422
    assert c.post("/api/signup", json={"username": "bob", "password": "short", "callsign": "K7BOB",
                                       "devices": [{"callsign": "K7BOB-9", "kind": "mobile"}]}).status_code == 422
    two = [{"callsign": "K7BOB-9", "kind": "mobile"}, {"callsign": "K7BOB-9", "kind": "mobile"}]
    assert signup(c, username="bob", callsign="K7BOB", devices=two).status_code == 422
    # Already registered callsigns and usernames are refused.
    login(client)
    assert client.post("/api/admin/stations", json={"callsign": "K7SWI-1", "kind": "digi"}, headers=H).status_code == 200
    assert signup(other(client), devices=[{"callsign": "K7SWI-1", "kind": "digi"}]).status_code == 409
    assert signup(other(client)).status_code == 200
    assert signup(other(client), devices=[{"callsign": "K7ALC-11", "kind": "igate"}]).status_code == 409
    # Bots that fill the hidden field get nowhere.
    r = signup(other(client), username="spammer", callsign="K7SPM", website="http://spam")
    assert r.status_code == 200 and r.json()["user"] is None
    assert "spammer" not in [u["username"] for u in client.get("/api/admin/users").json()]


def test_owner_manages_only_their_devices(client):
    alice, bob = other(client), other(client)
    signup(alice, devices=[{"callsign": "K7ALC-10", "kind": "igate"}, {"callsign": "K7ALC-9", "kind": "mobile"}])
    signup(bob, username="bob", callsign="K7BOB", devices=[{"callsign": "K7BOB-10", "kind": "digi"}])
    login(client)
    approve(client, "alice", "K7ALC-10")
    approve(client, "bob", "K7BOB-10")
    bob_dev = device_id(client, "K7BOB-10")
    a_igate, a_mobile = device_id(client, "K7ALC-10"), device_id(client, "K7ALC-9")

    # Someone else's device looks missing.
    edit = {"callsign": "K7BOB-10", "kind": "digi", "label": "mine now"}
    assert alice.put(f"/api/account/devices/{bob_dev}", json=edit, headers=H).status_code == 404
    assert alice.delete(f"/api/account/devices/{bob_dev}", headers=H).status_code == 404
    # Admin routes are off limits.
    assert alice.get("/api/admin/stations").status_code == 403
    assert alice.put("/api/admin/feeds", json={}, headers=H).status_code == 403
    assert alice.delete(f"/api/admin/stations/{bob_dev}/packets", headers=H).status_code == 403
    assert alice.get("/api/admin/users").status_code == 403
    assert alice.get("/api/admin/logs").status_code == 403

    # Approved: label and position can change, callsign and type can't.
    body = {"callsign": "K7ALC-10", "kind": "igate", "label": "Ridge", "lat": 43.7, "lon": -116.1}
    assert alice.put(f"/api/account/devices/{a_igate}", json=body, headers=H).status_code == 200
    assert alice.put(f"/api/account/devices/{a_igate}", json={**body, "callsign": "K7ALC-11"}, headers=H).status_code == 422
    assert alice.put(f"/api/account/devices/{a_igate}", json={**body, "kind": "digi"}, headers=H).status_code == 422
    # Still pending: anything can change.
    assert alice.put(f"/api/account/devices/{a_mobile}", json={"callsign": "K7ALC-7", "kind": "mobile"},
                     headers=H).status_code == 200
    # New devices queue for approval.
    assert alice.post("/api/account/devices", json={"callsign": "K7ALC-12", "kind": "digi"}, headers=H).status_code == 200
    statuses = {d["callsign"]: d["status"] for d in alice.get("/api/account").json()["devices"]}
    assert statuses == {"K7ALC-10": "approved", "K7ALC-7": "pending", "K7ALC-12": "pending"}
    assert "K7ALC-12" not in client.ingester.registry.aprs_filter()
    # Deactivating takes it out of the filter; deleting removes it.
    assert alice.put(f"/api/account/devices/{a_igate}", json={**body, "active": False}, headers=H).status_code == 200
    assert "K7ALC-10" not in client.ingester.registry.aprs_filter()
    assert alice.delete(f"/api/account/devices/{a_igate}", headers=H).status_code == 200
    assert "K7ALC-10" not in [s["callsign"] for s in client.get("/api/admin/stations").json()]


def test_reject_device_and_resubmit(client):
    alice = other(client)
    signup(alice)
    login(client)
    approve(client, "alice")
    sid = device_id(client, "K7ALC-10")
    assert client.post(f"/api/admin/stations/{sid}/reject", json={"note": "Not in the area"}, headers=H).status_code == 200
    dev = alice.get("/api/account").json()["devices"][0]
    assert dev["status"] == "rejected" and dev["review_note"] == "Not in the area"
    assert alice.put(f"/api/account/devices/{sid}", json={"callsign": "K7ALC-10", "kind": "igate"}, headers=H).status_code == 200
    dev = alice.get("/api/account").json()["devices"][0]
    assert dev["status"] == "pending" and dev["review_note"] == ""


def test_reject_signup_deletes_it(client):
    signup(other(client))
    login(client)
    assert client.delete(f"/api/admin/users/{user_id(client, 'alice')}", headers=H).status_code == 200
    assert client.get("/api/admin/stations").json() == []
    assert signup(other(client)).status_code == 200  # the username and callsign are free again


def test_withdraw_signup(client):
    alice = other(client)
    signup(alice)
    assert alice.delete("/api/account", headers=H).status_code == 200
    assert alice.get("/api/me").json()["user"] is None
    login(client)
    assert [u["username"] for u in client.get("/api/admin/users").json()] == ["admin"]


def test_disabled_user_is_logged_out(client):
    alice = other(client)
    signup(alice)
    login(client)
    uid = user_id(client, "alice")
    assert client.put(f"/api/admin/users/{uid}", json={"status": "disabled"}, headers=H).status_code == 200
    assert alice.get("/api/account").status_code == 401
    r = alice.post("/api/login", json={"username": "alice", "password": PW})
    assert r.status_code == 403


def test_last_admin_is_protected(client):
    login(client)
    me = user_id(client, "admin")
    assert client.put(f"/api/admin/users/{me}", json={"role": "node_owner", "callsign": "K7ADM"}, headers=H).status_code == 409
    assert client.put(f"/api/admin/users/{me}", json={"status": "disabled"}, headers=H).status_code == 409
    assert client.delete(f"/api/admin/users/{me}", headers=H).status_code == 409
    # A second admin makes it possible.
    signup(other(client), username="carol", callsign="K7CRL")
    carol = user_id(client, "carol")
    assert client.put(f"/api/admin/users/{carol}", json={"status": "active", "role": "admin"}, headers=H).status_code == 200
    assert client.put(f"/api/admin/users/{me}", json={"role": "node_owner", "callsign": "K7ADM"}, headers=H).status_code == 200


def test_password_change_and_admin_reset(client):
    alice, alice2 = other(client), other(client)
    signup(alice)
    assert alice2.post("/api/login", json={"username": "ALICE", "password": PW}).status_code == 200  # case-insensitive
    new = "another long password"
    assert alice.post("/api/account/password", json={"current": "wrong", "new": new}, headers=H).status_code == 400
    assert alice.post("/api/account/password", json={"current": PW, "new": new}, headers=H).status_code == 200
    assert alice.get("/api/account").status_code == 200  # this session stays
    assert alice2.get("/api/account").status_code == 401  # the other one ends
    login(client)
    uid = user_id(client, "alice")
    assert client.post(f"/api/admin/users/{uid}/password", json={"new": PW}, headers=H).status_code == 200
    assert alice.get("/api/account").status_code == 401
    assert other(client).post("/api/login", json={"username": "alice", "password": PW}).status_code == 200


def test_remote_config_needs_admin_tick(client):
    alice = other(client)
    signup(alice, devices=[{"callsign": "K7ALC-10", "kind": "igate"}])
    signup(other(client), username="bob", callsign="K7BOB", devices=[{"callsign": "K7BOB-10", "kind": "igate"}])
    login(client)
    approve(client, "alice", "K7ALC-10")
    approve(client, "bob", "K7BOB-10")
    assert alice.get("/api/remote").status_code == 403
    uid = user_id(client, "alice")
    assert client.put(f"/api/admin/users/{uid}", json={"can_config": True}, headers=H).status_code == 200
    d = alice.get("/api/remote").json()
    assert [n["callsign"] for n in d["nodes"]] == ["K7ALC-10"] and d["own_callsign"] == "K7ALC"
    # Only their own node, and only from their own callsign.
    r = alice.post("/api/remote/K7BOB-10/read", json={"login": "K7ALC-CF"}, headers=H)
    assert r.status_code == 404
    r = alice.post("/api/remote/K7ALC-10/read", json={"login": "K7BOB-CF"}, headers=H)
    assert r.status_code == 403 and "K7ALC" in r.json()["detail"]
    # The admin sees every node.
    assert {n["callsign"] for n in client.get("/api/remote").json()["nodes"]} == {"K7ALC-10", "K7BOB-10"}


def test_admin_assigns_owner(client):
    login(client)
    assert client.post("/api/admin/stations", json={"callsign": "K7SWI-1", "kind": "digi"}, headers=H).status_code == 200
    alice = other(client)
    signup(alice)
    approve(client, "alice")
    sid, uid = device_id(client, "K7SWI-1"), user_id(client, "alice")
    assert client.put(f"/api/admin/stations/{sid}", json={"callsign": "K7SWI-1", "kind": "digi", "owner_id": uid},
                      headers=H).status_code == 200
    assert "K7SWI-1" in [d["callsign"] for d in alice.get("/api/account").json()["devices"]]
    # An edit that leaves owner_id out keeps the owner.
    assert client.put(f"/api/admin/stations/{sid}", json={"callsign": "K7SWI-1", "kind": "digi", "label": "x"},
                      headers=H).status_code == 200
    assert "K7SWI-1" in [d["callsign"] for d in alice.get("/api/account").json()["devices"]]
    # Deleting the user keeps approved devices, unowned.
    assert client.delete(f"/api/admin/users/{uid}", headers=H).status_code == 200
    st = next(s for s in client.get("/api/admin/stations").json() if s["callsign"] == "K7SWI-1")
    assert st["owner_id"] is None


def test_old_admin_password_migrates(tmp_path, monkeypatch):
    """A database from before accounts: its password becomes user "admin"."""
    import importlib
    monkeypatch.setenv("DB_PATH", str(tmp_path / "old.sqlite"))
    from app import auth, config, db
    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(auth)
    conn = sqlite3.connect(tmp_path / "old.sqlite")
    conn.executescript("""
        CREATE TABLE stations (id INTEGER PRIMARY KEY, callsign TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', color TEXT, lat REAL, lon REAL,
            auto_lat REAL, auto_lon REAL, last_beacon INTEGER, active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE packets (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, mobile TEXT NOT NULL, lat REAL NOT NULL,
            lon REAL NOT NULL, alt REAL, speed REAL, course INTEGER, igate TEXT NOT NULL, heard_by TEXT,
            hops INTEGER NOT NULL, chain TEXT NOT NULL, raw TEXT NOT NULL);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, expires INTEGER NOT NULL);
        INSERT INTO stations(callsign, kind) VALUES ('K7SWI-1', 'digi');
        INSERT INTO sessions VALUES ('abc', 9999999999);
    """)
    conn.execute("INSERT INTO settings VALUES ('admin_password', ?)", (auth.hash_password("old password here"),))
    conn.commit()
    conn.close()
    db.init()
    u = auth.authenticate("admin", "old password here")
    assert u is not None and u["role"] == "admin" and u["status"] == "active"
    assert db.get_setting("admin_password") is None
    with db.session() as c:
        assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        st = c.execute("SELECT status, owner_id FROM stations").fetchone()
    assert st["status"] == "approved" and st["owner_id"] is None
