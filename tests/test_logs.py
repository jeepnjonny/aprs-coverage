"""Admin Logs tab: ring buffers, masking and the log API."""
import logging

from app import logbuf
from test_api import H, client, login  # noqa: F401  (fixture)


def test_mask():
    m = logbuf.mask
    assert m("user K7SWI-CF pass 8834 vers x filter g/K7SWI-CF") == "user K7SWI-CF pass ***** vers x filter g/K7SWI-CF"
    assert m("user K7SWI-CV pass -1 vers x") == "user K7SWI-CV pass -1 vers x"  # read-only login stays visible
    assert m("K7SWI-CF>APZCOV,TCPIP*::KJ7NYE-10:CSU s3cret word{12") == "K7SWI-CF>APZCOV,TCPIP*::KJ7NYE-10:CSU ****{12"
    assert m("K7SWI-CF>APZCOV,TCPIP*::KJ7NYE-10:CSR{12") == "K7SWI-CF>APZCOV,TCPIP*::KJ7NYE-10:CSR{12"


def test_ring_log():
    r = logbuf.RingLog(3)
    for i in range(5):
        r.add("rx", "feed", f"line {i}")
    tail = r.since(None, 10)
    assert [e["text"] for e in tail] == ["line 2", "line 3", "line 4"]  # oldest dropped
    assert [e["text"] for e in r.since(tail[0]["id"], 10)] == ["line 3", "line 4"]
    assert r.since(tail[-1]["id"], 10) == []
    assert [e["text"] for e in r.since(None, 1)] == ["line 4"]


def test_log_api(client):  # noqa: F811
    assert client.get("/api/admin/logs").status_code == 401
    login(client)
    ids = [s["id"] for s in client.get("/api/admin/logs").json()]
    assert ids == ["aprs", "app", "packets", "rejects", "nodecfg"]
    assert client.get("/api/admin/logs/nope").status_code == 404

    # ring buffers
    logbuf.APRS.add("tx", "cfg K7SWI-CF", "K7SWI-CF>APZCOV,TCPIP*::KJ7NYE-10:CSU hunter2{5")
    r = client.get("/api/admin/logs/aprs", params={"limit": 1}).json()
    assert r["epoch"] == logbuf.EPOCH and r["entries"][-1]["text"].endswith(":CSU ****{5")
    last = r["entries"][-1]["id"]
    logbuf.APRS.add("rx", "feed", "K7ABC-9>APRS:>hello", "ignore")
    new = client.get("/api/admin/logs/aprs", params={"after": last}).json()["entries"]
    assert [(e["level"], e["note"]) for e in new] == [("rx", "ignore")]

    logging.getLogger("admin").warning("something odd %s", 42)
    app_log = client.get("/api/admin/logs/app").json()["entries"]
    assert any(e["level"] == "WARNING" and e["tag"] == "admin" and e["text"] == "something odd 42" for e in app_log)

    # database-backed logs
    for body in ({"callsign": "K7ABC-9", "kind": "mobile"}, {"callsign": "KG7KMV-10", "kind": "igate"}):
        client.post("/api/admin/stations", json=body, headers=H)
    ing = client.ingester
    assert ing.handle_line("K7ABC-9>APRS,qAR,KG7KMV-10:=4336.66N/11612.40W>", now=2_000_000_000) == "accept"
    assert ing.handle_line("K7ABC-9>APRS,TCPIP*,qAC,T2X:=4337.66N/11612.40W>", now=2_000_000_100) == "reject"
    p = client.get("/api/admin/logs/packets").json()["entries"]
    assert len(p) == 1 and p[0]["level"] == "KG7KMV-10" and p[0]["tag"] == "K7ABC-9" and p[0]["note"] == "igate KG7KMV-10, 0 hops"
    assert client.get("/api/admin/logs/packets", params={"after": p[0]["id"]}).json()["entries"] == []
    rej = client.get("/api/admin/logs/rejects").json()["entries"]
    assert rej[0]["level"] == "not_rf" and rej[0]["tag"] == "K7ABC-9"
    assert client.get("/api/admin/logs/nodecfg").json() == {"epoch": 0, "entries": []}
