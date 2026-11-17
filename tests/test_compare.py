"""Comparisons, device log uploads with API keys, and matching logs to packets."""
import time

from app import compare, db, devicelog
from test_api import H, client, login  # noqa: F401  (fixture)
from test_accounts import approve, other, signup

_NOW = int(time.time())
T = _NOW - _NOW % 86400 - 2 * 86400  # midnight UTC, two days ago (uploads must be recent)
POS = "!4336.66N/11612.40W>"


def tx_line(n, call="K7ABC-9", path="WIDE1-1"):
    return f"{call}>APLRT1,{path}:{POS}n{n}"


def is_line(n, call="K7ABC-9", hops="WIDE1-1", igate="KG7KMV-13"):
    return f"{call}>APLRT1,{hops},qAR,{igate}:{POS}n{n}"


def register(client, *more):
    login(client)
    for body in ({"callsign": "K7ABC-9", "kind": "mobile"},
                 {"callsign": "K7ABC-7", "kind": "mobile"},
                 {"callsign": "SQB", "kind": "digi", "lat": 43.70, "lon": -116.20},
                 {"callsign": "KG7KMV-13", "kind": "igate", "lat": 43.60, "lon": -116.30},
                 {"callsign": "KJ7NYE-10", "kind": "igate", "lat": 43.62, "lon": -116.10}, *more):
        assert client.post("/api/admin/stations", json=body, headers=H).status_code == 200


def key_for(c):
    r = c.post("/api/account/keys", json={"name": "phone"}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["key"]


# ── stats helpers ────────────────────────────────────────────────────────────
def test_stats_helpers():
    lo, hi = compare.wilson(50, 100)
    assert lo < 0.5 < hi and round(hi - lo, 2) == 0.19
    assert compare.wilson(0, 0) is None
    assert compare.mcnemar_p(10, 10) == 1.0
    assert compare.mcnemar_p(0, 12) < 0.001
    assert compare.mcnemar_p(30, 60) < 0.01  # chi-square branch
    assert compare.two_prop_p(50, 100, 50, 100) == 1.0
    assert compare.two_prop_p(90, 100, 50, 100) < 0.001
    assert compare.bucket_start(T + 5000, "day") == T
    monday = compare.bucket_start(T, "week")
    assert (monday // 86400 - 4) % 7 == 0 and T - 7 * 86400 < monday <= T
    assert compare.bucket_start(T + 3600, "day", tz_s=-7 * 3600) == T - 86400 + 7 * 3600


def test_parse_and_last_digi():
    p = devicelog.parse(devicelog.clean_line("<\xff\x01" + tx_line(1) + "\r\n"))
    assert p["key"] == "K7ABC-9" and p["is_pos"] and round(p["lat"], 3) == 43.611
    assert devicelog.parse("not a packet") is None
    assert devicelog.last_digi(["SQB*", "WIDE1*", "WIDE2-1"]) == (True, "SQB")
    assert devicelog.last_digi(["SQB", "WIDE1*"]) == (True, "SQB")
    assert devicelog.last_digi(["WIDE1*"]) == (True, None)
    assert devicelog.last_digi(["WIDE1-1"]) == (False, None)


# ── API keys and uploads ────────────────────────────────────────────────────
def test_keys_and_upload_auth(client):
    register(client)
    key = key_for(client)
    keys = client.get("/api/account/keys").json()
    assert len(keys) == 1 and keys[0]["prefix"] == key[:10] and "key" not in keys[0]
    body = {"device": "K7ABC-9", "entries": [{"type": "tx", "ts": T, "packet": tx_line(1)}]}
    anon = other(client)
    assert anon.post("/api/devicelog", json=body).status_code == 401
    assert anon.post("/api/devicelog", json=body, headers={"Authorization": "Bearer acv_wrong"}).status_code == 401
    r = anon.post("/api/devicelog", json=body, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200 and r.json()["accepted"] == 1
    # retry is harmless
    r = anon.post("/api/devicelog", json=body, headers={"Authorization": f"Bearer {key}"})
    assert r.json() == {"accepted": 0, "duplicates": 1, "ignored": 0, "errors": [], "tx": 0, "heard": 0}
    assert client.get("/api/account/keys").json()[0]["last_used"] is not None
    assert client.delete(f"/api/account/keys/{keys[0]['id']}", headers=H).status_code == 200
    assert anon.post("/api/devicelog", json=body, headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_node_owner_uploads_only_own_devices(client):
    register(client)
    alice = other(client)
    signup(alice, devices=[{"callsign": "K7ALC-9", "kind": "mobile"}])
    assert alice.post("/api/account/keys", json={}, headers=H).status_code == 403  # pending
    approve(client, "alice", "K7ALC-9")
    key = key_for(alice)
    auth = {"Authorization": f"Bearer {key}"}
    ok = alice.post("/api/devicelog", headers=auth, json={"device": "K7ALC-9", "entries": [
        {"type": "tx", "ts": T, "packet": tx_line(1, "K7ALC-9")},
        {"type": "tx", "ts": T, "packet": tx_line(1, "K7XXX-9")},
        {"type": "rx", "ts": T, "packet": "N0ONE>APRS,WIDE1-1:>hello", "rssi": -90},
        {"type": "event", "ts": T, "text": "new antenna"},
        {"type": "bogus", "ts": T},
        {"type": "tx", "ts": 5, "packet": tx_line(2, "K7ALC-9")},
    ]}).json()
    assert ok["accepted"] == 2 and ok["ignored"] == 1
    assert [e["index"] for e in ok["errors"]] == [1, 4, 5]
    r = alice.post("/api/devicelog", headers=auth, json={"device": "K7ABC-9", "entries": []})
    assert r.status_code == 404


# ── matching ─────────────────────────────────────────────────────────────────
def test_match_prefers_closest_and_each_packet_once(client):
    register(client)
    ing = client.ingester
    # Parked tracker repeats the same packet every 60 s. Beacon 2 is lost; the
    # packet heard at T+120 belongs to beacon 3, not beacon 2.
    same = f"K7ABC-9>APLRT1,WIDE1-1,qAR,KG7KMV-13:{POS}parked"
    assert ing.handle_line(same, now=T + 2) == "accept"
    assert ing.handle_line(same, now=T + 122) == "accept"
    key = key_for(client)
    r = client.post("/api/devicelog", headers={"Authorization": f"Bearer {key}"}, json={
        "device": "K7ABC-9", "entries": [
            {"type": "tx", "ts": T + i * 60, "packet": f"K7ABC-9>APLRT1,WIDE1-1:{POS}parked"} for i in range(3)]})
    assert r.json()["accepted"] == 3
    with db.session() as c:
        got = [r[0] is not None for r in c.execute(
            "SELECT txid FROM device_log WHERE kind='tx' ORDER BY ts")]
    assert got == [True, False, True]


def test_rematch_when_packets_arrive_after_upload(client):
    register(client)
    key = key_for(client)
    client.post("/api/devicelog", headers={"Authorization": f"Bearer {key}"},
                json={"device": "K7ABC-9", "entries": [{"type": "tx", "ts": T, "packet": tx_line(1)}]})
    client.ingester.handle_line(is_line(1), now=T + 3)
    with db.session() as c:
        assert c.execute("SELECT txid FROM device_log").fetchone()[0] is None
        assert devicelog.rematch_recent(c, now=T + 60) == 1
        assert c.execute("SELECT txid FROM device_log").fetchone()[0] is not None


# ── comparisons ──────────────────────────────────────────────────────────────
def test_compare_mobiles_exact_vs_estimated(client):
    register(client)
    ing = client.ingester
    # K7ABC-9: logged 10 beacons a minute apart, 7 heard.
    lost = {2, 5, 8}
    for i in range(10):
        if i not in lost:
            ing.handle_line(is_line(i), now=T + i * 60 + 2)
    key = key_for(client)
    client.post("/api/devicelog", headers={"Authorization": f"Bearer {key}"}, json={
        "device": "K7ABC-9", "entries": [{"type": "tx", "ts": T + i * 60, "packet": tx_line(i)} for i in range(10)]
        + [{"type": "rx", "ts": T + 3, "packet": tx_line(0, path="SQB*,WIDE1*"), "rssi": -95, "snr": 6},
           {"type": "rx", "ts": T + 63, "packet": tx_line(1, path="SQB*,WIDE1*"), "rssi": -97, "snr": 4},
           {"type": "rx", "ts": T + 10, "packet": "SQB>APLRG1:!4342.00N/11612.00W#digi", "rssi": -80}]})
    # K7ABC-7: no log, beacons every 60 s, every 4th lost.
    for i in range(20):
        if i % 4 != 3:
            ing.handle_line(is_line(i, "K7ABC-7"), now=T + i * 60 + 5)
    r = client.get("/api/compare/mobiles", params={"a": "K7ABC-9", "b": "k7abc-7", "start": T - 10,
                                                   "end": T + 3600, "bucket": "day"})
    assert r.status_code == 200, r.text
    d = r.json()
    a, b = d["a"]["total"], d["b"]["total"]
    assert (a["method"], a["sent"], a["heard"]) == ("exact", 10, 7)
    assert b["method"] == "estimated" and d["b"]["interval_s"] == 60
    assert (b["sent"], b["heard"]) == (19, 15)  # the last loss is after the last packet heard
    assert a["direct_pct"] == 1.0
    echo = d["a"]["rx"]["echoes"][0]
    assert echo["via"] == "SQB" and echo["n"] == 2 and echo["rssi"] == -96 and echo["echo_rate"] == 0.2
    assert d["a"]["rx"]["beacons"][0]["via"] == "SQB"
    assert 0 <= d["p_value"] <= 1
    # a fixed interval can be given instead of estimated
    d = client.get("/api/compare/mobiles", params={"a": "K7ABC-9", "b": "K7ABC-7", "start": T - 10,
                                                   "end": T + 3600, "interval": 30}).json()
    assert d["b"]["total"]["sent"] == 15 + 10 * 1 + 4 * 3  # gaps of 60 s count one missed, 120 s three
    assert client.get("/api/compare/mobiles", params={"a": "K7ABC-9", "b": "K7ABC-9"}).status_code == 422


def test_compare_mobiles_together(client):
    register(client)
    ing = client.ingester
    far = "!4337.50N/11612.40W>"  # 1.5 km away, a plausible drive
    for i in range(10):
        ing.handle_line(is_line(i), now=T + i * 60)
        # K7ABC-7 rides along for the first 5 minutes, then goes elsewhere
        pos = POS if i < 5 else far
        ing.handle_line(f"K7ABC-7>APLRT1,WIDE1-1,qAR,KG7KMV-13:{pos}m{i}", now=T + i * 60 + 1)
    d = client.get("/api/compare/mobiles", params={"a": "K7ABC-9", "b": "K7ABC-7", "start": T - 10,
                                                   "end": T + 3600, "together": "true"}).json()
    assert d["a"]["total"]["packets"] == 5 and d["b"]["total"]["packets"] == 5


def test_compare_nodes(client):
    register(client)
    ing = client.ingester
    # 10 transmissions: KG7KMV-13 hears all, KJ7NYE-10 (duplicates feed) hears 4.
    for i in range(10):
        ing.handle_line(is_line(i), now=T + i * 60)
        if i < 4:
            ing.handle_line(is_line(i, igate="KJ7NYE-10"), now=T + i * 60 + 1, dupe=True)
    d = client.get("/api/compare/nodes", params={"a": "KG7KMV-13", "b": "KJ7NYE-10", "start": T - 10,
                                                 "end": T + 3600}).json()
    t = d["total"]
    assert (t["total"], t["both"], t["a_only"], t["b_only"]) == (10, 4, 6, 0)
    assert t["a_pct"] == 1.0 and t["b_pct"] == 0.4
    assert d["dupes_feed"] and d["p_value"] < 0.05
    band = next(b for b in d["bands"]["KJ7NYE-10"] if b["n"])
    assert band["n"] == 10 and band["heard"] == 4


def test_events(client):
    register(client)
    r = client.post("/api/events", json={"device": "SQB", "ts": T, "text": "swapped antenna"}, headers=H)
    assert r.status_code == 200
    d = client.get("/api/compare/nodes", params={"a": "SQB", "b": "KG7KMV-13", "start": T - 10, "end": T + 10}).json()
    assert [e["text"] for e in d["events"]] == ["swapped antenna"]
    assert client.post("/api/events", json={"device": "NOPE", "ts": T, "text": "x"}, headers=H).status_code == 404
    assert client.delete(f"/api/events/{d['events'][0]['id']}", headers=H).status_code == 200


def test_admin_logs_list_device_actions(client):
    from app import logbuf
    logbuf.DEVICES._buf.clear()  # the ring outlives each test's app
    register(client)
    key = key_for(client)
    anon = other(client)
    auth = {"Authorization": f"Bearer {key}"}
    client.ingester.handle_line(is_line(1), now=T + 2)
    anon.post("/api/devicelog", headers=auth, json={"device": "K7ABC-9", "app": "test/1", "entries": [
        {"type": "tx", "ts": T, "packet": tx_line(1)},
        {"type": "rx", "ts": T + 3, "packet": tx_line(1, path="SQB*,WIDE1*"), "rssi": -97, "snr": 6.5},
        {"type": "bogus", "ts": T}]})
    anon.post("/api/devicelog", headers={"Authorization": "Bearer acv_nope"}, json={"device": "K7ABC-9", "entries": []})
    anon.post("/api/devicelog", headers=auth, json={"device": "N0PE-9", "entries": []})
    client.post("/api/events", json={"device": "SQB", "ts": T, "text": "swapped antenna"}, headers=H)
    lines = [(e["level"], e["tag"], e["text"], e["note"])
             for e in client.get("/api/admin/logs/devices").json()["entries"]]
    assert lines[0][0] == "key" and "created upload key" in lines[0][2]
    up = next(x for x in lines if x[0] == "upload")
    assert up[1] == "admin K7ABC-9" and "2 new" in up[2] and "1 of 1 new transmissions heard" in up[2]
    assert up[3].startswith("test/1 | #2: type must be")
    refused = [x for x in lines if x[0] == "refused"]
    assert "unknown or revoked key acv_nope" in refused[0][2] and "N0PE-9 is not an approved device" in refused[1][2]
    assert lines[-1][0] == "note" and "swapped antenna" in lines[-1][2]
    stored = client.get("/api/admin/logs/devicelog").json()["entries"]
    assert [e["level"] for e in stored] == ["tx", "rx"]
    assert stored[0]["note"].startswith("heard on APRS-IS")
    assert stored[1]["note"] == "repeated by SQB, RSSI -97 dBm, SNR 6.5 dB"
    assert logbuf.DEVICES.since(None, 1)  # in-memory ring


def test_failures_map_layer(client):
    register(client)
    ing = client.ingester
    # 5 beacons logged from one spot, 2 heard
    for i in (0, 3):
        ing.handle_line(is_line(i), now=T + i * 60 + 2)
    key = key_for(client)
    client.post("/api/devicelog", headers={"Authorization": f"Bearer {key}"}, json={
        "device": "K7ABC-9", "entries": [{"type": "tx", "ts": T + i * 60, "packet": tx_line(i)} for i in range(5)]})
    d = client.get("/api/failures", params={"start": T - 10, "end": T + 3600, "cell": 1000}).json()
    assert d["failed"] == 3 and len(d["cells"]) == 1
    x, y, failed, sent, last = d["cells"][0]
    assert (failed, sent, last) == (3, 5, T + 4 * 60)
    cov = client.get("/api/coverage", params={"start": T - 10, "end": T + 3600, "cell": 1000}).json()
    assert [c[:2] for c in cov["cells"]] == [[x, y]]  # same grid as the coverage squares
    assert client.get("/api/failures", params={"start": T - 10, "end": T + 3600, "mobiles": "K7ABC-7"}).json()["cells"] == []
    # a beacon just sent isn't counted as lost until its packet has had time to arrive
    now = int(time.time())
    client.post("/api/devicelog", headers={"Authorization": f"Bearer {key}"}, json={
        "device": "K7ABC-9", "entries": [{"type": "tx", "ts": now, "packet": tx_line(99)}]})
    assert client.get("/api/failures", params={"start": now - 10}).json()["cells"] == []


def test_red_is_reserved_for_lost_beacons(client):
    login(client)
    for red in ("#ff0000", "#d1242f", "#e5534b"):
        r = client.post("/api/admin/stations", json={"callsign": "K7RED-10", "kind": "igate", "color": red}, headers=H)
        assert r.status_code == 422 and "reserved" in r.text
    for ok in ("#f0883e", "#e85aad", "#2f81f7"):
        r = client.post("/api/admin/stations", json={"callsign": f"K7OK{ok[1:3].upper()}", "kind": "igate", "color": ok}, headers=H)
        assert r.status_code == 200, r.text


def test_explain_unheard(client):
    register(client, {"callsign": "KG7KMV-15", "kind": "igate"})
    ing = client.ingester
    with db.session() as c:
        def why(device, ts, info, now=None):
            return devicelog.explain_unheard(c, device, ts, info, now=now or T + 86400)
        assert why("K7ABC-9", T, "x", now=T + 30).startswith("not heard yet")
        assert "none has ever been stored" in why("K7ABC-9", T, "x")
        assert "no active, approved mobile registration matches K7ZZZ-9" in why("K7ZZZ-9", T, "x")
        assert "KG7KMV-15 is registered as an igate/digi" in why("KG7KMV-15", T, "x")
    ing.handle_line(is_line(1), now=T + 3)
    with db.session() as c:
        assert "has different text" in devicelog.explain_unheard(c, "K7ABC-9", T, f"{POS}other", now=T + 86400)
        assert "matched to another logged beacon" in devicelog.explain_unheard(c, "K7ABC-9", T, f"{POS}n1", now=T + 86400)
        assert "the last one was stored 16 min before it" in devicelog.explain_unheard(c, "K7ABC-9", T + 1000, "x", now=T + 86400)
    # a packet APRS-IS delivered but the quality checks threw out
    ing.handle_line(f"K7ABC-9>APLRT1,WIDE1-1,qAR,KG7KMV-13:!4500.00N/11000.00W>far", now=T + 2000)
    with db.session() as c:
        assert "rejected as speed_jump" in devicelog.explain_unheard(c, "K7ABC-9", T + 1998, "x", now=T + 86400)
