"""Remote node config (the "Node config" tab)."""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, config, db, remotecfg

router = APIRouter()


remote = remotecfg.RemoteConfig()


# ── remote node config ("Node config" tab) ──────────────────────────────────
# Admins can configure every node and send from any callsign. A node owner an
# admin has allowed can configure only their own nodes, sending from their own
# callsign (any SSID). The sending callsign comes with every request (the page
# keeps it in the browser); the server never stores it, and computes its
# passcode itself.
def _config_node(callsign: str, user: dict) -> str:
    """Only active, approved igates/digis, and for a node owner only their own."""
    call = callsign.strip().upper()
    with db.session() as c:
        row = c.execute(
            "SELECT callsign, owner_id FROM stations WHERE callsign=? AND kind != 'mobile' AND active=1 "
            "AND status='approved'", (call,)
        ).fetchone()
    if row is None or (user["role"] != "admin" and row["owner_id"] != user["id"]):
        raise HTTPException(404, f"{call} is not an active registered igate or digi of yours"
                            if user["role"] != "admin" else f"{call} is not an active registered igate or digi")
    return row["callsign"]


def _login(login: str, user: dict) -> str:
    """Refuse a sending callsign that a station here is already logged in to
    APRS-IS with: the server would drop that station's connection. A node
    owner must send from their own callsign."""
    with db.session() as c:
        reserved = {r[0] for r in c.execute("SELECT callsign FROM stations WHERE kind != 'mobile'")}
    if config.APRS_LOGIN:
        reserved.add(config.APRS_LOGIN)
    try:
        call = remotecfg.check_login(login, reserved)
    except remotecfg.CommandError as e:
        raise HTTPException(e.status, str(e))
    if user["role"] != "admin" and call.split("-", 1)[0] != user["callsign"]:
        raise HTTPException(403, f"Send from your own callsign, {user['callsign'] or '(none set)'}, "
                                 "with any SSID that nothing else is logged in with")
    return call


async def _remote_call(coro):
    try:
        return await coro
    except remotecfg.CommandError as e:
        raise HTTPException(e.status, str(e))


LOG_ROWS = 10  # node-config activity rows shown


@router.get("/api/remote")
def remote_overview(login: str | None = None, user: dict = Depends(auth.require_config)):
    admin = user["role"] == "admin"
    with db.session() as c:
        nodes = [dict(r) for r in c.execute(
            f"""SELECT s.callsign, s.kind, s.label, n.fields, n.updated_at FROM stations s
               LEFT JOIN node_config n ON n.callsign = s.callsign
               WHERE s.kind != 'mobile' AND s.active=1 AND s.status='approved'
               {'' if admin else 'AND s.owner_id = ?'} ORDER BY s.callsign""",
            () if admin else (user["id"],))]
        calls = [n["callsign"] for n in nodes]
        if admin:
            logs = c.execute("SELECT * FROM node_config_log ORDER BY id DESC LIMIT ?", (LOG_ROWS,)).fetchall()
        elif calls:
            logs = c.execute(f"SELECT * FROM node_config_log WHERE callsign IN ({','.join('?' * len(calls))}) "
                             "ORDER BY id DESC LIMIT ?", (*calls, LOG_ROWS)).fetchall()
        else:
            logs = []
    for n in nodes:
        n["fields"] = json.loads(n["fields"]) if n["fields"] else {}
    login = (login or "").strip().upper() or None
    status = remote.status(login)
    if not admin:
        # Don't show a node owner other people's callsigns or nodes.
        if status["connection"]["login"] != login:
            status["connection"] = {"login": None, "server_line": ""}
        if status["busy"] and status["busy"] not in calls:
            status["busy"] = "another node"
    return {"status": status, "fields": remotecfg.FIELDS, "nodes": nodes,
            "log": [dict(r) for r in logs], "own_callsign": None if admin else user["callsign"]}


@router.delete("/api/remote/log")
def remote_log_clear(user: dict = Depends(auth.require_config)):
    """Clear the activity history: all of it for an admin, or the entries for
    a node owner's own nodes."""
    with db.session() as c:
        if user["role"] == "admin":
            n = c.execute("DELETE FROM node_config_log").rowcount
        else:
            n = c.execute("""DELETE FROM node_config_log WHERE callsign IN
                             (SELECT callsign FROM stations WHERE owner_id = ? AND kind != 'mobile')""",
                          (user["id"],)).rowcount
    return {"cleared": n}


class RemoteIn(BaseModel):
    login: str


class RemoteReadIn(RemoteIn):
    codes: list[str] | None = None


class RemoteUnlockIn(RemoteIn):
    token: str


class RemoteWriteIn(RemoteIn):
    fields: dict[str, str | int | float]


@router.post("/api/remote/{callsign}/read")
async def remote_read(callsign: str, body: RemoteReadIn, user: dict = Depends(auth.require_config)):
    return await _remote_call(remote.read(_login(body.login, user), _config_node(callsign, user), body.codes))


@router.post("/api/remote/{callsign}/unlock")
async def remote_unlock(callsign: str, body: RemoteUnlockIn, user: dict = Depends(auth.require_config)):
    return await _remote_call(remote.unlock(_login(body.login, user), _config_node(callsign, user), body.token))


@router.post("/api/remote/{callsign}/write")
async def remote_write(callsign: str, body: RemoteWriteIn, user: dict = Depends(auth.require_config)):
    return await _remote_call(remote.write(_login(body.login, user), _config_node(callsign, user), body.fields))
