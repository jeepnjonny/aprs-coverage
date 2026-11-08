"""FastAPI app: public map API, live feed, admin API, static pages."""
import asyncio
import json
import logging
import math
import re
import signal
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from . import auth, config, db, grid, ingest, logbuf, remotecfg
from .ingest import ingester
from .packets import mobile_matches

remote = remotecfg.RemoteConfig()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logbuf.install_app_handler()
STATIC = config.BASE_DIR / "static"


def _end_streams_on_stop() -> None:
    """On SIGTERM/SIGINT uvicorn waits for open connections to finish before
    running lifespan shutdown, and /api/live streams never finish on their own,
    so a stop hung until systemd killed the process. Chain onto uvicorn's
    signal handlers so the streams end as soon as the stop signal arrives."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        prev = signal.getsignal(sig)
        if not callable(prev):
            continue

        def handler(signum, frame, prev=prev):
            loop.call_soon_threadsafe(ingester.close_subscribers)
            prev(signum, frame)

        try:
            signal.signal(sig, handler)
        except ValueError:  # not the main thread (e.g. the test client)
            return


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    _end_streams_on_stop()
    task = asyncio.create_task(ingester.run())
    yield
    task.cancel()
    await remote.sender.close()


app = FastAPI(title="APRS Coverage", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def revalidate_pages(request: Request, call_next):
    """Pages and static files must be revalidated on every load (a cheap 304
    when unchanged), so browsers pick up a deploy without a hard refresh."""
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/admin") or path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC / "admin.html")


# ── query helpers ────────────────────────────────────────────────────────────
UNTRACED, OTHER = "__untraced__", "__other__"


def _infra_calls(c) -> list[str]:
    return [r["callsign"] for r in c.execute("SELECT callsign FROM stations WHERE kind != 'mobile'")]


def _csv(v: str | None) -> list[str] | None:
    """None = no filter (all). '' = an explicit empty selection."""
    if v is None:
        return None
    return [s.strip().upper() if not s.startswith("__") else s.strip() for s in v.split(",") if s.strip()]


def _where(c, start, end, mobiles, heard_by, match):
    clauses, args = ["ts >= ?", "ts <= ?"], [start, end]
    if mobiles is not None:
        if not mobiles:
            clauses.append("0")
        else:
            clauses.append(f"mobile IN ({','.join('?' * len(mobiles))})")
            args += mobiles
    if heard_by is not None:
        ors = []
        calls = [h for h in heard_by if not h.startswith("__")]
        if calls:
            if match == "any":
                ors += ["chain LIKE ?"] * len(calls)
                args += [f"% {h} %" for h in calls]
            else:
                ors.append(f"heard_by IN ({','.join('?' * len(calls))})")
                args += calls
        if UNTRACED in heard_by:
            ors.append("heard_by IS NULL")
        if OTHER in heard_by:
            infra = _infra_calls(c)
            if infra:
                ors.append(f"(heard_by IS NOT NULL AND heard_by NOT IN ({','.join('?' * len(infra))}))")
                args += infra
            else:
                ors.append("heard_by IS NOT NULL")
        clauses.append("(" + " OR ".join(ors) + ")" if ors else "0")
    return " AND ".join(clauses), args


def _time_range(start: int | None, end: int | None) -> tuple[int, int]:
    now = int(time.time())
    end = end if end is not None else now
    start = start if start is not None else end - 86400
    if start > end:
        raise HTTPException(400, "start must be before end")
    return start, end


def _cell_size(cell: int) -> int:
    if cell not in config.CELL_SIZES_M:
        raise HTTPException(400, f"cell must be one of {config.CELL_SIZES_M}")
    return cell


# ── public API ───────────────────────────────────────────────────────────────
@app.get("/api/config")
def api_config():
    return {
        "cell_sizes": config.CELL_SIZES_M,
        "grid_ref_lat": config.GRID_REF_LAT,
        "default_bounds": [[43.50, -116.68], [43.92, -116.12]],  # Nampa/Meridian/Boise/Emmett
    }


@app.get("/api/stations")
def api_stations():
    with db.session() as c:
        rows = c.execute("SELECT * FROM stations WHERE active=1 ORDER BY kind, callsign").fetchall()
        heard = dict(c.execute("SELECT heard_by, MAX(ts) FROM packets WHERE heard_by IS NOT NULL GROUP BY heard_by").fetchall())
        gated = dict(c.execute("SELECT igate, MAX(ts) FROM packets GROUP BY igate").fetchall())
        mob = dict(c.execute("SELECT mobile, MAX(ts) FROM packets GROUP BY mobile").fetchall())
    out = []
    for r in rows:
        call = r["callsign"]
        lat = r["lat"] if r["lat"] is not None else r["auto_lat"]
        lon = r["lon"] if r["lon"] is not None else r["auto_lon"]
        if r["kind"] == "mobile":
            last = max((t for m, t in mob.items() if _matches(m, call)), default=None)
        else:
            last = max(filter(None, [heard.get(call), gated.get(call)]), default=None)
        out.append({
            "callsign": call, "kind": r["kind"], "label": r["label"], "color": r["color"],
            "lat": lat, "lon": lon, "last_beacon": r["last_beacon"], "last_heard": last,
        })
    return out


def _matches(call: str, pattern: str) -> bool:
    return mobile_matches(call, pattern)


@app.get("/api/mobiles")
def api_mobiles():
    """Concrete mobile callsigns seen in the data (wildcard patterns expand here)."""
    with db.session() as c:
        return [r[0] for r in c.execute("SELECT DISTINCT mobile FROM packets ORDER BY mobile")]


@app.get("/api/coverage")
def api_coverage(
    start: int | None = None,
    end: int | None = None,
    cell: int = 500,
    mobiles: str | None = None,
    heard_by: str | None = None,
    match: str = Query("first", pattern="^(first|any)$"),
    color: str = Query("single", pattern="^(single|heard_by|mobile)$"),
):
    start, end = _time_range(start, end)
    cell = _cell_size(cell)
    cx, cy = grid.sql_cell_exprs(cell)
    with db.session() as c:
        where, args = _where(c, start, end, _csv(mobiles), _csv(heard_by), match)
        rows = c.execute(
            f"SELECT {cx} AS x, {cy} AS y, COALESCE(heard_by, '(untraced)') AS h, mobile AS m, "
            f"COUNT(DISTINCT txid) AS n, MIN(ts) AS first, MAX(ts) AS last "
            f"FROM packets WHERE {where} GROUP BY x, y, h, m",
            args,
        ).fetchall()
        # A transmission heard by several stations has one row per copy; the
        # square's total counts it once.
        totals = {(r[0], r[1]): r[2] for r in c.execute(
            f"SELECT {cx} AS x, {cy} AS y, COUNT(DISTINCT txid) FROM packets WHERE {where} GROUP BY x, y",
            args)}
        total = c.execute(f"SELECT COUNT(DISTINCT txid) FROM packets WHERE {where}", args).fetchone()[0]
    # Every square carries both breakdowns so the tooltip can list who heard it
    # and which mobiles were there, whatever the color mode.
    cells: dict[tuple[int, int], list] = {}
    for r in rows:
        e = cells.setdefault((r["x"], r["y"]), [r["x"], r["y"], totals[(r["x"], r["y"])], 0, {}, {}, {}, r["first"]])
        e[3] = max(e[3], r["last"])
        e[5][r["h"]] = e[5].get(r["h"], 0) + r["n"]
        e[6][r["m"]] = e[6].get(r["m"], 0) + r["n"]
        e[7] = min(e[7], r["first"])
    for e in cells.values():
        e[4] = {"single": {}, "heard_by": e[5], "mobile": e[6]}[color]
    dlat, dlon = grid.cell_deg(cell)
    return {
        "cell_m": cell, "dlat": dlat, "dlon": dlon, "start": start, "end": end,
        "packets": total,
        # [x, y, count, last_ts, {color key: count}, {heard_by: count}, {mobile: count}, first_ts]
        "cells": list(cells.values()),
    }


@app.get("/api/stats")
def api_stats(
    start: int | None = None,
    end: int | None = None,
    cell: int = 500,
    mobiles: str | None = None,
):
    """Per igate/digi and per mobile summary, using the current time and mobile filters."""
    start, end = _time_range(start, end)
    cell = _cell_size(cell)
    cx, cy = grid.sql_cell_exprs(cell)
    with db.session() as c:
        where, args = _where(c, start, end, _csv(mobiles), None, "first")
        direct = {
            r["k"]: dict(r) for r in c.execute(
                f"SELECT COALESCE(heard_by,'(untraced)') AS k, COUNT(DISTINCT txid) AS packets, "
                f"COUNT(DISTINCT {cx} || ',' || {cy}) AS cells, COUNT(DISTINCT mobile) AS mobiles, "
                f"MAX(ts) AS last FROM packets WHERE {where} GROUP BY k", args)
        }
        gated = dict(c.execute(
            f"SELECT igate, COUNT(DISTINCT txid) FROM packets WHERE {where} GROUP BY igate", args).fetchall())
        infra = c.execute("SELECT * FROM stations WHERE kind != 'mobile'").fetchall()

        ky = grid.M_PER_DEG_LAT / 1000
        infra_rows = []
        for s in infra:
            call = s["callsign"]
            lat = s["lat"] if s["lat"] is not None else s["auto_lat"]
            lon = s["lon"] if s["lon"] is not None else s["auto_lon"]
            max_km = None
            if lat is not None and lon is not None and call in direct:
                kx = grid.M_PER_DEG_LON_EQ / 1000 * math.cos(math.radians(lat))
                r = c.execute(
                    f"SELECT MAX(((lat-?)*?)*((lat-?)*?) + ((lon-?)*?)*((lon-?)*?)) "
                    f"FROM packets WHERE {where} AND heard_by=?",
                    [lat, ky, lat, ky, lon, kx, lon, kx, *args, call],
                ).fetchone()[0]
                max_km = round(math.sqrt(r), 1) if r is not None else None
            d = direct.pop(call, {})
            infra_rows.append({
                "callsign": call, "kind": s["kind"], "label": s["label"], "registered": True,
                "heard_direct": d.get("packets", 0), "cells": d.get("cells", 0),
                "mobiles": d.get("mobiles", 0), "gated": gated.get(call, 0),
                "max_range_km": max_km, "last_heard": d.get("last"),
            })
        for k, d in direct.items():  # unregistered first hops and untraced
            infra_rows.append({
                "callsign": k, "kind": None, "label": "", "registered": False,
                "heard_direct": d["packets"], "cells": d["cells"], "mobiles": d["mobiles"],
                "gated": gated.get(k, 0), "max_range_km": None, "last_heard": d["last"],
            })
        infra_rows.sort(key=lambda r: (-r["heard_direct"], r["callsign"]))

        mob_rows = [dict(r) for r in c.execute(
            f"SELECT mobile, COUNT(DISTINCT txid) AS packets, COUNT(DISTINCT {cx} || ',' || {cy}) AS cells, "
            f"MAX(ts) AS last FROM packets WHERE {where} GROUP BY mobile ORDER BY packets DESC", args)]
    return {"infra": infra_rows, "mobiles": mob_rows}


@app.get("/api/status")
def api_status():
    s = ingester.status()
    return {k: s[k] for k in ("enabled", "connected", "last_packet_ts")}


@app.get("/api/live")
async def api_live(request: Request):
    async def stream():
        q = ingester.subscribe()
        try:
            yield "retry: 5000\n\n"
            while not ingester.closing:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), 20)
                    if ev is None:  # server stopping
                        break
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            ingester.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── admin API ────────────────────────────────────────────────────────────────
class LoginIn(BaseModel):
    password: str


_PREFIX_OK = re.compile(r"^(/[A-Za-z0-9._~-]+)+/?$")


def _cookie_path(request: Request) -> str:
    """Scope the session cookie to the URL prefix nginx serves the app under
    (sent as X-Forwarded-Prefix, e.g. /aprs-coverage), so it is not sent to the
    other apps on the same host. Without the header (local runs) it is '/'."""
    prefix = request.headers.get("x-forwarded-prefix", "").strip()
    if not _PREFIX_OK.match(prefix):
        return "/"
    return prefix.rstrip("/") + "/"


@app.post("/api/admin/login")
def admin_login(body: LoginIn, request: Request, response: Response):
    ip = auth.client_ip(request)
    auth.check_rate_limit(ip)
    stored = db.get_setting("admin_password")
    if not stored:
        raise HTTPException(503, "No admin password set. On the server run: python -m app.manage set-password")
    if not auth.verify_password(body.password, stored):
        auth.record_failure(ip)
        raise HTTPException(401, "Wrong password")
    token = auth.create_session()
    response.set_cookie(auth.COOKIE, token, max_age=config.SESSION_DAYS * 86400, httponly=True,
                        secure=config.COOKIE_SECURE, samesite="strict", path=_cookie_path(request))
    return {"ok": True}


@app.post("/api/admin/logout")
def admin_logout(request: Request, response: Response):
    auth.destroy_session(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, path=_cookie_path(request))
    return {"ok": True}


@app.get("/api/admin/me")
def admin_me(request: Request):
    return {"logged_in": auth.is_logged_in(request)}


class PasswordIn(BaseModel):
    current: str
    new: str


@app.post("/api/admin/password", dependencies=[Depends(auth.require_admin)])
def admin_password(body: PasswordIn):
    if not auth.verify_password(body.current, db.get_setting("admin_password") or ""):
        raise HTTPException(400, "Current password is wrong")
    if len(body.new) < 10:
        raise HTTPException(400, "New password must be at least 10 characters")
    db.set_setting("admin_password", auth.hash_password(body.new))
    return {"ok": True}


CALL_RE = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,2})?$")
MOBILE_RE = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,2}|-\*|\*)?$")
# Object tracker: SENDER:OBJECT, e.g. K7SWI:CAR01 or K7SWI-*:CAR*. Object names
# are up to 9 characters; spaces are not allowed here (trailing pad is stripped).
OBJECT_RE = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,2}|-\*|\*)?:[A-Z0-9_./#*-]{1,9}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class StationIn(BaseModel):
    callsign: str
    kind: str
    label: str = ""
    notes: str = ""
    color: str | None = None
    lat: float | None = None
    lon: float | None = None
    active: bool = True

    @field_validator("callsign")
    @classmethod
    def _call(cls, v):
        return v.strip().upper()

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        if v in (None, ""):
            return None
        if not COLOR_RE.match(v):
            raise ValueError("color must be #rrggbb")
        return v.lower()

    def check(self):
        if self.kind not in ("mobile", "igate", "digi", "digi_igate"):
            raise HTTPException(422, "kind must be mobile, igate, digi or digi_igate")
        if self.kind == "mobile":
            ok = MOBILE_RE.match(self.callsign) or OBJECT_RE.match(self.callsign)
        else:
            ok = CALL_RE.match(self.callsign)
        if not ok:
            hint = (" (mobiles may end in * or -* for any SSID; object trackers are"
                    " SENDER:OBJECT, e.g. K7SWI:CAR01)") if self.kind == "mobile" else ""
            raise HTTPException(422, f"Invalid callsign{hint}")
        if (self.lat is None) != (self.lon is None):
            raise HTTPException(422, "Give both lat and lon, or neither")
        if self.lat is not None and not (-90 <= self.lat <= 90 and -180 <= self.lon <= 180):
            raise HTTPException(422, "lat/lon out of range")


@app.get("/api/admin/stations", dependencies=[Depends(auth.require_admin)])
def admin_list_stations():
    """Igates and digis get `own_server`, the name of your aprsc (from the
    duplicates feed's login reply), to compare with their `is_server`.
    Igates also get `gated`: packets from registered mobiles they gated in
    the last GATED_WINDOW_S, split by feed. "main" copies are the ones APRS-IS
    passed on (this igate was first); "dupe" copies came from the aprsc
    duplicates feed (another igate got there first)."""
    since = int(time.time()) - GATED_WINDOW_S
    with db.session() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM stations ORDER BY kind, callsign")]
        gated: dict[str, dict] = {}
        for r in c.execute(
            "SELECT igate, dupe, COUNT(*) AS n, MAX(ts) AS last FROM packets WHERE ts >= ? GROUP BY igate, dupe",
            (since,),
        ):
            g = gated.setdefault(r["igate"].upper(), {"main": 0, "main_last": None, "dupe": 0, "dupe_last": None})
            pre = "dupe" if r["dupe"] else "main"
            g[pre], g[f"{pre}_last"] = r["n"], r["last"]
    empty = {"main": 0, "main_last": None, "dupe": 0, "dupe_last": None}
    for s in rows:
        if s["kind"] != "mobile":
            s["own_server"] = ingester.own_server  # to compare with is_server
        if s["kind"] in ("igate", "digi_igate"):
            s["gated"] = {**gated.get(s["callsign"].upper(), empty), "window_s": GATED_WINDOW_S}
    return rows


GATED_WINDOW_S = 7 * 86400


@app.post("/api/admin/stations", dependencies=[Depends(auth.require_admin)])
def admin_create_station(body: StationIn):
    body.check()
    with db.session() as c:
        try:
            cur = c.execute(
                "INSERT INTO stations(callsign,kind,label,notes,color,lat,lon,active) VALUES(?,?,?,?,?,?,?,?)",
                (body.callsign, body.kind, body.label, body.notes, body.color, body.lat, body.lon, int(body.active)),
            )
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, f"{body.callsign} is already registered")
            raise
        sid = cur.lastrowid
    ingester.reload()
    return {"id": sid}


@app.put("/api/admin/stations/{sid}", dependencies=[Depends(auth.require_admin)])
def admin_update_station(sid: int, body: StationIn):
    body.check()
    with db.session() as c:
        try:
            cur = c.execute(
                "UPDATE stations SET callsign=?,kind=?,label=?,notes=?,color=?,lat=?,lon=?,active=? WHERE id=?",
                (body.callsign, body.kind, body.label, body.notes, body.color, body.lat, body.lon,
                 int(body.active), sid),
            )
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, f"{body.callsign} is already registered")
            raise
        if cur.rowcount == 0:
            raise HTTPException(404, "No such station")
    ingester.reload()
    return {"ok": True}


@app.delete("/api/admin/stations/{sid}", dependencies=[Depends(auth.require_admin)])
def admin_delete_station(sid: int):
    with db.session() as c:
        if c.execute("DELETE FROM stations WHERE id=?", (sid,)).rowcount == 0:
            raise HTTPException(404, "No such station")
    ingester.reload()
    return {"ok": True}


def _station_packets(c, sid: int) -> tuple[dict, str, list]:
    """WHERE clause for the packets a station owns: a mobile's own packets
    (wildcard patterns via GLOB, which matches fnmatch for '*'), or the packets
    an igate/digi heard directly. Packets it merely relayed after another
    station heard them are that station's coverage and are left alone."""
    st = c.execute("SELECT * FROM stations WHERE id=?", (sid,)).fetchone()
    if st is None:
        raise HTTPException(404, "No such station")
    call = st["callsign"].upper()
    if st["kind"] == "mobile":
        # Keep callsign patterns and SENDER:OBJECT patterns apart, as mobile_matches does.
        sep = "mobile GLOB '*:*'" if ":" in call else "mobile NOT GLOB '*:*'"
        return dict(st), f"mobile GLOB ? AND {sep}", [call]
    return dict(st), "heard_by = ?", [call]


@app.get("/api/admin/stations/{sid}/packets", dependencies=[Depends(auth.require_admin)])
def admin_count_station_packets(sid: int):
    with db.session() as c:
        _, where, args = _station_packets(c, sid)
        return {"count": c.execute(f"SELECT COUNT(*) FROM packets WHERE {where}", args).fetchone()[0]}


@app.delete("/api/admin/stations/{sid}/packets", dependencies=[Depends(auth.require_admin)])
def admin_purge_station_packets(sid: int):
    with db.session() as c:
        st, where, args = _station_packets(c, sid)
        deleted = c.execute(f"DELETE FROM packets WHERE {where}", args).rowcount
    if st["kind"] == "mobile":
        # Forget the last fix too, so a changed station isn't rejected as a
        # speed jump from its old position.
        pattern = st["callsign"].upper()
        for m in [m for m in ingester.last if mobile_matches(m, pattern)]:
            ingester.last.pop(m, None)
    logging.getLogger("admin").info("purged %d packets for %s", deleted, st["callsign"])
    return {"deleted": deleted}


@app.get("/api/admin/status", dependencies=[Depends(auth.require_admin)])
def admin_status():
    with db.session() as c:
        totals = dict(c.execute(
            "SELECT COUNT(*) AS packets, COALESCE(SUM(dupe), 0) AS dupes, MIN(ts) AS first, MAX(ts) AS last "
            "FROM packets").fetchone())
    return {"ingest": ingester.status(), "db": totals, "db_path": str(config.DB_PATH)}


@app.get("/api/admin/rejects", dependencies=[Depends(auth.require_admin)])
def admin_rejects(limit: int = Query(200, ge=1, le=2000)):
    with db.session() as c:
        return [dict(r) for r in c.execute("SELECT * FROM rejects ORDER BY id DESC LIMIT ?", (limit,))]


# ── feed settings (admin "Stations" tab) ─────────────────────────────────────
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$|^\[?[0-9A-Fa-f:]+\]?$")
LOGIN_RE = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,2})?$")


class FeedsIn(BaseModel):
    server: str = ""
    port: int | None = None
    dupe_enabled: bool = False
    dupe_server: str = ""
    dupe_port: int | None = None
    dupe_login: str = ""

    @field_validator("server", "dupe_server")
    @classmethod
    def _host(cls, v):
        v = v.strip()
        if v and not HOST_RE.match(v):
            raise ValueError("server must be a host name or IP address")
        return v.strip("[]")

    @field_validator("port", "dupe_port")
    @classmethod
    def _port(cls, v):
        if v is not None and not 1 <= v <= 65535:
            raise ValueError("port must be 1-65535")
        return v

    @field_validator("dupe_login")
    @classmethod
    def _dlogin(cls, v):
        v = v.strip().upper()
        if v and not LOGIN_RE.match(v):
            raise ValueError("login must be a callsign with an optional SSID, e.g. K7ABC-DF")
        return v


def _feeds_out() -> dict:
    from dataclasses import asdict
    return {"settings": asdict(ingester.feeds), "default_server": config.APRS_SERVER,
            "default_port": config.APRS_PORT, "login": config.APRS_LOGIN}


@app.get("/api/admin/feeds", dependencies=[Depends(auth.require_admin)])
def admin_feeds():
    return _feeds_out()


@app.put("/api/admin/feeds", dependencies=[Depends(auth.require_admin)])
def admin_set_feeds(body: FeedsIn):
    if body.dupe_enabled:
        if not (body.dupe_server and body.dupe_port and body.dupe_login):
            raise HTTPException(422, "The duplicates feed needs a server, a port and a login")
    if body.dupe_login and body.dupe_login == config.APRS_LOGIN:
        raise HTTPException(422, f"Use a different login from the coverage feed ({config.APRS_LOGIN}), "
                                 "or the server may drop one connection when the other logs in")
    ingester.set_feeds(ingest.FeedSettings(**body.model_dump()))
    logging.getLogger("admin").info("feed settings changed: %s", body.model_dump())
    return _feeds_out()


# ── remote node config (admin "Node config" tab) ─────────────────────────────
# The operator's sending callsign comes with every request (the page keeps it in
# the browser); the server never stores it, and computes its passcode itself.
def _config_node(callsign: str) -> str:
    """Only active, registered igates/digis can be configured."""
    call = callsign.strip().upper()
    with db.session() as c:
        row = c.execute(
            "SELECT callsign FROM stations WHERE callsign=? AND kind != 'mobile' AND active=1", (call,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"{call} is not an active registered igate or digi")
    return row["callsign"]


def _login(login: str) -> str:
    """Refuse a sending callsign that a station here is already logged in to
    APRS-IS with: the server would drop that station's connection."""
    with db.session() as c:
        reserved = {r[0] for r in c.execute("SELECT callsign FROM stations WHERE kind != 'mobile'")}
    if config.APRS_LOGIN:
        reserved.add(config.APRS_LOGIN)
    try:
        return remotecfg.check_login(login, reserved)
    except remotecfg.CommandError as e:
        raise HTTPException(e.status, str(e))


async def _remote_call(coro):
    try:
        return await coro
    except remotecfg.CommandError as e:
        raise HTTPException(e.status, str(e))


@app.get("/api/admin/remote", dependencies=[Depends(auth.require_admin)])
def admin_remote(login: str | None = None):
    with db.session() as c:
        nodes = [dict(r) for r in c.execute(
            """SELECT s.callsign, s.kind, s.label, n.fields, n.updated_at FROM stations s
               LEFT JOIN node_config n ON n.callsign = s.callsign
               WHERE s.kind != 'mobile' AND s.active=1 ORDER BY s.callsign""")]
        logs = [dict(r) for r in c.execute("SELECT * FROM node_config_log ORDER BY id DESC LIMIT 50")]
    for n in nodes:
        n["fields"] = json.loads(n["fields"]) if n["fields"] else {}
    login = (login or "").strip().upper() or None
    return {"status": remote.status(login), "fields": remotecfg.FIELDS, "nodes": nodes, "log": logs}


class RemoteIn(BaseModel):
    login: str


class RemoteReadIn(RemoteIn):
    codes: list[str] | None = None


class RemoteUnlockIn(RemoteIn):
    token: str


class RemoteWriteIn(RemoteIn):
    fields: dict[str, str | int | float]


@app.post("/api/admin/remote/{callsign}/read", dependencies=[Depends(auth.require_admin)])
async def admin_remote_read(callsign: str, body: RemoteReadIn):
    return await _remote_call(remote.read(_login(body.login), _config_node(callsign), body.codes))


@app.post("/api/admin/remote/{callsign}/unlock", dependencies=[Depends(auth.require_admin)])
async def admin_remote_unlock(callsign: str, body: RemoteUnlockIn):
    return await _remote_call(remote.unlock(_login(body.login), _config_node(callsign), body.token))


@app.post("/api/admin/remote/{callsign}/write", dependencies=[Depends(auth.require_admin)])
async def admin_remote_write(callsign: str, body: RemoteWriteIn):
    return await _remote_call(remote.write(_login(body.login), _config_node(callsign), body.fields))


# ── logs (admin "Logs" tab) ──────────────────────────────────────────────────
# Every log is served as entries {id, ts, level, tag, text, note}. The page asks
# for the newest `limit` entries, then polls with after=<last id> for new ones.
LOG_SOURCES = {
    "aprs": ("APRS-IS traffic", "Every line to and from APRS-IS on the coverage feed and the node-config "
             "connection, plus lines from registered stations on the duplicates feed (tag dupes). Received packets are tagged with what the ingester did with them. In memory: the "
             "newest 5000 lines since the last restart. Passcodes and unlock tokens are masked."),
    "app": ("Application", "This app's own log messages. In memory: the newest 2000 since the last restart."),
    "packets": ("Accepted packets", "Packets from registered mobiles that were stored as coverage. "
                "Type is the station that heard the mobile first."),
    "rejects": ("Rejected packets", "Packets from registered mobiles that failed a data-quality check."),
    "nodecfg": ("Node config", "Remote-config commands sent to igates/digis, with the replies."),
}
_RING_LOGS = {"aprs": logbuf.APRS, "app": logbuf.APP}
_DB_LOGS = {
    # table, row -> entry
    "packets": ("packets", lambda r: {
        "level": r["heard_by"] or "untraced", "tag": r["mobile"], "text": r["raw"],
        "note": f"igate {r['igate']}, {r['hops']} hop{'s' if r['hops'] != 1 else ''}"
                + (", duplicates feed" if r["dupe"] else "")}),
    "rejects": ("rejects", lambda r: {"level": r["reason"], "tag": r["source"] or "", "text": r["raw"], "note": ""}),
    "nodecfg": ("node_config_log", lambda r: {
        "level": r["result"], "tag": f"{r['login']} > {r['callsign']}" if r["login"] else r["callsign"],
        "text": f"{r['command']}  =>  {r['detail']}", "note": ""}),
}


@app.get("/api/admin/logs", dependencies=[Depends(auth.require_admin)])
def admin_logs():
    return [{"id": k, "name": n, "description": d} for k, (n, d) in LOG_SOURCES.items()]


@app.get("/api/admin/logs/{source}", dependencies=[Depends(auth.require_admin)])
def admin_log(source: str, after: int | None = Query(None, ge=0), limit: int = Query(1000, ge=1, le=5000)):
    if source in _RING_LOGS:
        return {"epoch": logbuf.EPOCH, "entries": _RING_LOGS[source].since(after, limit)}
    if source not in _DB_LOGS:
        raise HTTPException(404, "No such log")
    table, conv = _DB_LOGS[source]
    with db.session() as c:
        if after is None:
            rows = c.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()[::-1]
        else:
            rows = c.execute(f"SELECT * FROM {table} WHERE id > ? ORDER BY id LIMIT ?", (after, limit)).fetchall()
    return {"epoch": 0, "entries": [{"id": r["id"], "ts": r["ts"], **conv(r)} for r in rows]}
