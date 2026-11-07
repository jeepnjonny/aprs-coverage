"""Read-only APRS-IS client. Runs as an asyncio task inside the web process."""
import asyncio
import logging
import time
from collections import Counter

from . import config, db
from .logbuf import APRS as aprs_log
from .packets import LastFix, Registry, evaluate

log = logging.getLogger("ingest")

VERSION = "aprs-coverage 0.1"
READ_TIMEOUT_S = 90  # aprsc sends a keepalive about every 20 s
REJECT_KEEP = 2000


def load_registry() -> Registry:
    with db.session() as c:
        rows = c.execute("SELECT callsign, kind FROM stations WHERE active=1").fetchall()
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
               JOIN (SELECT mobile, MAX(id) AS id FROM packets GROUP BY mobile) m ON m.id = p.id"""
        ).fetchall()
    return {r["mobile"]: LastFix(r["ts"], r["lat"], r["lon"], r["heard_by"]) for r in rows}


class Ingester:
    def __init__(self):
        self.registry = Registry()
        self.last: dict[str, LastFix] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self.connected = False
        self.server_line = ""
        self.active_filter = ""
        self.connected_since: int | None = None
        self.last_packet_ts: int | None = None
        self.counts = Counter()
        self.error = ""
        self._writer: asyncio.StreamWriter | None = None
        self._wake = asyncio.Event()

    # ── registry ─────────────────────────────────────────────────────────────
    def reload(self) -> None:
        """Call after the station list changes. Pushes the new filter live."""
        self.registry = load_registry()
        new_filter = self.registry.aprs_filter()
        if self._writer and self.connected and new_filter and new_filter != self.active_filter:
            try:
                self._writer.write(f"#filter {new_filter}\r\n".encode())
                aprs_log.add("tx", "feed", f"#filter {new_filter}")
                self.active_filter = new_filter
                log.info("filter updated: %s", new_filter)
            except Exception as e:  # reconnect loop will recover
                log.warning("filter update failed: %s", e)
        self._wake.set()

    def status(self) -> dict:
        return {
            "enabled": config.APRS_ENABLED and bool(config.APRS_LOGIN),
            "connected": self.connected,
            "server": f"{config.APRS_SERVER}:{config.APRS_PORT}",
            "login": config.APRS_LOGIN,
            "server_line": self.server_line,
            "filter": self.active_filter,
            "connected_since": self.connected_since,
            "last_packet_ts": self.last_packet_ts,
            "counts": dict(self.counts),
            "error": self.error,
        }

    # ── live fan-out ─────────────────────────────────────────────────────────
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def _broadcast(self, event: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # ── packet handling ──────────────────────────────────────────────────────
    def handle_line(self, line: str, now: int | None = None) -> str:
        now = now or int(time.time())
        self.counts["lines"] += 1
        out = evaluate(line, now, self.registry, self.last)
        if out.kind == "accept":
            r = out.record
            with db.session() as c:
                c.execute(
                    """INSERT INTO packets(ts,mobile,lat,lon,alt,speed,course,igate,heard_by,hops,chain,raw)
                       VALUES(:ts,:mobile,:lat,:lon,:alt,:speed,:course,:igate,:heard_by,:hops,:chain,:raw)""",
                    r,
                )
            self.last[r["mobile"]] = LastFix(r["ts"], r["lat"], r["lon"], r["heard_by"])
            self.last_packet_ts = now
            self.counts["accepted"] += 1
            self._broadcast({k: r[k] for k in ("ts", "mobile", "lat", "lon", "igate", "heard_by", "hops")})
        elif out.kind == "infra":
            r = out.record
            with db.session() as c:
                c.execute(
                    "UPDATE stations SET auto_lat=?, auto_lon=?, last_beacon=? WHERE callsign=?",
                    (r["lat"], r["lon"], r["ts"], out.source),
                )
            self.counts["infra_beacons"] += 1
        elif out.kind == "reject":
            self.counts[f"reject_{out.reason}"] += 1
            with db.session() as c:
                c.execute(
                    "INSERT INTO rejects(ts,source,reason,raw) VALUES(?,?,?,?)",
                    (now, out.source, out.reason, line),
                )
                if self.counts["rejects_since_prune"] >= 200:
                    c.execute(
                        "DELETE FROM rejects WHERE id <= (SELECT MAX(id) FROM rejects) - ?",
                        (REJECT_KEEP,),
                    )
                    self.counts["rejects_since_prune"] = 0
            self.counts["rejects_since_prune"] += 1
        return out.kind

    # ── connection loop ──────────────────────────────────────────────────────
    async def run(self) -> None:
        self.registry = load_registry()
        self.last = load_last_fixes()
        backoff = 5
        while True:
            if not (config.APRS_ENABLED and config.APRS_LOGIN):
                self.error = "APRS_LOGIN not set" if config.APRS_ENABLED else "disabled"
                await self._idle(60)
                continue
            filt = self.registry.aprs_filter()
            if not filt:
                self.error = "no stations registered"
                await self._idle(30)
                continue
            try:
                await self._session(filt)
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                log.warning("APRS-IS connection error: %s", self.error)
                aprs_log.add("sys", "feed", f"connection error: {self.error}; retrying in {backoff} s")
            self.connected = False
            self._writer = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

    async def _idle(self, seconds: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _session(self, filt: str) -> None:
        log.info("connecting to %s:%s", config.APRS_SERVER, config.APRS_PORT)
        aprs_log.add("sys", "feed", f"connecting to {config.APRS_SERVER}:{config.APRS_PORT}")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(config.APRS_SERVER, config.APRS_PORT), 20
        )
        self._writer = writer
        try:
            login_line = f"user {config.APRS_LOGIN} pass -1 vers {VERSION} filter {filt}"
            writer.write(f"{login_line}\r\n".encode())
            aprs_log.add("tx", "feed", login_line)
            await writer.drain()
            self.active_filter = filt
            self.connected = True
            self.connected_since = int(time.time())
            self.error = ""
            log.info("logged in, filter: %s", filt)
            while True:
                raw = await asyncio.wait_for(reader.readline(), READ_TIMEOUT_S)
                if not raw:
                    raise ConnectionError("server closed connection")
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("#"):
                    aprs_log.add("srv", "feed", line)
                    if "logresp" in line or "aprsc" in line or "javAPRSSrvr" in line:
                        self.server_line = line[2:]
                    continue
                try:
                    kind = self.handle_line(line)
                except Exception:
                    kind = "error"
                    log.exception("failed to handle line: %r", line)
                aprs_log.add("rx", "feed", line, kind)
        finally:
            self.connected = False
            writer.close()
            aprs_log.add("sys", "feed", "disconnected")


ingester = Ingester()
