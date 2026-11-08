"""The aprsc duplicates feed: copies of one packet gated by several igates."""
import asyncio
import sqlite3

from app import packets
from app.packets import RecentTx, Registry, evaluate, strip_dupe_prefix, tx_key
from test_api import H, client, login  # noqa: F401  (fixture)

T = 2_000_000_000
INFO = ":=4336.66N/11612.40W>test"
# K7ABC-9 heard by digi SQB, gated by KG7KMV-13: the copy APRS-IS passes on.
VIA_SQB = "K7ABC-9>APRS,SQB*,WIDE2-1,qAR,KG7KMV-13" + INFO
# Copies aprsc dropped: KJ7NYE-10 heard the mobile directly, and also via SQB.
DIRECT_NYE = "K7ABC-9>APRS,WIDE1-1,WIDE2-1,qAR,KJ7NYE-10" + INFO
VIA_SQB_NYE = "K7ABC-9>APRS,SQB*,WIDE2-1,qAR,KJ7NYE-10" + INFO


def test_strip_dupe_prefix():
    assert strip_dupe_prefix("dup\t" + VIA_SQB) == VIA_SQB
    assert strip_dupe_prefix(VIA_SQB) == VIA_SQB
    assert strip_dupe_prefix("dupe>APRS:x") == "dupe>APRS:x"  # a callsign, not the prefix


def test_tx_key_ignores_path():
    assert tx_key("K7ABC-9", VIA_SQB) == tx_key("K7ABC-9", DIRECT_NYE)
    assert tx_key("K7ABC-9", VIA_SQB) != tx_key("K7ABC-9", VIA_SQB.replace("test", "other"))


def test_recent_tx_window():
    r = RecentTx(30)
    r.add("k", 100, 7, ("SQB", "KG7KMV-13"))
    assert r.get("k", 120).txid == 7 and ("SQB", "KG7KMV-13") in r.get("k", 120).routes
    assert r.get("k", 131) is None  # outside the window: a new transmission


def test_dupe_skips_repeat_beacon_check():
    reg = Registry(mobile_patterns=["K7ABC-9"])
    first = evaluate(VIA_SQB, T, reg, {})
    last = {"K7ABC-9": packets.LastFix(T, first.record["lat"], first.record["lon"], "SQB")}
    # The same heard_by within the window looks like a repeated beacon on the main feed...
    assert evaluate(VIA_SQB_NYE, T + 1, reg, last).reason == "duplicate"
    # ...but on the duplicates feed it is another igate's copy of the same packet.
    assert evaluate(VIA_SQB_NYE, T + 1, reg, last, dupe=True).kind == "accept"


def _register(client):
    login(client)
    for body in ({"callsign": "K7ABC-9", "kind": "mobile"},
                 {"callsign": "SQB", "kind": "digi", "lat": 43.70, "lon": -116.20},
                 {"callsign": "KG7KMV-13", "kind": "igate", "lat": 43.60, "lon": -116.30},
                 {"callsign": "KJ7NYE-10", "kind": "igate", "lat": 43.62, "lon": -116.10}):
        assert client.post("/api/admin/stations", json=body, headers=H).status_code == 200


def test_copies_count_once_per_station(client):  # noqa: F811
    _register(client)
    ing = client.ingester
    assert ing.handle_line(VIA_SQB, now=T) == "accept"
    assert ing.handle_line(DIRECT_NYE, now=T + 1, dupe=True) == "accept"
    assert ing.handle_line(VIA_SQB_NYE, now=T + 1, dupe=True) == "accept"
    # The same copy again (e.g. on both feeds) is not stored twice.
    assert ing.handle_line(VIA_SQB_NYE, now=T + 2, dupe=True) == "reject"
    assert ing.counts["dupe_accepted"] == 2 and ing.counts["dupe_reject_duplicate"] == 1
    # A duplicate doesn't become the mobile's last fix.
    assert ing.last["K7ABC-9"].heard_by == "SQB"

    q = {"start": T - 10, "end": T + 10, "cell": 500}
    cov = client.get("/api/coverage", params={**q, "color": "heard_by"}).json()
    assert cov["packets"] == 1 and len(cov["cells"]) == 1
    cell = cov["cells"][0]
    assert cell[2] == 1  # one transmission
    assert cell[5] == {"SQB": 1, "KJ7NYE-10": 1}  # heard by both, once each
    assert client.get("/api/coverage", params={**q, "heard_by": "KJ7NYE-10"}).json()["packets"] == 1

    stats = client.get("/api/stats", params=q).json()
    infra = {r["callsign"]: r for r in stats["infra"]}
    assert infra["SQB"]["heard_direct"] == 1
    assert infra["KJ7NYE-10"]["heard_direct"] == 1
    assert infra["KG7KMV-13"]["heard_direct"] == 0
    assert infra["KG7KMV-13"]["gated"] == 1 and infra["KJ7NYE-10"]["gated"] == 1
    assert stats["mobiles"] == [{"mobile": "K7ABC-9", "packets": 1, "cells": 1, "last": T + 1}]

    st = client.get("/api/admin/status").json()
    assert st["db"]["packets"] == 3 and st["db"]["dupes"] == 2


def test_dupe_before_original_shares_txid(client):  # noqa: F811
    _register(client)
    ing = client.ingester
    assert ing.handle_line(DIRECT_NYE, now=T, dupe=True) == "accept"
    assert ing.handle_line(VIA_SQB, now=T, dupe=False) == "accept"
    q = {"start": T - 10, "end": T + 10, "cell": 500}
    assert client.get("/api/coverage", params=q).json()["packets"] == 1


def test_feed_settings_api(client):  # noqa: F811
    assert client.get("/api/admin/feeds").status_code == 401
    login(client)
    f = client.get("/api/admin/feeds").json()
    assert f["settings"]["dupe_enabled"] is False and f["default_port"] == 14580

    put = lambda body: client.put("/api/admin/feeds", json=body, headers=H)
    assert put({"dupe_enabled": True}).status_code == 422  # needs server, port, login
    assert put({"server": "bad host!"}).status_code == 422
    assert put({"port": 70000}).status_code == 422
    assert put({"dupe_login": "NOT A CALL"}).status_code == 422
    body = {"server": "aprs.example.org", "port": 14580, "dupe_enabled": True,
            "dupe_server": "aprs.example.org", "dupe_port": 14501, "dupe_login": "k7abc-df"}
    r = put(body)
    assert r.status_code == 200 and r.json()["settings"]["dupe_login"] == "K7ABC-DF"
    ing = client.ingester
    assert ing.feeds.dupe_port == 14501 and ing.status()["server"] == "aprs.example.org:14580"
    assert ing._dupe_target() == "disabled"  # APRS_ENABLED=0 in tests
    # Saved in the database for the next start.
    from app import ingest
    assert ingest.load_feeds() == ing.feeds


def test_migrates_old_packets_table(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE packets (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, mobile TEXT NOT NULL,
        lat REAL NOT NULL, lon REAL NOT NULL, alt REAL, speed REAL, course INTEGER, igate TEXT NOT NULL,
        heard_by TEXT, hops INTEGER NOT NULL, chain TEXT NOT NULL, raw TEXT NOT NULL)""")
    c.execute("INSERT INTO packets(ts,mobile,lat,lon,igate,hops,chain,raw) VALUES(1,'A',1,1,'I',0,' I ','x')")
    c.commit()
    c.close()
    from app import config, db
    monkeypatch.setattr(config, "DB_PATH", path)
    db.init()
    with db.session() as c:
        row = c.execute("SELECT id, txid, dupe FROM packets").fetchone()
    assert (row["txid"], row["dupe"]) == (row["id"], 0)


def test_dupe_feed_connection(client, monkeypatch):  # noqa: F811
    """End to end over a socket: log in to a fake aprsc duplicates port and
    store the copy it sends."""
    _register(client)
    from app import config, ingest
    ing = client.ingester
    assert ing.handle_line(VIA_SQB, now=T) == "accept"
    logins = []

    async def scenario():
        async def serve(reader, writer):
            writer.write(b"# aprsc 2.1.19-g730c5c0\r\n")
            logins.append((await reader.readline()).decode().strip())
            writer.write(b"# logresp K7ABC-DF unverified, server T2TEST\r\n")
            writer.write(("dup\t" + DIRECT_NYE + "\r\n").encode())
            writer.write(b"dup\tN0BODY>APRS,qAR,X:=4336.66N/11612.40W>\r\n")  # unregistered: ignored
            await writer.drain()
            await asyncio.sleep(5)

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(config, "APRS_ENABLED", True)
        ing.feeds = ingest.FeedSettings(dupe_enabled=True, dupe_server="127.0.0.1", dupe_port=port,
                                        dupe_login="K7ABC-DF")
        monkeypatch.setattr(ingest.time, "time", lambda: T + 1)
        task = asyncio.create_task(ing.dupes.run(ing._dupe_target, ing._on_dupe_line))
        for _ in range(100):
            if ing.counts["dupe_accepted"]:
                break
            await asyncio.sleep(0.02)
        st = ing.dupes.status()
        task.cancel()
        server.close()
        return st

    st = asyncio.run(scenario())
    assert logins == ["user K7ABC-DF pass -1 vers aprs-coverage 0.1"]
    assert st["connected"] and "T2TEST" in st["server_line"]
    assert ing.counts["dupe_accepted"] == 1 and ing.counts["dupe_lines"] == 2
    infra = {r["callsign"]: r for r in
             client.get("/api/stats", params={"start": T - 10, "end": T + 10}).json()["infra"]}
    assert infra["KJ7NYE-10"]["heard_direct"] == 1 and infra["SQB"]["heard_direct"] == 1
