"""Remote node config: protocol parsing, the APRS-IS sender, and full round
trips against a fake node."""
import asyncio
import re
import time

import pytest

from app import remotecfg
from app.remotecfg import parse_reply, passcode, split_msgno
from test_api import H, client, login  # noqa: F401  (fixture)

NODE = "KJ7NYE-10"
OP = "K7SWI-CF"  # operator's sending callsign


def test_passcode():
    assert passcode("N0CALL") == 13023  # well-known reference value
    assert passcode("n0call-10") == passcode("N0CALL")  # SSID and case don't matter
    assert 0 <= passcode("KG7KMV") <= 0x7FFF


def test_check_login():
    # through the module: the client fixture reloads it, which replaces CommandError
    check = remotecfg.check_login
    assert check(" k7swi-cf ", set()) == "K7SWI-CF"
    assert check("2E0ABC", set()) == "2E0ABC"
    for bad in ("", "HELLO", "K7SWI-ABC", "K7 SWI", "123456"):
        with pytest.raises(remotecfg.CommandError):
            check(bad, set())
    with pytest.raises(remotecfg.CommandError, match="knock"):
        check("KG7KMV-10", {"KG7KMV-10"})


def test_split_msgno():
    assert split_msgno("CS OK BR=600{12") == ("CS OK BR=600", "12")
    assert split_msgno("CS RO=2{AB}C3") == ("CS RO=2", "AB")
    assert split_msgno("hello") == ("hello", "")


def test_parse_reply():
    r = parse_reply("CS RO=2 TC= SY=/# BP=WIDE1-1 DM=2 BR=600 GS=1 LA=43.6000 LO=-116.2000 EL=900.0")
    assert r["type"] == "READ" and r["fields"]["RO"] == "2" and r["fields"]["TC"] == "" and len(r["fields"]) == 10
    assert parse_reply("CS UNLOCKED 300") == {"type": "UNLOCKED", "seconds": 300}
    r = parse_reply("CS OK RO=1 BR=60 REBOOT=5")
    assert r == {"type": "OK", "fields": {"RO": "1", "BR": "60"}, "reboot": 5}
    r = parse_reply("CS ERR LOCKED_OUT 600")
    assert r["code"] == "LOCKED_OUT" and "600" in r["message"]
    assert parse_reply("CS ERR BADVALUE BR")["detail"] == "BR"


def test_validate_value():
    validate_value = remotecfg.validate_value
    assert validate_value("BR", 600) == "600"
    assert validate_value("LA", "43.61") == "43.61"
    assert validate_value("SY", "/#") == "/#"
    assert validate_value("TC", "") == ""
    for code, v in (("BR", 0), ("BR", "1.5"), ("RO", 3), ("LA", 91), ("SY", "#"), ("TC", "TOOLONGNAME"),
                    ("BP", "WIDE1-1,WIDE2-1"), ("TC", "A,B"), ("XX", 1)):
        with pytest.raises(remotecfg.CommandError):
            validate_value(code, v)


# ── the real sender, against a minimal fake APRS-IS server ───────────────────
def test_aprs_sender(monkeypatch):
    from app import config

    async def scenario():
        logins, got = [], []

        async def server(reader, writer):
            line = (await reader.readline()).decode()
            logins.append(line)
            call, code = line.split()[1], int(line.split()[3])
            ok = "verified" if code == passcode(call) else "unverified"
            writer.write(f"# logresp {call} {ok}, server TEST\r\n".encode())
            writer.write(f"{NODE}>APLRT1,qAR,KG7KMV-10::{call:<9}:CS RO=2{{7\r\n".encode())
            writer.write(f"{NODE}>APLRT1,qAR,KG7KMV-10::SOMEONE  :not for us\r\n".encode())
            await writer.drain()
            logins.append((await reader.readline()).decode())
            writer.close()

        srv = await asyncio.start_server(server, "127.0.0.1", 0)
        monkeypatch.setattr(config, "APRS_SERVER", "127.0.0.1")
        monkeypatch.setattr(config, "APRS_PORT", srv.sockets[0].getsockname()[1])
        s = remotecfg.AprsSender(lambda f, t: got.append((f, t)))
        await s.connect(OP)
        assert s.status()["login"] == OP
        s.send(f"{OP}>APZCOV,TCPIP*::{NODE:<9}:CSR{{1")
        for _ in range(50):
            if len(logins) == 2:
                break
            await asyncio.sleep(0.02)
        await s.close()
        assert logins[0].startswith(f"user {OP} pass {passcode(OP)} ") and f"filter g/{OP}" in logins[0]
        assert logins[1].strip().endswith(":CSR{1")
        assert got == [(NODE, "CS RO=2{7")]
        # the traffic is in the APRS-IS log, with the passcode masked
        from app import logbuf
        texts = [e["text"] for e in logbuf.APRS.since(None, 50) if e["tag"] == f"cfg {OP}"]
        assert f"user {OP} pass ***** vers {remotecfg.VERSION} filter g/{OP}" in texts
        assert any(t.endswith(":CSR{1") for t in texts) and str(passcode(OP)) not in " ".join(texts)

        # A login the server doesn't verify is refused up front.
        monkeypatch.setattr(remotecfg, "passcode", lambda c: 1)
        with pytest.raises(remotecfg.CommandError, match="did not verify"):
            await remotecfg.AprsSender(lambda f, t: None).connect("N0CALL")
        srv.close()

    asyncio.run(scenario())


# ── full round trips through the API, with a fake node ───────────────────────
class FakeNode:
    """Answers like the firmware: ack every copy, 60 s dedup, reply 'CS ...'."""

    def __init__(self, token="s3cret"):
        self.token, self.drop, self.enabled = token, 0, True
        self.sent, self.seen, self.unlocked_by, self.n = [], {}, None, 100
        self.cfg = {"RO": "2", "TC": "", "SY": "/#", "BP": "WIDE1-1", "DM": "2", "BR": "1800",
                    "GS": "1", "LA": "43.6000", "LO": "-116.2000", "EL": "900.0"}
        self.deliver = None  # set to RemoteConfig.handle_message

    def on_packet(self, pkt):
        self.sent.append(pkt)
        m = re.match(r"^([^>]+)>[^:]*::(.{9}):(.*)$", pkt)
        frm, to, text = m.group(1), m.group(2).strip(), m.group(3)
        if to != NODE:
            return  # our ack of the node's reply
        if self.drop:
            self.drop -= 1
            return
        body, msgno = split_msgno(text)
        out = [f"ack{msgno}"]
        now = time.monotonic()
        if now - self.seen.get((frm, body), -1e9) >= 60:  # firmware dedup TTL
            self.seen[(frm, body)] = now
            reply = self.handle(frm, body)
            if reply and self.enabled:
                self.n += 1
                out.append(f"CS {reply}{{{self.n}")
        loop = asyncio.get_running_loop()
        for i, t in enumerate(out):
            loop.call_later(0.02 * (i + 1), self.deliver, NODE, t)

    def handle(self, frm, body):
        op, _, args = body.partition(" ")
        if op == "CSR":
            codes = args.split(",") if args else list(self.cfg)
            return " ".join(f"{c}={self.cfg[c]}" for c in codes)
        if op == "CSU":
            if args == self.token:
                self.unlocked_by = frm
                return "UNLOCKED 300"
            return "ERR BADTOKEN"
        if op == "CSW":
            if self.unlocked_by != frm:  # the window belongs to one sender
                return "ERR LOCKED"
            pairs = dict(p.split("=", 1) for p in args.split(","))
            self.cfg.update(pairs)
            reboot = " REBOOT=5" if {"RO", "GS"} & set(pairs) else ""
            return "OK " + " ".join(f"{k}={v}" for k, v in pairs.items()) + reboot


class FakeSender:
    def __init__(self, node):
        self.node, self.login, self.connects = node, None, []

    async def connect(self, login):
        self.connects.append(login)
        self.login = login

    def send(self, packet):
        self.node.on_packet(packet)

    async def close(self):
        pass

    def status(self):
        return {"login": self.login, "server_line": "fake"}


@pytest.fixture()
def node(client, monkeypatch):  # noqa: F811
    from app import config, main
    monkeypatch.setattr(config, "APRS_LOGIN", "K7SWI-CV")
    monkeypatch.setattr(remotecfg, "MIN_GAP_S", 0)
    monkeypatch.setattr(remotecfg, "RETRY_S", 0.3)
    monkeypatch.setattr(remotecfg, "TIMEOUT_S", 1.5)
    fake = FakeNode()
    main.remote.sender = FakeSender(fake)
    fake.deliver = main.remote.handle_message
    login(client)
    for body in ({"callsign": NODE, "kind": "digi"}, {"callsign": "KG7KMV-10", "kind": "igate"},
                 {"callsign": "K7ABC-9", "kind": "mobile"}):
        assert client.post("/api/admin/stations", json=body, headers=H).status_code == 200
    return fake


def post(c, kind, body, node=NODE):
    return c.post(f"/api/admin/remote/{node}/{kind}", json={"login": OP, **body}, headers=H)


def test_read_unlock_write_round_trip(client, node):  # noqa: F811
    r = post(client, "read", {}, NODE.lower())
    assert r.status_code == 200, r.text
    assert r.json()["type"] == "READ" and r.json()["fields"]["BR"] == "1800"
    assert node.sent[0].startswith(f"{OP}>APZCOV,TCPIP*::{NODE:<9}:CSR{{")
    assert any(p.endswith(":ack101") for p in node.sent)  # the reply was acked

    # A second full read within 60 s is spelled differently so the node answers.
    assert post(client, "read", {}).json()["type"] == "READ"
    assert post(client, "read", {}).status_code == 429

    assert post(client, "write", {"fields": {"BR": 600}}).json()["code"] == "LOCKED"
    assert post(client, "unlock", {"token": "wrong"}).json()["code"] == "BADTOKEN"
    r = post(client, "unlock", {"token": "s3cret"})
    assert r.json() == {"type": "UNLOCKED", "seconds": 300, "raw": "CS UNLOCKED 300"}
    assert client.get("/api/admin/remote", params={"login": OP}).json()["status"]["unlocked"][NODE] > 0
    # the window belongs to OP, not to another operator callsign
    assert client.get("/api/admin/remote", params={"login": "N0CALL"}).json()["status"]["unlocked"] == {}

    r = post(client, "write", {"fields": {"BR": 600, "RO": "1"}})
    assert r.json()["type"] == "OK" and r.json()["reboot"] == 5

    state = client.get("/api/admin/remote", params={"login": OP}).json()
    n = next(x for x in state["nodes"] if x["callsign"] == NODE)
    assert n["fields"]["BR"] == "600" and n["fields"]["RO"] == "1" and n["fields"]["LA"] == "43.6000"
    assert all(x["callsign"] != "K7ABC-9" for x in state["nodes"])  # mobiles aren't configurable
    assert NODE not in state["status"]["unlocked"]  # the role change reboots the node
    log = state["log"]
    assert "s3cret" not in str(log) and any(e["command"] == "CSU ****" for e in log)
    assert {e["login"] for e in log} == {OP}


def test_retry_until_acked(client, node):  # noqa: F811
    node.drop = 1  # first transmission is lost
    r = post(client, "read", {"codes": ["BR"]})
    assert r.status_code == 200 and r.json()["fields"] == {"BR": "1800"}
    tx = [p for p in node.sent if f"::{NODE:<9}:CSR" in p]
    assert len(tx) == 2 and tx[0] == tx[1]  # same message number on the retry


def test_errors(client, node):  # noqa: F811
    node.enabled = False  # node acks but never replies
    r = post(client, "read", {})
    assert r.status_code == 504 and "acked" in r.json()["detail"]

    node.drop = 99
    r = post(client, "read", {"codes": ["RO"]})
    assert r.status_code == 504 and "No ack" in r.json()["detail"]

    assert post(client, "read", {}, "K7ABC-9").status_code == 404
    assert client.post(f"/api/admin/remote/{NODE}/read", json={"login": OP}).status_code == 403  # CSRF
    assert post(client, "write", {"fields": {"BR": 0}}).status_code == 422


def test_sending_callsign_checks(client, node):  # noqa: F811
    send = lambda call: client.post(f"/api/admin/remote/{NODE}/read", json={"login": call}, headers=H)
    assert send("").status_code == 422
    assert send("NOTACALL").status_code == 422
    # logging in as a station that is already on APRS-IS would knock it off
    assert "knock" in send("KG7KMV-10").json()["detail"]
    assert "knock" in send("K7SWI-CV").json()["detail"]  # the coverage feed's own login
    assert client.get("/api/admin/remote").status_code == 200  # no callsign needed to view
