"""Helpers the route modules share: query parsing, station validation and listing."""
import colorsys
import re
import sqlite3
import time

from fastapi import HTTPException
from pydantic import BaseModel, field_validator

from .. import auth, config, db
from ..ingest import ingester

# ── query helpers ────────────────────────────────────────────────────────────
UNTRACED, OTHER = "__untraced__", "__other__"


def _infra_calls(c) -> list[str]:
    return [r["callsign"] for r in c.execute(
        "SELECT callsign FROM stations WHERE kind != 'mobile' AND status='approved'")]


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


MAX_TS = 4_102_444_800  # 2100-01-01: bounds user timestamps so they fit SQLite and datetime


def _time_range(start: int | None, end: int | None, default_span: int = 86400) -> tuple[int, int]:
    """Resolve a query's [start, end] in unix seconds. Values are clamped to
    0 .. MAX_TS, so an absurd number can't overflow SQLite or datetime."""
    now = int(time.time())
    end = min(max(end, 0), MAX_TS) if end is not None else now
    start = min(max(start, 0), MAX_TS) if start is not None else max(end - default_span, 0)
    if start > end:
        raise HTTPException(400, "start must be before end")
    return start, end


def _cell_size(cell: int) -> int:
    if cell not in config.CELL_SIZES_M:
        raise HTTPException(400, f"cell must be one of {config.CELL_SIZES_M}")
    return cell


def _ts(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


# ── stations: validation and listing ─────────────────────────────────────────
CALL_RE = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,2})?$")
MOBILE_RE = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,2}|-\*|\*)?$")
# Object tracker: SENDER:OBJECT, e.g. K7SWI:CAR01 or K7SWI-*:CAR*. Object names
# are up to 9 characters; spaces are not allowed here (trailing pad is stripped).
OBJECT_RE = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,2}|-\*|\*)?:[A-Z0-9_./#*-]{1,9}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def is_reddish(color: str) -> bool:
    """Red is reserved on the map for the outline of squares where beacons were
    lost, so no station may be drawn red. Same test as isReddish() in map.js."""
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, lum, sat = colorsys.rgb_to_hls(r, g, b)
    hue = h * 360
    return (hue >= 340 or hue <= 18) and sat >= 0.35 and 0.2 <= lum <= 0.8
# Device hardware, for the dropdown and the map's filter: the boards the LoRa
# FieldOps firmware builds for (KJ7NYE/LoRa_FieldOps_APRS_Tracker), then "other".
# Keys are stored, so keep them stable; labels can change freely.
HARDWARE = {
    "heltec_t114": "Heltec T114",
    "heltec_v3": "Heltec WiFi LoRa 32 V3",
    "heltec_tracker": "Heltec Wireless Tracker",
    "tbeam": "LilyGo T-Beam",
    "tbeam_1w": "LilyGo T-Beam 1W",
    "lilygo_t3": "LilyGo T3",
    "lilygo_t3s3": "LilyGo T3-S3",
    "loranger_v1": "LoRanger V1",
    "loranger_v2": "LoRanger V2",
    "other": "Other",
}


class StationIn(BaseModel):
    callsign: str
    kind: str
    label: str = ""
    notes: str = ""
    color: str | None = None
    hardware: str = ""
    lat: float | None = None
    lon: float | None = None
    active: bool = True

    @field_validator("callsign")
    @classmethod
    def _call(cls, v):
        return v.strip().upper()

    @field_validator("label", "notes")
    @classmethod
    def _text(cls, v):
        v = v.strip()
        if len(v) > 200:
            raise ValueError("label and notes are limited to 200 characters")
        return v

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        if v in (None, ""):
            return None
        if not COLOR_RE.fullmatch(v):
            raise ValueError("color must be #rrggbb")
        if is_reddish(v):
            raise ValueError("red is reserved on the map for squares where beacons were lost; pick another color")
        return v.lower()

    @field_validator("hardware")
    @classmethod
    def _hardware(cls, v):
        v = (v or "").strip()
        if v and v not in HARDWARE:
            raise ValueError("unknown hardware type")
        return v

    def check(self):
        if self.kind not in ("mobile", "igate", "digi", "digi_igate"):
            raise HTTPException(422, "kind must be mobile, igate, digi or digi_igate")
        if self.kind == "mobile":
            ok = MOBILE_RE.fullmatch(self.callsign) or OBJECT_RE.fullmatch(self.callsign)
        else:
            ok = CALL_RE.fullmatch(self.callsign)
        if not ok:
            hint = (" (mobiles may end in * or -* for any SSID; object trackers are"
                    " SENDER:OBJECT, e.g. K7SWI:CAR01)") if self.kind == "mobile" else ""
            raise HTTPException(422, f"Invalid callsign {self.callsign or '(empty)'}{hint}")
        if self.kind == "mobile":
            self.lat = self.lon = None
        if (self.lat is None) != (self.lon is None):
            raise HTTPException(422, "Give both lat and lon, or neither")
        if self.lat is not None and not (-90 <= self.lat <= 90 and -180 <= self.lon <= 180):
            raise HTTPException(422, "lat/lon out of range")


class AdminStationIn(StationIn):
    owner_id: int | None = None  # only changed when the request includes it


def _insert_station(c, body: StationIn, *, owner_id: int | None, status: str) -> int:
    try:
        cur = c.execute(
            "INSERT INTO stations(callsign,kind,label,notes,color,hardware,lat,lon,active,owner_id,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (body.callsign, body.kind, body.label, body.notes, body.color, body.hardware, body.lat, body.lon,
             int(body.active), owner_id, status),
        )
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"{body.callsign} is already registered")
        raise
    return cur.lastrowid


def _check_owner(c, owner_id: int | None) -> None:
    if owner_id is not None and not c.execute("SELECT 1 FROM users WHERE id=?", (owner_id,)).fetchone():
        raise HTTPException(422, "No such user")


GATED_WINDOW_S = 7 * 86400


def _station_rows(where: str = "1", args: tuple = ()) -> list[dict]:
    """Stations with their owner's username. Igates and digis get `own_server`,
    the name of your aprsc (from the duplicates feed's login reply), to compare
    with their `is_server`. Igates also get `gated`: packets from registered
    mobiles they gated in the last GATED_WINDOW_S, split by feed. "main" copies
    are the ones APRS-IS passed on (this igate was first); "dupe" copies came
    from the aprsc duplicates feed (another igate got there first)."""
    since = int(time.time()) - GATED_WINDOW_S
    with db.session() as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT s.*, u.username AS owner FROM stations s LEFT JOIN users u ON u.id = s.owner_id "
            f"WHERE {where} ORDER BY s.kind, s.callsign", args)]
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


def _user(c, uid: int) -> dict:
    row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such user")
    return dict(row)


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_profile(callsign: str, name: str, email: str, need_call: bool) -> tuple[str, str, str]:
    call = callsign.strip().upper()
    if (call or need_call) and not auth.BASE_CALL_RE.fullmatch(call):
        raise HTTPException(422, "Callsign must be a base callsign without an SSID, e.g. K7ABC")
    name, email = name.strip(), email.strip()
    if len(name) > 80:
        raise HTTPException(422, "Name is limited to 80 characters")
    if email and (len(email) > 120 or not EMAIL_RE.fullmatch(email)):
        raise HTTPException(422, "That email address doesn't look right")
    return call, name, email
