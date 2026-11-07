"""FastAPI app: public map API, live feed, admin API, static pages."""
import asyncio
import fnmatch
import json
import logging
import math
import re
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from . import auth, config, db, grid, remotecfg
from .ingest import ingester

remote = remotecfg.RemoteConfig()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = config.BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
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
    import fnmatch
    return fnmatch.fnmatchcase(call, pattern)


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
            f"COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last "
            f"FROM packets WHERE {where} GROUP BY x, y, h, m",
            args,
        ).fetchall()
    # Every square carries both breakdowns so the tooltip can list who heard it
    # and which mobiles were there, whatever the color mode.
    cells: dict[tuple[int, int], list] = {}
    for r in rows:
        e = cells.setdefault((r["x"], r["y"]), [r["x"], r["y"], 0, 0, {}, {}, {}, r["first"]])
        e[2] += r["n"]
        e[3] = max(e[3], r["last"])
        e[5][r["h"]] = e[5].get(r["h"], 0) + r["n"]
        e[6][r["m"]] = e[6].get(r["m"], 0) + r["n"]
        e[7] = min(e[7], r["first"])
    for e in cells.values():
        e[4] = {"single": {}, "heard_by": e[5], "mobile": e[6]}[color]
    dlat, dlon = grid.cell_deg(cell)
    return {
        "cell_m": cell, "dlat": dlat, "dlon": dlon, "start": start, "end": end,
        "packets": sum(e[2] for e in cells.values()),
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
                f"SELECT COALESCE(heard_by,'(untraced)') AS k, COUNT(*) AS packets, "
                f"COUNT(DISTINCT {cx} || ',' || {cy}) AS cells, COUNT(DISTINCT mobile) AS mobiles, "
                f"MAX(ts) AS last FROM packets WHERE {where} GROUP BY k", args)
        }
        gated = dict(c.execute(
            f"SELECT igate, COUNT(*) FROM packets WHERE {where} GROUP BY igate", args).fetchall())
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
            f"SELECT mobile, COUNT(*) AS packets, COUNT(DISTINCT {cx} || ',' || {cy}) AS cells, "
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
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), 20)
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
        pattern = MOBILE_RE if self.kind == "mobile" else CALL_RE
        if not pattern.match(self.callsign):
            hint = " (mobiles may end in * or -* for any SSID)" if self.kind == "mobile" else ""
            raise HTTPException(422, f"Invalid callsign{hint}")
        if (self.lat is None) != (self.lon is None):
            raise HTTPException(422, "Give both lat and lon, or neither")
        if self.lat is not None and not (-90 <= self.lat <= 90 and -180 <= self.lon <= 180):
            raise HTTPException(422, "lat/lon out of range")


@app.get("/api/admin/stations", dependencies=[Depends(auth.require_admin)])
def admin_list_stations():
    with db.session() as c:
        return [dict(r) for r in c.execute("SELECT * FROM stations ORDER BY kind, callsign")]


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
        return dict(st), "mobile GLOB ?", [call]
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
        for m in [m for m in ingester.last if fnmatch.fnmatchcase(m, pattern)]:
            ingester.last.pop(m, None)
    logging.getLogger("admin").info("purged %d packets for %s", deleted, st["callsign"])
    return {"deleted": deleted}


@app.get("/api/admin/status", dependencies=[Depends(auth.require_admin)])
def admin_status():
    with db.session() as c:
        totals = dict(c.execute("SELECT COUNT(*) AS packets, MIN(ts) AS first, MAX(ts) AS last FROM packets").fetchone())
    return {"ingest": ingester.status(), "db": totals, "db_path": str(config.DB_PATH)}


@app.get("/api/admin/rejects", dependencies=[Depends(auth.require_admin)])
def admin_rejects(limit: int = Query(200, ge=1, le=2000)):
    with db.session() as c:
        return [dict(r) for r in c.execute("SELECT * FROM rejects ORDER BY id DESC LIMIT ?", (limit,))]


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
