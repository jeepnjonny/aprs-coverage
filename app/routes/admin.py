"""Admin API: stations, users, feed status and settings, logs."""
import logging
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator

from .. import auth, config, db, devicelog, ingest, logbuf
from ..ingest import ingester
from ..packets import mobile_matches
from .shared import (
    AdminStationIn, callsign_unique, STATION_FIELDS, station_values, _check_owner, _clean_profile, _insert_station, _station_rows, _user,
)

router = APIRouter()


# ── admin: stations ──────────────────────────────────────────────────────────
@router.get("/api/admin/stations", dependencies=[Depends(auth.require_admin)])
def admin_list_stations():
    return _station_rows()


@router.post("/api/admin/stations", dependencies=[Depends(auth.require_admin)])
def admin_create_station(body: AdminStationIn):
    body.check()
    with db.session() as c:
        _check_owner(c, body.owner_id)
        sid = _insert_station(c, body, owner_id=body.owner_id, status="approved")
    ingester.reload()
    return {"id": sid}


@router.put("/api/admin/stations/{sid}", dependencies=[Depends(auth.require_admin)])
def admin_update_station(sid: int, body: AdminStationIn):
    body.check()
    fields = list(STATION_FIELDS)
    args = station_values(body)
    with db.session() as c:
        if "owner_id" in body.model_fields_set:
            _check_owner(c, body.owner_id)
            fields.append("owner_id")
            args.append(body.owner_id)
        sets = ",".join(f"{f}=?" for f in fields)
        with callsign_unique(body.callsign):
            cur = c.execute(f"UPDATE stations SET {sets} WHERE id=?", (*args, sid))
        if cur.rowcount == 0:
            raise HTTPException(404, "No such station")
    ingester.reload()
    return {"ok": True}


@router.delete("/api/admin/stations/{sid}", dependencies=[Depends(auth.require_admin)])
def admin_delete_station(sid: int):
    with db.session() as c:
        if c.execute("DELETE FROM stations WHERE id=?", (sid,)).rowcount == 0:
            raise HTTPException(404, "No such station")
    ingester.reload()
    return {"ok": True}


@router.post("/api/admin/stations/{sid}/approve")
def admin_approve_station(sid: int, admin: dict = Depends(auth.require_admin)):
    return _set_station_status(sid, "approved", "", admin)


class RejectIn(BaseModel):
    note: str = ""


@router.post("/api/admin/stations/{sid}/reject")
def admin_reject_station(sid: int, body: RejectIn, admin: dict = Depends(auth.require_admin)):
    """The device stays, marked rejected with the reason, so its owner sees why
    and can fix and resubmit it, or delete it."""
    return _set_station_status(sid, "rejected", body.note.strip()[:devicelog.MAX_NOTE_LEN], admin)


def _set_station_status(sid: int, status: str, note: str, admin: dict) -> dict:
    with db.session() as c:
        st = c.execute("SELECT callsign FROM stations WHERE id=?", (sid,)).fetchone()
        if st is None:
            raise HTTPException(404, "No such station")
        c.execute("UPDATE stations SET status=?, review_note=? WHERE id=?", (status, note, sid))
    ingester.reload()
    logging.getLogger("admin").info("%s %s %s", admin["username"], status, st["callsign"])
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


@router.get("/api/admin/stations/{sid}/packets", dependencies=[Depends(auth.require_admin)])
def admin_count_station_packets(sid: int):
    with db.session() as c:
        _, where, args = _station_packets(c, sid)
        return {"count": c.execute(f"SELECT COUNT(*) FROM packets WHERE {where}", args).fetchone()[0]}


@router.delete("/api/admin/stations/{sid}/packets", dependencies=[Depends(auth.require_admin)])
def admin_purge_station_packets(sid: int):
    with db.session() as c:
        st, where, args = _station_packets(c, sid)
        deleted = c.execute(f"DELETE FROM packets WHERE {where}", args).rowcount
    if st["kind"] == "mobile":
        # Forget the last fix too, so a changed station isn't rejected as a
        # speed jump from its old position.
        pattern = st["callsign"].upper()
        for m in [m for m in list(ingester.last) if mobile_matches(m, pattern)]:
            ingester.last.pop(m, None)
    logging.getLogger("admin").info("purged %d packets for %s", deleted, st["callsign"])
    return {"deleted": deleted}


# ── admin: users ─────────────────────────────────────────────────────────────
@router.get("/api/admin/users", dependencies=[Depends(auth.require_admin)])
def admin_users():
    with db.session() as c:
        rows = c.execute(
            """SELECT u.*,
                      (SELECT COUNT(*) FROM stations s WHERE s.owner_id = u.id) AS devices,
                      (SELECT COUNT(*) FROM stations s WHERE s.owner_id = u.id AND s.status = 'pending') AS pending_devices
               FROM users u ORDER BY u.status = 'pending' DESC, u.username COLLATE NOCASE""").fetchall()
    return [auth.public_user(dict(r)) | {"can_config": bool(r["can_config"]), "created_at": r["created_at"],
                                         "last_login": r["last_login"], "devices": r["devices"],
                                         "pending_devices": r["pending_devices"]} for r in rows]


class UserEditIn(BaseModel):
    callsign: str | None = None
    name: str | None = None
    email: str | None = None
    role: str | None = None
    status: str | None = None
    can_config: bool | None = None


@router.put("/api/admin/users/{uid}")
def admin_edit_user(uid: int, body: UserEditIn, admin: dict = Depends(auth.require_admin)):
    """Approve (status active), disable, change role or remote-config access,
    or fix the profile. Changes apply on the user's next request."""
    with db.session() as c:
        old = _user(c, uid)
        new = old | {k: v for k, v in body.model_dump().items() if v is not None}
        if new["role"] not in auth.ROLES:
            raise HTTPException(422, f"role must be one of {', '.join(auth.ROLES)}")
        if new["status"] not in auth.STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(auth.STATUSES)}")
        call, name, email = _clean_profile(new["callsign"], new["name"], new["email"],
                                           need_call=new["role"] == "node_owner")
        was_admin = old["role"] == "admin" and old["status"] == "active"
        if was_admin and (new["role"] != "admin" or new["status"] != "active") and not auth.active_admins(c, uid):
            raise HTTPException(409, "That would leave no active admin")
        c.execute("UPDATE users SET callsign=?, name=?, email=?, role=?, status=?, can_config=? WHERE id=?",
                  (call, name, email, new["role"], new["status"], int(bool(new["can_config"])), uid))
        if new["status"] == "disabled":
            auth.end_user_sessions(c, uid)
    changed = {k: new[k] for k in ("role", "status", "can_config") if new[k] != old[k]}
    if changed:
        logging.getLogger("admin").info("%s changed user %s: %s", admin["username"], old["username"], changed)
    return {"ok": True}


class SetPasswordIn(BaseModel):
    new: str


@router.post("/api/admin/users/{uid}/password")
def admin_set_password(uid: int, body: SetPasswordIn, request: Request, admin: dict = Depends(auth.require_admin)):
    auth.check_password_rules(body.new)
    with db.session() as c:
        _user(c, uid)
        c.execute("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_password(body.new), uid))
        auth.end_user_sessions(c, uid, keep_token=request.cookies.get(auth.COOKIE) if uid == admin["id"] else None)
    return {"ok": True}


@router.delete("/api/admin/users/{uid}")
def admin_delete_user(uid: int, admin: dict = Depends(auth.require_admin)):
    """Also how a sign-up is rejected. Their pending and rejected devices go
    with them; approved ones stay, managed by the admins."""
    if uid == admin["id"]:
        raise HTTPException(409, "You can't delete your own account")
    with db.session() as c:
        u = _user(c, uid)
        if u["role"] == "admin" and u["status"] == "active" and not auth.active_admins(c, uid):
            raise HTTPException(409, "That would leave no active admin")
        c.execute("DELETE FROM stations WHERE owner_id=? AND status != 'approved'", (uid,))
        c.execute("UPDATE stations SET owner_id=NULL WHERE owner_id=?", (uid,))
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))
    ingester.reload()
    logging.getLogger("admin").info("%s deleted user %s", admin["username"], u["username"])
    return {"ok": True}


@router.get("/api/admin/status", dependencies=[Depends(auth.require_admin)])
def admin_status():
    with db.session() as c:
        totals = dict(c.execute(
            "SELECT COUNT(*) AS packets, COALESCE(SUM(dupe), 0) AS dupes, MIN(ts) AS first, MAX(ts) AS last "
            "FROM packets").fetchone())
    return {"ingest": ingester.status(), "db": totals, "db_path": str(config.DB_PATH)}


@router.get("/api/admin/rejects", dependencies=[Depends(auth.require_admin)])
def admin_rejects(limit: int = Query(10, ge=1, le=2000)):
    with db.session() as c:
        ingest.prune_rejects(c, int(time.time()))  # age out even when no new rejects arrive
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
        if v and not HOST_RE.fullmatch(v):
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
        if v and not LOGIN_RE.fullmatch(v):
            raise ValueError("login must be a callsign with an optional SSID, e.g. K7ABC-DF")
        return v


def _feeds_out() -> dict:
    from dataclasses import asdict
    return {"settings": asdict(ingester.feeds), "default_server": config.APRS_SERVER,
            "default_port": config.APRS_PORT, "login": config.APRS_LOGIN}


@router.get("/api/admin/feeds", dependencies=[Depends(auth.require_admin)])
def admin_feeds():
    return _feeds_out()


@router.put("/api/admin/feeds", dependencies=[Depends(auth.require_admin)])
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
    "rejects": ("Rejected packets", "Packets from registered mobiles that failed a data-quality check. "
                "Kept for 2 days."),
    "nodecfg": ("Node config", "Remote-config commands sent to igates/digis, with the replies."),
    "devices": ("Device uploads", "Device log uploads from apps (accepted, and refused with the reason), "
                "upload keys created and revoked, device notes, and background re-matching. In memory: the "
                "newest 2000 since the last restart."),
    "devicelog": ("Uploaded device log", "Lines stored from device log uploads: what each mobile transmitted "
                  "(tx, and whether it reached APRS-IS) and heard (rx, with signal strength)."),
}
_RING_LOGS = {"aprs": logbuf.APRS, "app": logbuf.APP, "devices": logbuf.DEVICES}
_DB_LOGS = {
    # table, row -> entry
    "packets": ("packets", lambda r: {
        "level": r["heard_by"] or "untraced", "tag": r["mobile"], "text": r["raw"],
        "note": f"igate {r['igate']}, {r['hops']} hop{'s' if r['hops'] != 1 else ''}"
                + (", duplicates feed" if r["dupe"] else "")}),
    "rejects": ("rejects", lambda r: {"level": r["reason"], "tag": r["source"] or "", "text": r["raw"], "note": ""}),
    "devicelog": ("device_log", lambda r: {
        "level": r["kind"], "tag": r["device"], "text": r["packet"],
        "note": ("not a position" if not r["is_pos"] else f"heard on APRS-IS (tx {r['txid']})" if r["txid"]
                 else f"not counted: identical to a packet APRS-IS passed under {devicelog.APRSIS_DUPE_S} s "
                      f"before (tx {r['repeat_of']}), so APRS-IS dropped it as a duplicate" if r["repeat_of"]
                 else "not heard") if r["kind"] == "tx" else
                ", ".join(x for x in (
                    (f"repeated by {r['via'] or 'an untraced digi'}" if r["echo"] else f"beacon of {r['via']}"),
                    f"RSSI {r['rssi']:g} dBm" if r["rssi"] is not None else "",
                    f"SNR {r['snr']:g} dB" if r["snr"] is not None else "") if x)}),
    "nodecfg": ("node_config_log", lambda r: {
        "level": r["result"], "tag": f"{r['login']} > {r['callsign']}" if r["login"] else r["callsign"],
        "text": f"{r['command']}  =>  {r['detail']}", "note": ""}),
}


@router.get("/api/admin/logs", dependencies=[Depends(auth.require_admin)])
def admin_logs():
    return [{"id": k, "name": n, "description": d} for k, (n, d) in LOG_SOURCES.items()]


@router.get("/api/admin/logs/{source}", dependencies=[Depends(auth.require_admin)])
def admin_log(source: str, after: int | None = Query(None, ge=0), limit: int = Query(1000, ge=1, le=5000)):
    if source in _RING_LOGS:
        return {"epoch": logbuf.EPOCH, "entries": _RING_LOGS[source].since(after, limit)}
    if source not in _DB_LOGS:
        raise HTTPException(404, "No such log")
    table, conv = _DB_LOGS[source]
    with db.session() as c:
        if table == "rejects":
            ingest.prune_rejects(c, int(time.time()))
        if after is None:
            rows = c.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()[::-1]
        else:
            rows = c.execute(f"SELECT * FROM {table} WHERE id > ? ORDER BY id LIMIT ?", (after, limit)).fetchall()
        entries = [{"id": r["id"], "ts": r["ts"], **conv(r)} for r in rows]
        if source == "devicelog":  # say why each unheard transmission wasn't matched
            for e, r in zip(entries, rows):
                if r["kind"] == "tx" and r["is_pos"] and not r["txid"] and not r["repeat_of"]:
                    e["note"] = devicelog.explain_unheard(c, r["device"], r["ts"], r["info"])
    return {"epoch": 0, "entries": entries}
