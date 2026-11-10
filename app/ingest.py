"""Read-only APRS-IS clients. Run as asyncio tasks inside the web process.

Two connections:
  * the coverage feed: a filtered port (14580) carrying registered stations only;
  * optionally, an aprsc duplicates feed: every copy of a packet that aprsc
    dropped because another igate had already gated it. With your igates
    connected to your own aprsc, this shows every igate and digi that heard a
    packet, not only the first one to reach APRS-IS.

Both feeds' server settings are kept in the database and edited on the admin page.
"""
import asyncio
import json
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass

from . import config, db
from .logbuf import APRS as aprs_log
from .packets import (LastFix, Outcome, RecentTx, Registry, evaluate, login_server, logresp_server,
                      strip_dupe_prefix, tx_key)

log = logging.getLogger("ingest")

VERSION = "aprs-coverage 0.1"
READ_TIMEOUT_S = 90  # aprsc sends a keepalive about every 20 s
REJECT_KEEP = 2000
REJECT_MAX_AGE_S = 2 * 86400  # rejected packets age out after 2 days
FEEDS_KEY = "feeds"
OWN_SERVER_KEY = "own_server"  # server name your aprsc gave in its login reply


def prune_rejects(c, now: int) -> int:
    """Drop rejected packets older than REJECT_MAX_AGE_S, and all but the
    newest REJECT_KEEP. Returns how many rows went."""
    n = c.execute("DELETE FROM rejects WHERE ts < ?", (now - REJECT_MAX_AGE_S,)).rowcount
    n += c.execute("DELETE FROM rejects WHERE id <= (SELECT MAX(id) FROM rejects) - ?", (REJECT_KEEP,)).rowcount
    return n


@dataclass
class FeedSettings:
    """Server settings for both feeds. Blank server/port on the coverage feed
    means the APRS_SERVER/APRS_PORT defaults from .env."""
    server: str = ""
    port: int | None = None
    dupe_enabled: bool = False
    dupe_server: str = ""
    dupe_port: int | None = None
    dupe_login: str = ""

    def main_target(self) -> tuple[str, int]:
        return self.server or config.APRS_SERVER, self.port or config.APRS_PORT


def load_feeds() -> FeedSettings:
    raw = db.get_setting(FEEDS_KEY)
    if not raw:
        return FeedSettings()
    try:
        d = json.loads(raw)
        return FeedSettings(**{k: v for k, v in d.items() if k in FeedSettings.__dataclass_fields__})
    except (ValueError, TypeError):
        log.warning("ignoring unreadable feed settings: %r", raw)
        return FeedSettings()


def save_feeds(fs: FeedSettings) -> None:
    db.set_setting(FEEDS_KEY, json.dumps(asdict(fs)))


def load_registry() -> Registry:
    with db.session() as c:
        rows = c.execute("SELECT callsign, kind FROM stations WHERE active=1 AND status='approved'").fetchall()
    reg = Registry()
    for r in rows:
        if r["kind"] == "mobile":
            reg.mobile_patterns.append(r["callsign"].upper())
        else:
            reg.infra.add(r["callsign"].upper())
    return reg


def load_last_fixes() -> dict[str, LastFix]:
    with db.session() as c:
        rows = c.execute(
            """SELECT p.mobile, p.ts, p.lat, p.lon, p.heard_by FROM packets p
               JOIN (SELECT mobile, MAX(id) AS id FROM packets WHERE dupe=0 GROUP BY mobile) m
               ON m.id = p.id"""
        ).fetchall()
    return {r["mobile"]: LastFix(r["ts"], r["lat"], r["lon"], r["heard_by"]) for r in rows}


class FeedConn:
    """One APRS-IS connection with reconnects, backoff and status."""

    def __init__(self, tag: str):
        self.tag = tag  # tag in the APRS-IS log
        self.connected = False
        self.server = ""
        self.server_line = ""
        self.connected_since: int | None = None
        self.last_line_ts: int | None = None
        self.error = ""
        self._writer: asyncio.StreamWriter | None = None
        self._wake = asyncio.Event()
        self._restart = False
        self.on_server = None  # called with the server name from the login reply

    def status(self) -> dict:
        return {"connected": self.connected, "server": self.server, "server_line": self.server_line,
                "connected_since": self.connected_since, "last_line_ts": self.last_line_ts,
                "error": self.error}

    def write(self, text: str) -> None:
        self._writer.write(f"{text}\r\n".encode())
        aprs_log.add("tx", self.tag, text)

    def wake(self) -> None:
        self._wake.set()

    def restart(self) -> None:
        """Reconnect now (settings changed), or connect if idle."""
        self._restart = True
        if self._writer is not None:
            self._writer.close()
        self._wake.set()

    async def idle(self, seconds: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self, target, on_line) -> None:
        """target() returns (host, port, login line), or a string saying why
        not to connect. on_line(line) handles each packet line."""
        backoff = 5
        while True:
            t = target()
            if isinstance(t, str):
                self.error, self.server = t, ""
                await self.idle(60)
                continue
            host, port, login = t
            self.server = f"{host}:{port}"
            self._restart = False
            try:
                await self._session(host, port, login, on_line)
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._restart:
                    self.error = f"{type(e).__name__}: {e}"
                    log.warning("%s: connection error: %s", self.tag, self.error)
                    aprs_log.add("sys", self.tag, f"connection error: {self.error}; retrying in {backoff} s")
            self.connected = False
            self._writer = None
            if self._restart:
                aprs_log.add("sys", self.tag, "settings changed; reconnecting")
                continue
            await self.idle(backoff)
            backoff = min(backoff * 2, 120)

    async def _session(self, host: str, port: int, login: str, on_line) -> None:
        log.info("%s: connecting to %s:%s", self.tag, host, port)
        aprs_log.add("sys", self.tag, f"connecting to {host}:{port}")
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 20)
        self._writer = writer
        try:
            if self._restart:  # settings changed while connecting
                raise ConnectionError("settings changed")
            self.write(login)
            await writer.drain()
            self.connected = True
            self.connected_since = int(time.time())
            self.error = ""
            while True:
                raw = await asyncio.wait_for(reader.readline(), READ_TIMEOUT_S)
                if not raw:
                    raise ConnectionError("server closed connection")
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                self.last_line_ts = int(time.time())
                if line.startswith("#"):
                    aprs_log.add("srv", self.tag, line)
                    if "logresp" in line or "aprsc" in line or "javAPRSSrvr" in line:
                        self.server_line = line[2:]
                    name = logresp_server(line)
                    if name and self.on_server:
                        self.on_server(name)
                    continue
                on_line(line)
        finally:
            self.connected = False
            writer.close()
            aprs_log.add("sys", self.tag, "disconnected")


class Ingester:
    def __init__(self):
        self.registry = Registry()
        self.feeds = FeedSettings()
        self.last: dict[str, LastFix] = {}
        self.recent = RecentTx(config.DUPLICATE_WINDOW_S)
        self.subscribers: set[asyncio.Queue] = set()
        self.closing = False
        self.main = FeedConn("feed")
        self.dupes = FeedConn("dupes")
        # The duplicates feed is your own aprsc, so its name is "your server".
        self.dupes.on_server = self._set_own_server
        self.own_server = ""
        self.active_filter = ""
        self.last_packet_ts: int | None = None
        self.counts = Counter()

    # ── registry and settings ────────────────────────────────────────────────
    def reload(self) -> None:
        """Call after the station list changes. Pushes the new filter live."""
        self.registry = load_registry()
        new_filter = self.registry.aprs_filter()
        if self.main._writer and self.main.connected and new_filter and new_filter != self.active_filter:
            try:
                self.main.write(f"#filter {new_filter}")
                self.active_filter = new_filter
                log.info("filter updated: %s", new_filter)
            except Exception as e:  # reconnect loop will recover
                log.warning("filter update failed: %s", e)
        self.main.wake()

    def set_feeds(self, fs: FeedSettings) -> None:
        """Save new feed settings and reconnect whichever feed changed."""
        save_feeds(fs)
        old, self.feeds = self.feeds, fs
        if old.main_target() != fs.main_target():
            self.main.restart()
        if (old.dupe_enabled, old.dupe_server, old.dupe_port, old.dupe_login) != \
                (fs.dupe_enabled, fs.dupe_server, fs.dupe_port, fs.dupe_login):
            self.dupes.restart()

    def _set_own_server(self, name: str) -> None:
        if name != self.own_server:
            self.own_server = name
            db.set_setting(OWN_SERVER_KEY, name)

    def status(self) -> dict:
        host, port = self.feeds.main_target()
        m = self.main
        return {
            "enabled": config.APRS_ENABLED and bool(config.APRS_LOGIN),
            "connected": m.connected,
            "server": f"{host}:{port}",
            "login": config.APRS_LOGIN,
            "server_line": m.server_line,
            "filter": self.active_filter,
            "connected_since": m.connected_since,
            "last_packet_ts": self.last_packet_ts,
            "counts": dict(self.counts),
            "error": m.error,
            "dupe": {"enabled": self.feeds.dupe_enabled, "login": self.feeds.dupe_login,
                     **self.dupes.status()},
        }

    def _main_target(self):
        if not (config.APRS_ENABLED and config.APRS_LOGIN):
            return "APRS_LOGIN not set" if config.APRS_ENABLED else "disabled"
        filt = self.registry.aprs_filter()
        if not filt:
            return "no stations registered"
        self.active_filter = filt
        host, port = self.feeds.main_target()
        return host, port, f"user {config.APRS_LOGIN} pass -1 vers {VERSION} filter {filt}"

    def _dupe_target(self):
        f = self.feeds
        if not config.APRS_ENABLED:
            return "disabled"
        if not f.dupe_enabled:
            return "off"
        if not (f.dupe_server and f.dupe_port and f.dupe_login):
            return "server, port and login are required"
        # The duplicates feed takes no filter: everything is checked here.
        return f.dupe_server, f.dupe_port, f"user {f.dupe_login} pass -1 vers {VERSION}"

    # ── live fan-out ─────────────────────────────────────────────────────────
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def close_subscribers(self) -> None:
        """Server is stopping: wake every live stream with a None sentinel so
        it ends. Streams also check `closing` in case a queue was full."""
        self.closing = True
        for q in list(self.subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def _broadcast(self, event: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # ── packet handling ──────────────────────────────────────────────────────
    def handle_line(self, line: str, now: int | None = None, dupe: bool = False) -> str:
        """Evaluate and store one line. dupe=True for the duplicates feed."""
        now = now or int(time.time())
        pre = "dupe_" if dupe else ""
        self.counts[f"{pre}lines"] += 1
        self._note_login_server(line, now)
        out = evaluate(line, now, self.registry, self.last, dupe=dupe)
        if out.kind == "accept":
            r = out.record
            key = tx_key(r["mobile"], line)
            route = (r["heard_by"], r["igate"])
            tx = self.recent.get(key, now)
            if tx is not None and route in tx.routes:
                # The same copy again (e.g. on both feeds): nothing new heard.
                out = Outcome("reject", "duplicate", out.source)
            else:
                self._store(r, key, route, tx, now, dupe)
                self.counts[f"{pre}accepted"] += 1
        if out.kind == "infra":
            r = out.record
            with db.session() as c:
                c.execute(
                    "UPDATE stations SET auto_lat=?, auto_lon=?, last_beacon=? WHERE callsign=?",
                    (r["lat"], r["lon"], r["ts"], out.source),
                )
            self.counts[f"{pre}infra_beacons"] += 1
        elif out.kind == "reject":
            self.counts[f"{pre}reject_{out.reason}"] += 1
            with db.session() as c:
                c.execute(
                    "INSERT INTO rejects(ts,source,reason,raw) VALUES(?,?,?,?)",
                    (now, out.source, out.reason, line),
                )
                if self.counts["rejects_since_prune"] >= 200:
                    prune_rejects(c, now)
                    self.counts["rejects_since_prune"] = 0
            self.counts["rejects_since_prune"] += 1
        return out.kind

    def _note_login_server(self, line: str, now: int) -> None:
        """Remember which APRS-IS server a registered igate/digi is logged into,
        from the qAC on packets it sends over its own login."""
        ls = login_server(line)
        if ls and ls[0] in self.registry.infra:
            with db.session() as c:
                c.execute("UPDATE stations SET is_server=?, is_server_ts=? WHERE callsign=?",
                          (ls[1], now, ls[0]))

    def _store(self, r: dict, key: str, route, tx, now: int, dupe: bool) -> None:
        """Insert an accepted copy. Copies of one transmission share a txid
        (the id of the first copy stored), so stats count it once."""
        with db.session() as c:
            cur = c.execute(
                """INSERT INTO packets(ts,mobile,lat,lon,alt,speed,course,igate,heard_by,hops,chain,raw,txid,dupe)
                   VALUES(:ts,:mobile,:lat,:lon,:alt,:speed,:course,:igate,:heard_by,:hops,:chain,:raw,:txid,:dupe)""",
                {**r, "txid": tx.txid if tx else None, "dupe": int(dupe)},
            )
            txid = tx.txid if tx else cur.lastrowid
            if tx is None:
                c.execute("UPDATE packets SET txid=? WHERE id=?", (txid, txid))
        self.recent.add(key, now, txid, route)
        if not dupe:
            self.last[r["mobile"]] = LastFix(r["ts"], r["lat"], r["lon"], r["heard_by"])
        self.last_packet_ts = now
        ev = {k: r[k] for k in ("ts", "mobile", "lat", "lon", "igate", "heard_by", "hops")}
        self._broadcast({**ev, "dupe": dupe})

    def _on_main_line(self, line: str) -> None:
        try:
            kind = self.handle_line(line)
        except Exception:
            kind = "error"
            log.exception("failed to handle line: %r", line)
        aprs_log.add("rx", "feed", line, kind)

    def _on_dupe_line(self, line: str) -> None:
        line = strip_dupe_prefix(line)
        try:
            kind = self.handle_line(line, dupe=True)
        except Exception:
            kind = "error"
            log.exception("failed to handle duplicate: %r", line)
        # The duplicates feed is unfiltered, so only log what concerns us.
        if kind != "ignore":
            aprs_log.add("rx", "dupes", line, kind)

    # ── connection loops ─────────────────────────────────────────────────────
    async def run(self) -> None:
        self.registry = load_registry()
        self.feeds = load_feeds()
        self.own_server = db.get_setting(OWN_SERVER_KEY) or ""
        self.last = load_last_fixes()
        await asyncio.gather(
            self.main.run(self._main_target, self._on_main_line),
            self.dupes.run(self._dupe_target, self._on_dupe_line),
        )


ingester = Ingester()
