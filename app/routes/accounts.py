"""Logging in, sign-up, your own account, devices, upload keys and device-log uploads."""
import logging
import re
import sqlite3
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import auth, config, db, devicelog, logbuf
from ..ingest import ingester
from .shared import (
    callsign_unique, DETAIL_FIELDS, STATION_FIELDS, station_values, StationIn, _clean_profile, _insert_station, _station_rows, _user,
)

router = APIRouter()


# ── device log uploads (optional; an app sends them with an API key) ────────
class KeyIn(BaseModel):
    name: str = ""


@router.get("/api/account/keys")
def list_keys(user: dict = Depends(auth.require_active)):
    with db.session() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, name, prefix, created_at, last_used FROM api_keys WHERE user_id=? ORDER BY id",
            (user["id"],))]


@router.post("/api/account/keys")
def create_key(body: KeyIn, user: dict = Depends(auth.require_active)):
    """The key is returned once; only its hash is stored."""
    name = body.name.strip()[:60]
    key = devicelog.new_key()
    with db.session() as c:
        if c.execute("SELECT COUNT(*) FROM api_keys WHERE user_id=?", (user["id"],)).fetchone()[0] >= devicelog.MAX_KEYS:
            raise HTTPException(422, f"At most {devicelog.MAX_KEYS} keys. Revoke one first.")
        cur = c.execute("INSERT INTO api_keys(user_id, name, key_hash, prefix) VALUES(?,?,?,?)",
                        (user["id"], name, devicelog.key_hash(key), key[:10]))
    logging.getLogger("admin").info("%s created upload key %s", user["username"], key[:10])
    logbuf.DEVICES.add("key", user["username"], f"created upload key {key[:10]}…" + (f" ({name})" if name else ""))
    return {"id": cur.lastrowid, "key": key, "prefix": key[:10], "name": name}


@router.delete("/api/account/keys/{kid}")
def revoke_key(kid: int, user: dict = Depends(auth.require_active)):
    with db.session() as c:
        k = c.execute("SELECT prefix, name FROM api_keys WHERE id=? AND user_id=?", (kid, user["id"])).fetchone()
        if k is None:
            raise HTTPException(404, "No such key")
        c.execute("DELETE FROM api_keys WHERE id=?", (kid,))
    logbuf.DEVICES.add("key", user["username"], f"revoked upload key {k['prefix']}…" + (f" ({k['name']})" if k["name"] else ""))
    return {"ok": True}


class DeviceLogIn(BaseModel):
    device: str
    app: str = ""
    entries: list[dict]


@router.post("/api/devicelog")
def upload_device_log(body: DeviceLogIn, request: Request):
    """Upload a device's own log: what it transmitted (tx), what it heard (rx)
    and dated notes (event). Authenticated with an API key from the account
    page, sent as 'Authorization: Bearer <key>'. See README, Device log uploads."""
    authz = request.headers.get("authorization", "")
    key = authz[7:].strip() if authz[:7].lower() == "bearer " else ""
    ip = auth.client_ip(request)
    app_note = body.app.strip()[:80]
    with db.session() as c:
        user = devicelog.user_for_key(c, key, int(time.time())) if key else None
        if user is None:
            why = "no key" if not key else f"unknown or revoked key {key[:10]}…"
            logbuf.DEVICES.add("refused", ip, f"upload for {body.device.strip().upper()[:40] or '(no device)'}: {why}", app_note)
            raise HTTPException(401, "A valid API key is required (Authorization: Bearer <key>)")
        tag = f"{user['username']} {body.device.strip().upper()}"
        try:
            res = devicelog.upload(c, user, body.device, body.entries)
        except devicelog.UploadError as e:
            logbuf.DEVICES.add("refused", tag, f"upload of {len(body.entries)} entries: {e}", app_note)
            raise HTTPException(e.status, str(e))
    text = (f"{len(body.entries)} entries: {res['accepted']} new, {res['duplicates']} already stored, "
            f"{res['ignored']} ignored, {len(res['errors'])} with errors")
    if res["tx"]:
        text += f"; {res['heard']} of {res['tx']} new transmissions heard on APRS-IS"
    errs = "; ".join(f"#{e['index']}: {e['error']}" for e in res["errors"][:3])
    logbuf.DEVICES.add("upload", tag, text, " | ".join(x for x in (app_note, errs) if x))
    return res


@router.get("/api/account/log-devices")
def log_devices(user: dict = Depends(auth.require_active)):
    """Approved mobiles this user can upload a log file for (all of them for
    an admin). A pattern registration (K7ABC-*) is listed as is; the upload
    names the concrete callsign."""
    admin = user["role"] == "admin"
    with db.session() as c:
        return [r[0] for r in c.execute(
            "SELECT callsign FROM stations WHERE kind='mobile' AND status='approved' "
            f"{'' if admin else 'AND owner_id=?'} ORDER BY callsign", () if admin else (user["id"],))]


class LogFileIn(BaseModel):
    device: str
    name: str = ""
    text: str


@router.post("/api/account/devicelog-file")
def upload_log_file(body: LogFileIn, user: dict = Depends(auth.require_active)):
    """A transmit log copied off a tracker's SD card (CSV), uploaded from the
    account page. Stored like an app's upload; see devicelog.parse_log_file."""
    device = body.device.strip().upper()
    tag = f"{user['username']} {device}"
    fname = body.name.strip()[:80]
    if len(body.text.encode()) > devicelog.MAX_FILE_BYTES:
        raise HTTPException(413, f"At most {devicelog.MAX_FILE_BYTES // 2**20} MB per file")
    with db.session() as c:
        try:
            res = devicelog.upload_file(c, user, device, body.text)
        except devicelog.UploadError as e:
            logbuf.DEVICES.add("refused", tag, f"log file {fname or '(unnamed)'}: {e}", "file")
            raise HTTPException(e.status, str(e))
    text = (f"log file {fname or '(unnamed)'}: {res['rows']} rows, {res['accepted']} new, "
            f"{res['duplicates']} already stored, {res['ignored']} ignored, {res['skipped']} skipped, "
            f"{res['rejected']} with errors")
    if res["tx"]:
        text += f"; {res['heard']} of {res['tx']} new transmissions heard on APRS-IS"
    errs = "; ".join(f"line {e['line']}: {e['error']}" for e in res["errors"][:3])
    logbuf.DEVICES.add("upload", tag, text, " | ".join(x for x in ("file", errs) if x))
    return res


# ── accounts: login, sign-up, your own account and devices ───────────────────
_PREFIX_OK = re.compile(r"^(/[A-Za-z0-9._~-]+)+/?$")


def _cookie_path(request: Request) -> str:
    """Scope the session cookie to the URL prefix nginx serves the app under
    (sent as X-Forwarded-Prefix, e.g. /aprs-coverage), so it is not sent to the
    other apps on the same host. Without the header (local runs) it is '/'."""
    prefix = request.headers.get("x-forwarded-prefix", "").strip()
    if not _PREFIX_OK.match(prefix):
        return "/"
    return prefix.rstrip("/") + "/"


def _start_session(request: Request, response: Response, user_id: int) -> None:
    token = auth.create_session(user_id)
    response.set_cookie(auth.COOKIE, token, max_age=config.SESSION_DAYS * 86400, httponly=True,
                        secure=config.COOKIE_SECURE, samesite="strict", path=_cookie_path(request))


MAX_DEVICES = 20  # per user
MAX_PENDING_ACCOUNTS = 25  # sign-ups pause while this many wait for approval
SIGNUPS_PER_IP, SIGNUP_WINDOW = 5, 3600
_signups = auth.SlidingWindow(SIGNUP_WINDOW)
# Sign-ups run in worker threads; the per-IP and pending-account limits are
# check-then-act, so one sign-up at a time (they're rare, and slow anyway).
_signup_lock = threading.Lock()


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = auth.client_ip(request)
    ukey = "u:" + body.username.strip().lower()
    auth.check_rate_limit(ip, ukey)
    u = auth.authenticate(body.username, body.password)
    if u is None:
        auth.record_failure(ip, ukey)
        if not auth.has_admin():
            raise HTTPException(503, "No admin account yet. On the server run: python -m app.manage set-password")
        raise HTTPException(401, "Wrong username or password")
    if u["status"] == "disabled":
        raise HTTPException(403, "This account is disabled")
    _start_session(request, response, u["id"])
    return {"user": auth.public_user(dict(u))}


@router.post("/api/logout")
def logout(request: Request, response: Response):
    auth.destroy_session(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, path=_cookie_path(request))
    return {"ok": True}


@router.get("/api/me")
def me(request: Request):
    u = auth.current_user(request)
    return {"user": auth.public_user(u) if u else None}


class SignupIn(BaseModel):
    username: str
    password: str
    callsign: str
    name: str = ""
    email: str = ""
    devices: list[StationIn]
    website: str = ""  # honeypot: hidden on the form, so only bots fill it in


def _signup_locked(body, ip: str) -> tuple[dict, list[str]]:
    """The checks and inserts of a sign-up; call with _signup_lock held."""
    if _signups.count(ip) >= SIGNUPS_PER_IP:
        raise HTTPException(429, "Too many sign-ups from here. Try again later.")
    username = body.username.strip()
    if not auth.USERNAME_RE.fullmatch(username):
        raise HTTPException(422, "Username: 3 to 32 letters, digits, dots, dashes or underscores")
    auth.check_password_rules(body.password)
    call, name, email = _clean_profile(body.callsign, body.name, body.email, need_call=True)
    if not body.devices:
        raise HTTPException(422, "Add at least one device")
    if len(body.devices) > MAX_DEVICES:
        raise HTTPException(422, f"At most {MAX_DEVICES} devices")
    for d in body.devices:
        d.check()
    calls = [d.callsign for d in body.devices]
    if len(set(calls)) != len(calls):
        raise HTTPException(422, "Each device needs its own callsign")
    with db.session() as c:
        if c.execute("SELECT COUNT(*) FROM users WHERE status='pending'").fetchone()[0] >= MAX_PENDING_ACCOUNTS:
            raise HTTPException(503, "Too many sign-ups are waiting for approval. Try again later.")
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise HTTPException(409, "That username is taken")
        taken = [cs for cs in calls if c.execute("SELECT 1 FROM stations WHERE callsign=?", (cs,)).fetchone()]
        if taken:
            raise HTTPException(409, f"Already registered: {', '.join(taken)}. Ask an admin if it's yours.")
        try:
            uid = auth.create_user(c, username, body.password, callsign=call, name=name, email=email)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That username is taken")
        for d in body.devices:
            _insert_station(c, d, owner_id=uid, status="pending")
        user = _user(c, uid)
    _signups.add(ip)
    return user, calls


@router.post("/api/signup")
def signup(body: SignupIn, request: Request, response: Response):
    """Create a pending account and its pending devices, and log in, so the
    person can watch for approval and fix their submission meanwhile."""
    if body.website:
        return {"user": None}  # a bot; say nothing useful
    ip = auth.client_ip(request)
    with _signup_lock:
        user, calls = _signup_locked(body, ip)
    uid = user["id"]
    logging.getLogger("admin").info("sign-up: %s (%s), devices %s", user["username"], user["callsign"], ", ".join(calls))
    _start_session(request, response, uid)
    return {"user": auth.public_user(user)}


@router.get("/api/account")
def account(user: dict = Depends(auth.require_user)):
    return {"user": auth.public_user(user), "devices": _station_rows("s.owner_id = ?", (user["id"],))}


class ProfileIn(BaseModel):
    name: str = ""
    email: str = ""


@router.put("/api/account")
def update_account(body: ProfileIn, user: dict = Depends(auth.require_user)):
    _, name, email = _clean_profile("", body.name, body.email, need_call=False)
    with db.session() as c:
        c.execute("UPDATE users SET name=?, email=? WHERE id=?", (name, email, user["id"]))
        return {"user": auth.public_user(_user(c, user["id"]))}


class PasswordIn(BaseModel):
    current: str
    new: str


@router.post("/api/account/password")
def change_password(body: PasswordIn, request: Request, user: dict = Depends(auth.require_user)):
    # Rate-limited like logins, so a stolen session can't be used to guess
    # the password itself.
    key = f"pw:{user['id']}"
    auth.check_rate_limit(key)
    if not auth.verify_password(body.current, user["pw_hash"]):
        auth.record_failure(key)
        raise HTTPException(400, "Current password is wrong")
    auth.check_password_rules(body.new)
    with db.session() as c:
        c.execute("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_password(body.new), user["id"]))
        auth.end_user_sessions(c, user["id"], keep_token=request.cookies.get(auth.COOKIE))
    return {"ok": True}


@router.delete("/api/account")
def withdraw_signup(request: Request, response: Response, user: dict = Depends(auth.require_user)):
    """A pending user can withdraw their sign-up. Approved accounts are closed by an admin."""
    if user["status"] != "pending":
        raise HTTPException(403, "Ask an admin to close an approved account")
    with db.session() as c:
        c.execute("DELETE FROM stations WHERE owner_id=?", (user["id"],))
        c.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        c.execute("DELETE FROM users WHERE id=?", (user["id"],))
    ingester.reload()  # in case an admin had approved one of the devices already
    response.delete_cookie(auth.COOKIE, path=_cookie_path(request))
    return {"ok": True}


def _own_device(c, sid: int, user: dict):
    """A device of this user's. Anyone else's is reported as missing."""
    st = c.execute("SELECT * FROM stations WHERE id=? AND owner_id=?", (sid, user["id"])).fetchone()
    if st is None:
        raise HTTPException(404, "No such device")
    return st


@router.post("/api/account/devices")
def add_device(body: StationIn, user: dict = Depends(auth.require_user)):
    body.check()
    with db.session() as c:
        if c.execute("SELECT COUNT(*) FROM stations WHERE owner_id=?", (user["id"],)).fetchone()[0] >= MAX_DEVICES:
            raise HTTPException(422, f"At most {MAX_DEVICES} devices per account")
        sid = _insert_station(c, body, owner_id=user["id"], status="pending")
    logging.getLogger("admin").info("%s submitted %s for approval", user["username"], body.callsign)
    return {"id": sid}


@router.put("/api/account/devices/{sid}")
def edit_device(sid: int, body: StationIn, user: dict = Depends(auth.require_user)):
    """An approved device keeps its callsign and type; everything else
    (hardware included, since boards get swapped) can
    change. A pending or rejected one can change entirely, and goes (back)
    into the approval queue."""
    body.check()
    with db.session() as c:
        st = _own_device(c, sid, user)
        approved = st["status"] == "approved"
        if approved:
            if (body.callsign, body.kind) != (st["callsign"], st["kind"]):
                raise HTTPException(422, "The callsign and type can't change after approval. "
                                         "Add a new device instead, then delete this one.")
            sets = ",".join(f"{f}=?" for f in DETAIL_FIELDS)
            c.execute(f"UPDATE stations SET {sets} WHERE id=?", (*station_values(body, DETAIL_FIELDS), sid))
        else:
            sets = ",".join(f"{f}=?" for f in STATION_FIELDS)
            with callsign_unique(body.callsign):
                c.execute(f"UPDATE stations SET {sets},status='pending',review_note='' WHERE id=?",
                          (*station_values(body), sid))
    if approved:
        ingester.reload()
    return {"ok": True}


@router.delete("/api/account/devices/{sid}")
def delete_device(sid: int, user: dict = Depends(auth.require_user)):
    """Removes the registration only. Stored packets stay; an admin can purge them."""
    with db.session() as c:
        st = _own_device(c, sid, user)
        c.execute("DELETE FROM stations WHERE id=?", (sid,))
    if st["status"] == "approved":
        ingester.reload()
        logging.getLogger("admin").info("%s deleted %s", user["username"], st["callsign"])
    return {"ok": True}
