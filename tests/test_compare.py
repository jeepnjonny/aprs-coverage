"""Comparisons, device log uploads with API keys, and matching logs to packets."""
import time

from app import compare, db, devicelog
from conftest import H, login
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


def test_newly_missed_announces_each_lost_beacon_once(client):
    register(client)
    key = key_for(client)
    client.post("/api/devicelog", headers={"Authorization": f"Bearer {key}"}, json={
        "device": "K7ABC-9", "entries": [{"type": "tx", "ts": T + i * 60, "packet": tx_line(i)} for i in range(3)]})
    client.ingester.handle_line(is_line(0), now=T + 2)
    client.ingester.handle_line(is_line(2), now=T + 122)  # arrived after the upload: re-matched first
    seen: set[int] = set()
    with db.session() as c:
        assert devicelog.newly_missed(c, seen, now=T + 60) == []  # nothing settled yet
        got = devicelog.newly_missed(c, seen, now=T + 300)
        assert [(m["mobile"], m["ts"]) for m in got] == [("K7ABC-9", T + 60)]
        assert round(got[0]["lat"], 2) == 43.61
        assert devicelog.newly_missed(c, seen, now=T + 400) == []  # announced once
        assert devicelog.newly_missed(c, seen, now=T + 3600) == [] and not seen  # too old: pruned


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
    assert b["method"] == "estimated" and d["b"]["beacon"]["stop_s"] == 60  # parked, beaconing steadily
    assert (b["sent"], b["heard"]) == (19, 15)  # the last loss is after the last packet heard
    assert a["direct_pct"] == 1.0
    echo = d["a"]["rx"]["echoes"][0]
    assert echo["via"] == "SQB" and echo["n"] == 2 and echo["rssi"] == -96 and echo["echo_rate"] == 0.2
    assert d["a"]["rx"]["beacons"][0]["via"] == "SQB"
    assert 0 <= d["p_value"] <= 1
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
    ing.handle_line("K7ABC-9>APLRT1,WIDE1-1,qAR,KG7KMV-13:!4500.00N/11000.00W>far", now=T + 2000)
    with db.session() as c:
        assert "rejected as speed_jump" in devicelog.explain_unheard(c, "K7ABC-9", T + 1998, "x", now=T + 86400)


def test_fast_identical_beacons_are_not_counted_lost(client):
    """Parked, beaconing every 10 s: APRS-IS passes the first copy and drops
    identical ones for 30 s, so those repeats are neither heard nor lost."""
    register(client)
    same = f"{POS}parked"
    client.ingester.handle_line(f"K7ABC-9>APLRT1,WIDE1-1,qAR,KG7KMV-13:{same}", now=T + 2)
    client.ingester.handle_line(f"K7ABC-9>APLRT1,WIDE1-1,qAR,KG7KMV-13:{same}", now=T + 42)
    key = key_for(client)
    client.post("/api/devicelog", headers={"Authorization": f"Bearer {key}"}, json={
        "device": "K7ABC-9", "entries": [{"type": "tx", "ts": T + i * 10, "packet": f"K7ABC-9>APLRT1,WIDE1-1:{same}"}
                                         for i in range(8)]})  # T .. T+70
    with db.session() as c:
        rows = [(r["ts"] - T, r["txid"] is not None, r["repeat_of"] is not None)
                for r in c.execute("SELECT * FROM device_log ORDER BY ts")]
    # T+0 and T+40 heard; T+10..30 and T+50..70 repeats of those (dropped by APRS-IS)
    assert rows == [(0, True, False), (10, False, True), (20, False, True), (30, False, True),
                    (40, True, False), (50, False, True), (60, False, True), (70, False, True)]
    d = client.get("/api/compare/mobiles", params={"a": "K7ABC-9", "b": "K7ABC-7", "start": T - 10, "end": T + 3600}).json()
    assert (d["a"]["total"]["sent"], d["a"]["total"]["heard"]) == (2, 2)
    assert client.get("/api/failures", params={"start": T - 10, "end": T + 3600}).json()["cells"] == []
    log = client.get("/api/admin/logs/devicelog").json()["entries"]
    assert "dropped it as a duplicate" in log[1]["note"]


# ── reliability view ─────────────────────────────────────────────────────────
def _pos(lat, lon):
    la = f"{int(lat):02d}{(lat % 1) * 60:05.2f}N"
    lo = f"{int(-lon):03d}{(-lon % 1) * 60:05.2f}W"
    return f"!{la}/{lo}>"


def test_reliability_estimates_gaps_along_the_road(client):
    register(client)
    ing = client.ingester
    # K7ABC-7 drives east at 60 s beacons, ~1 km per beacon; beacons 5-8 lost.
    for i in range(12):
        if i not in (5, 6, 7, 8):
            ing.handle_line(f"K7ABC-7>APLRT1,WIDE1-1,qAR,KG7KMV-13:{_pos(43.6, -116.5 + i * 0.0124)}b{i}", now=T + i * 60)
    d = client.get("/api/reliability", params={"start": T - 10, "end": T + 3600, "cell": 1000}).json()
    assert (d["sent"], d["heard"], d["exact"]) == (12, 8, 0)
    assert d["mobiles"]["K7ABC-7"]["beacon"]["kind"] == "fixed" and d["mobiles"]["K7ABC-7"]["beacon"]["fast_s"] == 60
    from app import grid
    dlat, dlon = grid.cell_deg(1000)
    lost = {(x, y) for x, y, h, n, *_ in d["cells"] if h == 0}
    # the four lost beacons land between the 5th and 10th fixes
    xs = sorted(x for x, y in lost)
    assert len(xs) >= 3 and min(xs) > int((-116.5 + 4 * 0.0124) // dlon) and max(xs) < int((-116.5 + 9 * 0.0124) // dlon) + 1
    # exact only: this tracker has no log, so nothing
    assert client.get("/api/reliability", params={"start": T - 10, "end": T + 3600, "estimates": "false"}).json()["sent"] == 0


def test_reliability_parked_gap_is_not_loss(client):
    register(client)
    ing = client.ingester
    for i in range(6):  # driving, 60 s
        ing.handle_line(f"K7ABC-7>APLRT1,WIDE1-1,qAR,KG7KMV-13:{_pos(43.6, -116.5 + i * 0.0124)}b{i}", now=T + i * 60)
    # parked 50 minutes at the last spot, then heard again from the same spot
    ing.handle_line(f"K7ABC-7>APLRT1,WIDE1-1,qAR,KG7KMV-13:{_pos(43.6, -116.5 + 5 * 0.0124)}later", now=T + 3300)
    d = client.get("/api/reliability", params={"start": T - 10, "end": T + 7200}).json()
    assert (d["sent"], d["heard"]) == (7, 7)


def test_reliability_uses_logs_and_station_filter(client):
    register(client)
    ing = client.ingester
    lost = {2, 5, 8}
    for i in range(10):
        if i not in lost:
            igate = "KJ7NYE-10" if i < 4 else "KG7KMV-13"
            ing.handle_line(is_line(i, igate=igate), now=T + i * 60 + 2)
    key = key_for(client)
    client.post("/api/devicelog", headers={"Authorization": f"Bearer {key}"}, json={
        "device": "K7ABC-9", "entries": [{"type": "tx", "ts": T + i * 60, "packet": tx_line(i)} for i in range(10)]})
    d = client.get("/api/reliability", params={"start": T - 10, "end": T + 3600}).json()
    assert (d["sent"], d["heard"], d["exact"]) == (10, 7, 10)  # logged: no double counting of heard packets
    only = client.get("/api/reliability", params={"start": T - 10, "end": T + 3600, "heard_by": "KJ7NYE-10"}).json()
    assert (only["sent"], only["heard"]) == (10, 3)  # what KJ7NYE-10 alone would have covered


def test_reliability_same_second_packets(client):
    """Two packets from one mobile stored in the same second at different
    places (a digi's delayed copy next to a fresh beacon) used to divide by zero."""
    register(client)
    ing = client.ingester
    for i in range(8):
        ing.handle_line(f"K7ABC-7>APLRT1,WIDE1-1,qAR,KG7KMV-13:{_pos(43.6, -116.5 + i * 0.0124)}b{i}", now=T + i * 60)
    with db.session() as c:  # the ingester's speed check would reject it, so store it directly
        c.execute("INSERT INTO packets(ts,mobile,lat,lon,igate,heard_by,hops,chain,raw) "
                  "VALUES(?,?,?,?,?,?,?,?,?)", (T + 7 * 60, "K7ABC-7", 43.6, -116.5 + 2 * 0.0124, "KG7KMV-13",
                                                "SQB", 1, " SQB KG7KMV-13 ", "K7ABC-7>APLRT1,SQB*:late"))
        c.execute("UPDATE packets SET txid=id WHERE txid IS NULL")
    r = client.get("/api/reliability", params={"start": T - 10, "end": T + 3600})
    assert r.status_code == 200 and r.json()["sent"] == 9


def test_reliability_counts_packet_before_its_log_arrives(client):
    """The app uploads about once a minute: a packet heard after the newest
    logged beacon counts straight away, then once (exactly) after the upload."""
    register(client)
    ing = client.ingester
    key = key_for(client)
    auth = {"Authorization": f"Bearer {key}"}
    client.post("/api/devicelog", headers=auth, json={"device": "K7ABC-9", "entries": [
        {"type": "tx", "ts": T + i * 30, "packet": tx_line(i)} for i in range(4)]})  # all lost
    ing.handle_line(is_line(4), now=T + 4 * 30 + 2)  # heard; its log entry not uploaded yet
    d = client.get("/api/reliability", params={"start": T - 10, "end": T + 3600}).json()
    assert (d["sent"], d["heard"], d["exact"]) == (5, 1, 4)
    client.post("/api/devicelog", headers=auth, json={"device": "K7ABC-9", "entries": [
        {"type": "tx", "ts": T + 4 * 30, "packet": tx_line(4)}]})
    d = client.get("/api/reliability", params={"start": T - 10, "end": T + 3600}).json()
    assert (d["sent"], d["heard"], d["exact"]) == (5, 1, 5)


# ── before / after a change ──────────────────────────────────────────────────
def _drive(ing, call, t0, keep, lon0=-116.5, n=20, igate="KG7KMV-13", lat=43.6):
    """Drive east, 60 s beacons ~1 km apart; keep(i) says which are heard."""
    for i in range(n):
        if keep(i):
            ing.handle_line(f"{call}>APLRT1,WIDE1-1,qAR,{igate}:{_pos(lat, lon0 + i * 0.0124)}t{t0}i{i}", now=t0 + i * 60)


# These put the change three days back (T is two days back), so the "after"
# drives, which run to 2+ hours past day 2, are in the past at any time of day.
def test_change_mobile_same_route(client):
    register(client)
    ing = client.ingester
    at = T - 86400
    for d in range(1, 4):  # before: 3 days, every other beacon lost from i=6 on
        _drive(ing, "K7ABC-7", at - d * 86400, lambda i: i < 6 or i % 2 == 0)
    for d in range(0, 3):  # after: everything heard, plus a detour on another road
        _drive(ing, "K7ABC-7", at + 3600 + d * 86400, lambda i: True)
        _drive(ing, "K7ABC-7", at + 7200 + d * 86400, lambda i: True, lat=44.2)
    d = client.get("/api/compare/change", params={"station": "K7ABC-7", "at": at, "days": 7}).json()
    b, a = d["before"], d["after"]
    assert d["station_kind"] == "mobile" and a["pct"] == 1.0 and b["pct"] < 0.9
    assert d["p_value"] < 0.05
    assert d["squares"]["after"] > d["squares"]["both"]  # the detour isn't counted...
    # ...only the route: 20 beacons a day for 3 days, less the last one (never
    # placed before the change: the beacon lost after the last packet heard)
    assert a["sent"] == 57
    assert d["better"] and not d["worse"] and d["better"][0]["delta"] > 0
    assert {r["side"] for r in d["days_series"]} == {"before", "after"}
    wide = client.get("/api/compare/change", params={"station": "K7ABC-7", "at": at, "same_route": "false"}).json()
    # with the detour and the whole route, plus the silent 40-minute drive between
    # the two roads, which counts as lost beacons along the way
    assert wide["after"]["sent"] == 120 + 3 * 40
    assert client.get("/api/compare/change", params={"station": "K7ABC-7", "at": 9_999_999_999}).status_code == 422


def test_change_node(client):
    register(client)
    ing = client.ingester
    at = T - 86400
    # before: KJ7NYE-10 heard a third; after its antenna change, two thirds
    for day in (-2, -1, 1, 2):  # in time order, as the ingester sees them
        tag = f"{'b' if day < 0 else 'a'}{abs(day)}"
        for i in range(30):
            t = at + day * 86400 + i * 60
            line = is_line(i, igate="KG7KMV-13").replace("n%d" % i, f"{tag}n{i}")
            ing.handle_line(line, now=t)
            if (i % 3 == 0) == (day < 0):
                ing.handle_line(line.replace("KG7KMV-13", "KJ7NYE-10"), now=t + 1, dupe=True)
    client.post("/api/events", json={"device": "KJ7NYE-10", "ts": at, "text": "new antenna"}, headers=H)
    d = client.get("/api/compare/change", params={"station": "KJ7NYE-10", "at": at, "days": 7}).json()
    assert d["station_kind"] == "node"
    assert (d["before"]["heard"], d["before"]["sent"]) == (20, 60)
    assert (d["after"]["heard"], d["after"]["sent"]) == (40, 60)
    assert [e["text"] for e in d["events"]] == ["new antenna"]


# ── log files copied off a tracker's SD card ────────────────────────────────
def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_parse_log_file():
    from app.devicelog import UploadError, parse_log_file, parse_time
    assert parse_time("1763251200") == parse_time("2025-11-16T00:00:00Z") == 1763251200
    assert parse_time("2025-11-16 00:00:00") == parse_time("2025-11-15T17:00:00-07:00") == 1763251200
    assert parse_time("yesterday") is None
    text = ("﻿# LoRanger V2 log\n"
            "UTC,Type,Packet\n"
            f"{iso(T)},TX,K7ABC-9>APLRT1,WIDE1-1:{POS}a, b\n"             # unquoted commas kept
            f'{T + 60},sent,"K7ABC-9>APLRT1,WIDE1-1:{POS}c"\n'
            f'{T + 65},rx,"K7ABC-9>APLRT1,SQB*,WIDE1*:{POS}a, b"\n'        # not a transmission: skipped
            f"{T + 70},DIGI,K7XYZ>APRS,K7ABC-9*:>hi\n"                     # skipped
            "\n"
            f"soon,tx,K7ABC-9>APLRT1:{POS}\n")
    entries, errors, skipped, untimed = parse_log_file(text)
    assert [(e["type"], e["ts"], e["line"]) for e in entries] == [("tx", T, 3), ("tx", T + 60, 4)]
    assert entries[0]["packet"] == f"K7ABC-9>APLRT1,WIDE1-1:{POS}a, b"
    assert skipped == 2 and untimed == 0 and errors == [{"line": 8, "error": "can't read the time 'soon'"}]
    # no type column: every row is a transmission
    entries, _, _, _ = parse_log_file(f"timestamp,raw\n{T},{tx_line(1)}\n")
    assert entries[0]["type"] == "tx" and entries[0]["packet"] == tx_line(1)
    for bad in ("", "# only a comment\n", f"{T},{tx_line(1)}\n", "when,what\n"):
        try:
            parse_log_file(bad)
            raise AssertionError(bad)
        except UploadError as e:
            assert e.status == 422


def test_log_file_upload(client):
    register(client)
    ing = client.ingester
    assert ing.handle_line(is_line(1), now=T + 3) == "accept"   # beacon 1 heard, 2 lost
    csv_text = "utc,type,packet\n" + "".join(f"{iso(T + i * 60)},tx,{tx_line(i + 1)}\n" for i in range(2))
    body = {"device": "k7abc-9", "name": "TX0001.CSV", "text": csv_text}
    assert client.post("/api/account/devicelog-file", json=body).status_code == 403   # no X-Requested-With
    r = client.post("/api/account/devicelog-file", json=body, headers=H).json()
    assert (r["rows"], r["accepted"], r["tx"], r["heard"], r["first"], r["last"]) == (2, 2, 2, 1, T, T + 60)
    r = client.post("/api/account/devicelog-file", json=body, headers=H).json()
    assert (r["accepted"], r["duplicates"], r["rejected"]) == (0, 2, 0)                # repeat is harmless
    with db.session() as c:
        assert [x[0] is not None for x in c.execute("SELECT txid FROM device_log ORDER BY ts")] == [True, False]
    # the reliability view counts them as exact
    rel = client.get("/api/reliability", params={"start": T - 600, "end": T + 600, "cell": 500}).json()
    assert rel["exact"] == 2 and rel["sent"] == 2 and rel["heard"] == 1

    # errors carry file line numbers; an entry upload() refuses counts as rejected
    long = tx_line(9) + "x" * 600
    bad = f"utc,packet\n{T},{tx_line(1, 'K7XXX-9')}\nnope,{tx_line(2)}\n{T + 9},{long}\n"
    r = client.post("/api/account/devicelog-file", json={"device": "K7ABC-9", "text": bad}, headers=H).json()
    assert (r["rejected"], r["ignored"]) == (2, 1) and [e["line"] for e in r["errors"]] == [3, 4]
    assert "one TNC2 line" in r["errors"][1]["error"]

    # big files go in batches of MAX_ENTRIES, in time order
    rows = [f"{T + 3600 + i * 5},{tx_line(i, 'K7ABC-7')}" for i in range(devicelog.MAX_ENTRIES + 5)][::-1]
    r = client.post("/api/account/devicelog-file", headers=H,
                    json={"device": "K7ABC-7", "text": "ts,packet\n" + "\n".join(rows)}).json()
    assert r["accepted"] == devicelog.MAX_ENTRIES + 5 and r["first"] == T + 3600

    for dev, status in (("KG7KMV-13", 422), ("N0PE-9", 404), ("", 422)):
        assert client.post("/api/account/devicelog-file", headers=H,
                           json={"device": dev, "text": csv_text}).status_code == status
    assert client.post("/api/account/devicelog-file", headers=H,
                       json={"device": "K7ABC-9", "text": "x" * (devicelog.MAX_FILE_BYTES + 1)}).status_code == 413
    assert client.get("/api/account/log-devices").json() == ["K7ABC-7", "K7ABC-9"]


def test_log_file_owner_only(client):
    register(client)
    alice = other(client)
    signup(alice, devices=[{"callsign": "K7ALC-*", "kind": "mobile"}])
    assert alice.get("/api/account/log-devices").status_code == 403                    # pending
    approve(client, "alice", "K7ALC-*")
    assert alice.get("/api/account/log-devices").json() == ["K7ALC-*"]
    text = f"utc,packet\n{T},{tx_line(1, 'K7ALC-9')}\n"
    assert alice.post("/api/account/devicelog-file", headers=H,
                      json={"device": "K7ABC-9", "text": text}).status_code == 404
    r = alice.post("/api/account/devicelog-file", headers=H, json={"device": "K7ALC-9", "text": text})
    assert r.status_code == 200 and r.json()["accepted"] == 1


# The LoRanger V2's SD log, as its firmware docs give it (v2-sd-logging.md):
# /log/<session>/tx.csv and rx.csv, packet RFC-4180 quoted, utc empty without a fix.
V2_TX = ('utc,millis,packet\n'
         '2026-11-22T15:04:37Z,60000,"KJ7NYE-4>APLRT1,WIDE1-1:=/8KS:1#-J>!!E/A=002532Loranger V2 Test Box Bat=8.03V"\n'
         ',1000,"KJ7NYE-4>APLRT1,WIDE1-1:>status, ""quoted"""\n')
V2_RX = 'utc,millis,rssi,snr,packet\n2026-11-22T15:05:12Z,95000,-98.0,8.5,"KJ7NYE-10>APRS:>Testing"\n'


def test_parse_loranger_v2_files():
    from app.devicelog import UploadError, parse_log_file
    entries, errors, skipped, untimed = parse_log_file(V2_TX)
    assert (errors, skipped, untimed) == ([], 0, 1)               # no GPS fix yet: skipped, not an error
    assert entries == [{"type": "tx", "ts": 1795359877, "line": 2,
                        "packet": "KJ7NYE-4>APLRT1,WIDE1-1:=/8KS:1#-J>!!E/A=002532Loranger V2 Test Box Bat=8.03V"}]
    try:
        parse_log_file(V2_RX)                                       # receive logs aren't taken
        raise AssertionError("rx.csv accepted")
    except UploadError as e:
        assert e.status == 422 and "receive log" in str(e)
    _, _, _, untimed = parse_log_file('utc,millis,packet\n,5,"K7ABC-9>APRS:>a ""b"" c"\n')
    assert untimed == 1
    entries, _, _, _ = parse_log_file('utc,millis,packet\n2026-11-22T15:04:37Z,5,"K7ABC-9>APRS:>a ""b"", c"\n')
    assert entries[0]["packet"] == 'K7ABC-9>APRS:>a "b", c'
    for chan in ("utc,millis,event,detail\n,1000,wake,timer\n",                     # power.csv
                  "millis,utc,source,event,detail\n45120,2026-11-22T15:03:42Z,cli,x,y\n",  # events.csv
                  "millis,utc,vbat,vbus,vsys,ibat_ma,tdie_c,state,term,ts,faults,activity\n",  # battery.csv
                  "millis,utc,lat,lon,alt_m,speed_kph,course,sats,hdop,activity\n"):    # gps.csv
        try:
            parse_log_file(chan)
            raise AssertionError(chan)
        except UploadError as e:
            assert e.status == 422 and "no packets" in str(e)


def test_loranger_v2_session_upload(client):
    register(client, {"callsign": "KJ7NYE-4", "kind": "mobile"})
    ing = client.ingester
    sent = [f"KJ7NYE-4>APLRT1,WIDE1-1:{POS}v2 {i}" for i in range(3)]
    assert ing.handle_line(sent[0].replace("WIDE1-1:", "SQB*,WIDE1*,qAR,KG7KMV-13:"), now=T + 4) == "accept"
    tx = "utc,millis,packet\n" + "".join(f'{iso(T + i * 60)},{i * 60000},"{p}"\n' for i, p in enumerate(sent))
    tx += ',999,"KJ7NYE-4>APLRT1:>no fix yet"\n'
    tx += f'{iso(T + 30)},30000,"K7XYZ-9>APRS,KJ7NYE-4*:>relayed for someone else"\n'   # it digipeated this
    r = client.post("/api/account/devicelog-file", headers=H,
                    json={"device": "KJ7NYE-4", "name": "20261122-150000/tx.csv", "text": tx}).json()
    assert (r["rows"], r["accepted"], r["untimed"], r["ignored"], r["rejected"]) == (5, 3, 1, 1, 0)
    assert (r["tx"], r["heard"]) == (3, 1)
    r = client.post("/api/account/devicelog-file", headers=H,
                    json={"device": "KJ7NYE-4", "name": "20261122-150000/rx.csv", "text": V2_RX})
    assert r.status_code == 422 and "Upload the tracker's tx.csv" in r.json()["detail"]
    # another tracker's card is refused, naming whose it is
    r = client.post("/api/account/devicelog-file", headers=H, json={"device": "K7ABC-9", "text": tx})
    assert r.status_code == 422 and "from KJ7NYE-4 (3), K7XYZ-9 (1)" in r.json()["detail"]
    # a channel without packets
    r = client.post("/api/account/devicelog-file", headers=H,
                    json={"device": "KJ7NYE-4", "name": "power.csv", "text": "utc,millis,event,detail\n"})
    assert r.status_code == 422 and "Upload a tracker's tx.csv" in r.json()["detail"]
