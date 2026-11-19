"""Public, read-only API: map data, live feed, comparisons, device notes."""
import asyncio
import json
import math
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .. import auth, compare, config, db, devicelog, grid, logbuf, reliability
from ..ingest import ingester
from ..packets import mobile_matches
from .shared import HARDWARE, MAX_TS, _cell_size, _csv, _time_range, _ts, _where

router = APIRouter()


# ── public API ───────────────────────────────────────────────────────────────
@router.get("/api/config")
def api_config():
    return {
        "hardware": HARDWARE,
        "cell_sizes": config.CELL_SIZES_M,
        "grid_ref_lat": config.GRID_REF_LAT,
        "default_bounds": [[43.50, -116.68], [43.92, -116.12]],  # Nampa/Meridian/Boise/Emmett
    }


@router.get("/api/stations")
def api_stations():
    with db.session() as c:
        rows = c.execute("SELECT * FROM stations WHERE active=1 AND status='approved' ORDER BY kind, callsign").fetchall()
        heard = dict(c.execute("SELECT heard_by, MAX(ts) FROM packets WHERE heard_by IS NOT NULL GROUP BY heard_by").fetchall())
        gated = dict(c.execute("SELECT igate, MAX(ts) FROM packets GROUP BY igate").fetchall())
        mob = dict(c.execute("SELECT mobile, MAX(ts) FROM packets GROUP BY mobile").fetchall())
    out = []
    for r in rows:
        call = r["callsign"]
        lat, lon = db.station_pos(r)
        if r["kind"] == "mobile":
            last = max((t for m, t in mob.items() if mobile_matches(m, call)), default=None)
        else:
            last = max(filter(None, [heard.get(call), gated.get(call)]), default=None)
        out.append({
            "callsign": call, "kind": r["kind"], "label": r["label"], "color": r["color"],
            "hardware": r["hardware"], "lat": lat, "lon": lon, "last_beacon": r["last_beacon"], "last_heard": last,
        })
    return out


@router.get("/api/mobiles")
def api_mobiles():
    """Concrete mobile callsigns seen in the data (wildcard patterns expand here)."""
    with db.session() as c:
        return [r[0] for r in c.execute("SELECT DISTINCT mobile FROM packets ORDER BY mobile")]


@router.get("/api/coverage")
def api_coverage(
    start: int | None = None,
    end: int | None = None,
    cell: int = 1000,
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
        e = cells.setdefault((r["x"], r["y"]), [r["x"], r["y"], totals.get((r["x"], r["y"]), 0), 0, {}, {}, {}, r["first"]])
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


@router.get("/api/reliability")
def api_reliability(
    start: int | None = None,
    end: int | None = None,
    cell: int = 1000,
    mobiles: str | None = None,
    heard_by: str | None = None,
    match: str = Query("first", pattern="^(first|any)$"),
    estimates: bool = True,
):
    """Per square, the share of beacons sent from it that reached APRS-IS:
    exact from uploaded transmit logs, estimated from gaps between heard
    packets for other trackers (estimates=false for exact only). With a
    station filter, only those stations' hearing counts."""
    start, end = _time_range(start, end)
    cell = _cell_size(cell)
    with db.session() as c:
        mob, hb = _csv(mobiles), _csv(heard_by)
        heard_ok = None
        if hb is not None:
            # a little past the end: a beacon logged just before it may arrive just after
            where, args = _where(c, start, end + 600, mob, hb, match)
            heard_ok = {r[0] for r in c.execute(f"SELECT DISTINCT txid FROM packets WHERE {where}", args)}
        return reliability.reliability(c, start, end, cell, mob, heard_ok, estimates)


@router.get("/api/failures")
def api_failures(
    start: int | None = None,
    end: int | None = None,
    cell: int = 1000,
    mobiles: str | None = None,
):
    """Squares with beacons a tracker logged sending (uploaded transmit logs)
    that never reached APRS-IS. Only from trackers that upload a log; squares
    no such tracker visited say nothing either way. Beacons sent in the last
    couple of minutes are left out until their packets have had time to arrive."""
    start, end = _time_range(start, end)
    cell = _cell_size(cell)
    cx, cy = grid.sql_cell_exprs(cell)
    settled = int(time.time()) - devicelog.MATCH_AFTER_S - 30
    clauses, args = ["kind='tx'", "is_pos=1", "repeat_of IS NULL", "ts >= ?", "ts <= ?"], [start, end]
    mob = _csv(mobiles)
    if mob is not None:
        clauses.append(f"device IN ({db.placeholders(mob)})" if mob else "0")
        args += mob
    where = " AND ".join(clauses)
    with db.session() as c:
        rows = c.execute(
            f"SELECT {cx} AS x, {cy} AS y, SUM(txid IS NULL AND ts <= ?) AS failed, "
            f"SUM(txid IS NOT NULL OR ts <= ?) AS sent, MAX(CASE WHEN txid IS NULL THEN ts END) AS last "
            f"FROM device_log WHERE {where} GROUP BY x, y HAVING failed > 0",
            [settled, settled, *args]).fetchall()
    dlat, dlon = grid.cell_deg(cell)
    return {
        "cell_m": cell, "dlat": dlat, "dlon": dlon, "start": start, "end": end,
        "failed": sum(r["failed"] for r in rows),
        # [x, y, failed, logged, last failed ts]
        "cells": [[r["x"], r["y"], r["failed"], r["sent"], r["last"]] for r in rows],
    }


@router.get("/api/stats")
def api_stats(
    start: int | None = None,
    end: int | None = None,
    cell: int = 1000,
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
        infra = c.execute("SELECT * FROM stations WHERE kind != 'mobile' AND status='approved'").fetchall()

        ky = grid.M_PER_DEG_LAT / 1000
        infra_rows = []
        for s in infra:
            call = s["callsign"]
            lat, lon = db.station_pos(s)
            max_km = max_at = None
            if lat is not None and lon is not None and call in direct:
                kx = grid.M_PER_DEG_LON_EQ / 1000 * math.cos(math.radians(lat))
                # SQLite returns the bare lat/lon columns from the row holding the MAX.
                far = c.execute(
                    f"SELECT MAX(((lat-?)*?)*((lat-?)*?) + ((lon-?)*?)*((lon-?)*?)) AS d2, lat, lon "
                    f"FROM packets WHERE {where} AND heard_by=?",
                    [lat, ky, lat, ky, lon, kx, lon, kx, *args, call],
                ).fetchone()
                if far["d2"] is not None:
                    max_km = round(math.sqrt(far["d2"]), 1)
                    max_at = [far["lat"], far["lon"]]
            d = direct.pop(call, {})
            infra_rows.append({
                "callsign": call, "kind": s["kind"], "label": s["label"], "registered": True,
                "heard_direct": d.get("packets", 0), "cells": d.get("cells", 0),
                "mobiles": d.get("mobiles", 0), "gated": gated.get(call, 0),
                "max_range_km": max_km, "max_range_at": max_at, "last_heard": d.get("last"),
            })
        for k, d in direct.items():  # unregistered first hops and untraced
            infra_rows.append({
                "callsign": k, "kind": None, "label": "", "registered": False,
                "heard_direct": d["packets"], "cells": d["cells"], "mobiles": d["mobiles"],
                "gated": gated.get(k, 0), "max_range_km": None, "max_range_at": None, "last_heard": d["last"],
            })
        infra_rows.sort(key=lambda r: (-r["heard_direct"], r["callsign"]))

        mob_rows = [dict(r) for r in c.execute(
            f"SELECT mobile, COUNT(DISTINCT txid) AS packets, COUNT(DISTINCT {cx} || ',' || {cy}) AS cells, "
            f"MAX(ts) AS last FROM packets WHERE {where} GROUP BY mobile ORDER BY packets DESC", args)]
    return {"infra": infra_rows, "mobiles": mob_rows}


@router.get("/api/status")
def api_status():
    s = ingester.status()
    return {k: s[k] for k in ("enabled", "connected", "last_packet_ts")}


# Live streams hold a connection open for up to an hour, and the app runs as
# one worker, so cap how many one address (and everyone together) may hold.
LIVE_PER_IP, LIVE_TOTAL = 6, 300
_live_by_ip: dict[str, int] = defaultdict(int)


@router.get("/api/live")
async def api_live(request: Request):
    ip = auth.client_ip(request)
    if _live_by_ip[ip] >= LIVE_PER_IP or sum(_live_by_ip.values()) >= LIVE_TOTAL:
        if not _live_by_ip[ip]:
            del _live_by_ip[ip]
        raise HTTPException(429, "Too many live connections")
    _live_by_ip[ip] += 1  # this handler runs on the event loop, so no lock needed
    released = False

    def release():  # from the stream's end or the response's background task, whichever runs
        nonlocal released
        if not released:
            released = True
            _live_by_ip[ip] -= 1
            if _live_by_ip[ip] <= 0:
                del _live_by_ip[ip]

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
            release()

    return StreamingResponse(stream(), media_type="text/event-stream", background=BackgroundTask(release),
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── comparisons (public, read-only) ─────────────────────────────────────────
def _compare_range(start, end):
    return _time_range(start, end, default_span=90 * 86400)


def _two(a: str, b: str) -> tuple[str, str]:
    a, b = a.strip().upper(), b.strip().upper()
    if not a or not b:
        raise HTTPException(422, "Pick two stations")
    if a == b:
        raise HTTPException(422, "Pick two different stations")
    return a, b


@router.get("/api/compare/mobiles")
def api_compare_mobiles(
    a: str, b: str,
    start: int | None = None, end: int | None = None,
    bucket: str = Query("week", pattern="^(day|week|month)$"),
    tz: int = Query(0, ge=-840, le=840, description="viewer's offset from UTC, minutes east"),
    together: bool = False,
    sep_m: float = Query(200, ge=20, le=5000),
    interval: int | None = Query(None, ge=5, le=3600, description="beacon interval, s; omit to estimate"),
):
    a, b = _two(a, b)
    start, end = _compare_range(start, end)
    with db.session() as c:
        return compare.compare_mobiles(c, a, b, start, end, bucket, tz * 60, together, sep_m, interval)


@router.get("/api/compare/nodes")
def api_compare_nodes(
    a: str, b: str,
    start: int | None = None, end: int | None = None,
    bucket: str = Query("week", pattern="^(day|week|month)$"),
    tz: int = Query(0, ge=-840, le=840),
    match: str = Query("first", pattern="^(first|any)$"),
    mobiles: str | None = None,
):
    a, b = _two(a, b)
    start, end = _compare_range(start, end)
    with db.session() as c:
        return compare.compare_nodes(c, a, b, start, end, bucket, tz * 60, match, _csv(mobiles) or None)


@router.get("/api/compare/change")
def api_compare_change(
    station: str,
    at: int = Query(..., ge=0, le=MAX_TS),
    days: int | None = Query(7, ge=1, le=365, description="days on each side; omit for 'until now'"),
    until_now: bool = False,
    same_route: bool = True,
    cell: int = 500,
    tz: int = Query(0, ge=-840, le=840),
    match: str = Query("first", pattern="^(first|any)$"),
):
    """One station before and after a dated change. For a mobile: share of its
    beacons that reached APRS-IS. For an igate/digi: share of the
    transmissions anyone heard that it heard."""
    station = station.strip().upper()
    now = int(time.time())
    if not station:
        raise HTTPException(422, "Pick a station")
    if not 0 < at < now:
        raise HTTPException(422, "The change must be in the past")
    cell = _cell_size(cell)
    with db.session() as c:
        return compare.compare_change(c, station, at, None if until_now else days, cell, tz * 60,
                                      same_route, match, now)


class EventIn(BaseModel):
    device: str
    ts: int = Field(ge=0, le=MAX_TS)
    text: str


@router.get("/api/events")
def list_events(device: str, limit: int = Query(50, ge=1, le=500)):
    """A device's notes, newest first (public, like the comparisons)."""
    with db.session() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, device, ts, text, source, user_id FROM device_events WHERE device=? ORDER BY ts DESC LIMIT ?",
            (device.strip().upper(), limit))]


@router.post("/api/events")
def add_event(body: EventIn, user: dict = Depends(auth.require_active)):
    """A dated note on one of your devices (antenna swap, moved, new
    firmware), shown on comparisons that include it."""
    device, text = body.device.strip().upper(), body.text.strip()
    if not text or len(text) > devicelog.MAX_NOTE_LEN:
        raise HTTPException(422, f"Note: 1 to {devicelog.MAX_NOTE_LEN} characters")
    with db.session() as c:
        try:
            devicelog.device_kind(c, user, device)
        except devicelog.UploadError as e:
            raise HTTPException(e.status, str(e))
        cur = c.execute("INSERT OR IGNORE INTO device_events(device, ts, text, source, user_id) "
                        "VALUES(?,?,?,'web',?)", (device, body.ts, text, user["id"]))
    if cur.rowcount:
        logbuf.DEVICES.add("note", f"{user['username']} {device}", f"added note for {_ts(body.ts)}: {text}")
    return {"id": cur.lastrowid if cur.rowcount else None}


@router.delete("/api/events/{eid}")
def delete_event(eid: int, user: dict = Depends(auth.require_active)):
    with db.session() as c:
        ev = c.execute("SELECT * FROM device_events WHERE id=?", (eid,)).fetchone()
        if ev is None or (user["role"] != "admin" and ev["user_id"] != user["id"]):
            raise HTTPException(404, "No such note")
        c.execute("DELETE FROM device_events WHERE id=?", (eid,))
    logbuf.DEVICES.add("note", f"{user['username']} {ev['device']}", f"deleted note for {_ts(ev['ts'])}: {ev['text']}")
    return {"ok": True}
