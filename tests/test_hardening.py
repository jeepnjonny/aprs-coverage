"""Tests for the feed thread-safety, reconnect backoff, remote-config input
and login lockout fixes."""
import asyncio
import threading

import pytest
from fastapi import HTTPException

from app import auth, ingest, remotecfg


# ── remote config: no line injection into the APRS-IS stream ────────────────
@pytest.mark.parametrize("value", ["abc\r\nK7SWI>APRS:>forged", "abc\nx", "tab\there", "café"])
def test_validate_value_rejects_non_printable(value):
    with pytest.raises(remotecfg.CommandError):
        remotecfg.validate_value("TC", value)


def test_unlock_rejects_line_breaks():
    async def go():
        cfg = remotecfg.RemoteConfig.__new__(remotecfg.RemoteConfig)
        with pytest.raises(remotecfg.CommandError):
            await cfg.unlock("K7ABC-10", "K7ABC-1", "x\nK7ABC>APRS:>forged")
    asyncio.run(go())


def test_sender_refuses_line_breaks():
    s = remotecfg.AprsSender(lambda f, t: None)

    class W:
        def is_closing(self):
            return False

        def write(self, b):
            raise AssertionError("should not be written")
    s._writer = W()
    with pytest.raises(remotecfg.CommandError):
        s.send("K7ABC>APRS::K7ABC-1  :hi\r\nK7ABC>APRS:>forged")


# ── login lockout: one address can't lock someone else out ─────────────────
def test_one_ip_cannot_lock_out_a_user():
    auth._fails.clear()
    for _ in range(auth.MAX_FAILS):
        auth.record_failure("10.0.0.1", "u:admin")
    with pytest.raises(HTTPException):
        auth.check_rate_limit("10.0.0.1", "u:admin")  # the attacker is blocked
    auth.check_rate_limit("10.0.0.2", "u:admin")      # the real user is not


def test_spread_out_guessing_still_hits_the_username_cap():
    auth._fails.clear()
    for i in range(auth.MAX_FAILS_PER_USER):
        auth.record_failure(f"10.0.{i // 250}.{i % 250}", "u:admin")
    with pytest.raises(HTTPException):
        auth.check_rate_limit("10.9.9.9", "u:admin")


def test_expired_failure_keys_are_dropped(monkeypatch):
    auth._fails.clear()
    auth.record_failure("10.0.0.1", "u:someone")
    monkeypatch.setattr(auth.time, "time", lambda: 10**10)
    auth.check_rate_limit("10.0.0.1", "u:someone")
    assert auth._fails == {}


# ── feeds: control calls from worker threads run on the event loop ──────────
def test_feed_control_from_a_thread_runs_on_the_loop():
    async def go():
        conn = ingest.FeedConn("t")
        conn._loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        ran_on = []
        conn._wake = asyncio.Event()
        orig_set = conn._wake.set
        conn._wake.set = lambda: (ran_on.append(threading.get_ident()), orig_set())
        t = threading.Thread(target=conn.restart)
        t.start()
        t.join()
        await asyncio.wait_for(conn._wake.wait(), 1)
        assert conn._restart and ran_on == [loop_thread]
    asyncio.run(go())


# ── reconnect backoff resets after a connection that worked ─────────────────
def test_backoff_resets_after_a_stable_session(monkeypatch):
    waits = []
    clock = [0.0]
    monkeypatch.setattr(ingest.time, "monotonic", lambda: clock[0])

    async def go():
        conn = ingest.FeedConn("t")
        sessions = iter([0, 0, 0, 600, 0])  # how long each session "lasts"

        async def session(host, port, login, on_line):
            clock[0] += next(sessions)
            raise ConnectionError("dropped")

        async def idle(seconds):
            waits.append(seconds)
            if len(waits) == 5:
                raise asyncio.CancelledError

        conn._session = session
        conn.idle = idle
        with pytest.raises(asyncio.CancelledError):
            await conn.run(lambda: ("h", 1, "login"), lambda line: None)

    asyncio.run(go())
    assert waits == [5, 10, 20, 5, 10]


def test_crashed_feed_loop_is_restarted(monkeypatch):
    monkeypatch.setattr(ingest, "CRASH_RESTART_S", 0)
    calls = []

    async def go():
        conn = ingest.FeedConn("t")

        async def run(target, on_line):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("bug")
            raise asyncio.CancelledError

        conn.run = run
        with pytest.raises(asyncio.CancelledError):
            await ingest.Ingester._supervise(conn, None, None)
        assert conn.error.startswith("internal error: RuntimeError")

    asyncio.run(go())
    assert len(calls) == 2


def test_security_headers(client):
    for path in ("/", "/admin", "/api/config"):
        h = client.get(path).headers
        assert h["x-frame-options"] == "DENY"
        assert h["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in h["content-security-policy"]


def test_absurd_timestamps_are_clamped(client):
    assert client.get("/api/coverage", params={"start": -5, "end": 10**30}).status_code in (200, 422)
    assert client.get("/api/coverage", params={"start": 0, "end": 2**63}).status_code in (200, 422)


def test_packets_are_handled_off_the_loop_and_broadcast_on_it():
    async def go():
        ing = ingest.Ingester()
        ing._loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        seen = {}

        def handle_line(line, now=None, dupe=False):
            seen["thread"] = threading.get_ident()
            ing._broadcast({"mobile": "K7ABC-9", "line": line})
            return "accept"

        ing.handle_line = handle_line
        q = ing.subscribe()
        await ing._on_main_line("K7ABC-9>APRS:x")
        ev = await asyncio.wait_for(q.get(), 1)
        assert seen["thread"] != loop_thread
        assert ev["line"] == "K7ABC-9>APRS:x"

    asyncio.run(go())


def test_a_failing_line_is_logged_as_error_not_raised():
    async def go():
        ing = ingest.Ingester()

        def boom(line, now=None, dupe=False):
            raise ValueError("bad")

        ing.handle_line = boom
        assert await ing._handle_off_loop("x", dupe=False) == "error"

    asyncio.run(go())


def test_live_streams_are_capped_per_address(client):
    from app.routes import public
    public.ingester.closing = True  # streams end straight after the first line
    try:
        r = client.get("/api/live")
        assert r.status_code == 200 and r.text.startswith("retry:")
        assert dict(public._live_by_ip) == {}  # the slot was given back
        public._live_by_ip["testclient"] = public.LIVE_PER_IP
        assert client.get("/api/live").status_code == 429
    finally:
        public._live_by_ip.clear()
        public.ingester.closing = False


def test_referrer_is_sent_to_tile_servers(client):
    # OpenStreetMap refuses tiles without a Referer.
    assert client.get("/").headers["referrer-policy"] == "strict-origin-when-cross-origin"
    csp = client.get("/").headers["content-security-policy"]
    assert "script-src 'self' https://unpkg.com" in csp and "tile.openstreetmap.org" in csp


def test_backup_copies_once_a_day_and_rotates(tmp_path, monkeypatch):
    import sqlite3

    from app import config, db
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "live.sqlite")
    db.init()
    with db.session() as c:
        c.execute("INSERT INTO settings(key, value) VALUES('x', 'y')")
    out = tmp_path / "bk"
    for day in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"):
        assert db.backup(out, keep=2, today=day) is not None
    assert db.backup(out, keep=2, today="2026-01-04") is None  # already done today
    files = sorted(p.name for p in out.iterdir())
    assert files == ["coverage-2026-01-03.sqlite", "coverage-2026-01-04.sqlite"]
    with sqlite3.connect(out / files[-1]) as c:
        assert c.execute("SELECT value FROM settings WHERE key='x'").fetchone() == ("y",)


def test_env_file_inline_comments_and_quotes(tmp_path, monkeypatch):
    from app import config
    env = tmp_path / ".env"
    env.write_text('A_ONE=K7ABC-CV  # my login\nA_TWO="has # hash"\nA_THREE=plain\n')
    for k in ("A_ONE", "A_TWO", "A_THREE"):
        monkeypatch.delenv(k, raising=False)
    config._load_dotenv(env)
    import os
    assert (os.environ["A_ONE"], os.environ["A_TWO"], os.environ["A_THREE"]) == ("K7ABC-CV", "has # hash", "plain")
