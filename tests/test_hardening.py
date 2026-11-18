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
