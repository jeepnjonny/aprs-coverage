"""Paths the review found untested: login lockout and sign-up limits over
HTTP, the 'other' / 'untraced' heard-by filters, month buckets, and ending
live streams on shutdown."""
import asyncio

from app import compare, ingest
from conftest import H, login
from test_accounts import other, signup


def test_login_lockout_over_http(client):
    for _ in range(8):
        assert client.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    r = client.post("/api/login", json={"username": "admin", "password": "correct horse battery"})
    assert r.status_code == 429  # this address is locked out, even with the right password


def test_password_change_guesses_are_limited(client):
    login(client)
    for _ in range(8):
        r = client.post("/api/account/password", json={"current": "wrong", "new": "x" * 12}, headers=H)
        assert r.status_code == 400
    r = client.post("/api/account/password", json={"current": "correct horse battery", "new": "x" * 12}, headers=H)
    assert r.status_code == 429


def test_signups_per_address_are_limited(client):
    for i in range(5):
        r = signup(other(client), username=f"user{i}", devices=[{"callsign": f"K7AA{chr(65 + i)}-9", "kind": "mobile"}])
        assert r.status_code == 200, r.text
    r = signup(other(client), username="user9", devices=[{"callsign": "K7AAZ-9", "kind": "mobile"}])
    assert r.status_code == 429


def test_untraced_and_other_heard_by_filters(client):
    login(client)
    for body in ({"callsign": "K7ABC-9", "kind": "mobile"}, {"callsign": "KG7KMV-10", "kind": "igate"}):
        assert client.post("/api/admin/stations", json=body, headers=H).status_code == 200
    ing, t = client.ingester, 2_000_000_000
    ing.handle_line("K7ABC-9>APRS,qAR,KG7KMV-10:=4336.66N/11612.40W>", now=t)            # registered igate
    ing.handle_line("K7ABC-9>APRS,qAR,W7XYZ-10:=4337.66N/11612.40W>", now=t + 100)        # unregistered igate
    ing.handle_line("K7ABC-9>APRS,WIDE2*,qAR,KG7KMV-10:=4338.66N/11612.40W>", now=t + 200)  # untraced digi hop

    def packets(heard_by):
        r = client.get("/api/coverage", params={"start": t - 10, "end": t + 1000, "heard_by": heard_by})
        return r.json()["packets"]
    assert packets("KG7KMV-10") == 1
    assert packets("__other__") == 1
    assert packets("__untraced__") == 1
    assert packets("__other__,__untraced__") == 2
    assert packets("%") == 0  # not a wildcard


def test_month_buckets():
    jan31 = 1_738_281_600   # 2025-01-31 00:00 UTC
    assert compare.bucket_start(jan31 + 3600, "month") == 1_735_689_600   # 2025-01-01
    assert compare.bucket_start(jan31 + 86400, "month") == jan31 + 86400  # 2025-02-01 is its own month
    # In UTC-7, 2025-02-01 03:00 UTC is still January 31.
    assert compare.bucket_start(jan31 + 86400 + 3 * 3600, "month", tz_s=-7 * 3600) == 1_735_689_600 + 7 * 3600


def test_shutdown_ends_live_streams():
    async def go():
        ing = ingest.Ingester()
        q1, q2 = ing.subscribe(), ing.subscribe()
        for _ in range(200):  # one queue already full: the closing flag still ends it
            q2.put_nowait({})
        ing.close_subscribers()
        assert ing.closing and await q1.get() is None
    asyncio.run(go())
