"""User accounts and server-side sessions.

Two roles: an admin can do everything; a node owner manages only their own
devices, and configures their own nodes over the air only if an admin ticked
"remote config" for them. Anyone can sign up from the site: the account and the
devices it lists stay pending until an admin approves them. A pending user can
log in to see and edit their submission, nothing else.
"""
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from . import config, db

COOKIE = "acov_session"
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)

ROLES = ("admin", "node_owner")
STATUSES = ("pending", "active", "disabled")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
# Base amateur callsign, no SSID (remote config sends from <this>-<SSID>).
BASE_CALL_RE = re.compile(r"^[A-Z0-9]{1,2}[0-9][A-Z]{1,4}$")
MIN_PASSWORD = 10

# Login rate limit: at most MAX_FAILS failed attempts per key (IP, and
# separately username) per WINDOW seconds.
MAX_FAILS, WINDOW = 8, 900
_fails: dict[str, deque] = defaultdict(deque)


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(h.hex(), hash_hex)


# Checked against when the username doesn't exist, so a login takes as long
# either way and doesn't reveal which usernames are taken.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if peer in ("127.0.0.1", "::1"):  # behind nginx on the same host
        return request.headers.get("x-real-ip", peer)
    return peer


def check_rate_limit(*keys: str) -> None:
    now = time.time()
    for key in keys:
        q = _fails[key]
        while q and q[0] < now - WINDOW:
            q.popleft()
        if len(q) >= MAX_FAILS:
            raise HTTPException(429, "Too many failed logins, try again later")


def record_failure(*keys: str) -> None:
    for key in keys:
        _fails[key].append(time.time())


def check_password_rules(pw: str) -> None:
    if len(pw) < MIN_PASSWORD:
        raise HTTPException(422, f"Password must be at least {MIN_PASSWORD} characters")


# ── users ────────────────────────────────────────────────────────────────────
def find_user(username: str):
    with db.session() as c:
        return c.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()


def authenticate(username: str, password: str):
    """The user row if the password is right and the account isn't disabled."""
    u = find_user(username)
    ok = verify_password(password, u["pw_hash"] if u else _DUMMY_HASH)
    return u if ok and u is not None else None


def create_user(c, username: str, password: str, *, role: str = "node_owner", status: str = "pending",
                callsign: str = "", name: str = "", email: str = "", can_config: bool = False) -> int:
    cur = c.execute(
        "INSERT INTO users(username, callsign, name, email, pw_hash, role, status, can_config) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (username, callsign, name, email, hash_password(password), role, status, int(can_config)),
    )
    return cur.lastrowid


def active_admins(c, excluding: int | None = None) -> int:
    return c.execute(
        "SELECT COUNT(*) FROM users WHERE role='admin' AND status='active' AND id != ?", (excluding or -1,)
    ).fetchone()[0]


def has_admin() -> bool:
    with db.session() as c:
        return active_admins(c) > 0


# ── sessions ─────────────────────────────────────────────────────────────────
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with db.session() as c:
        c.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        c.execute(
            "INSERT INTO sessions(token_hash, user_id, expires) VALUES(?,?,?)",
            (_token_hash(token), user_id, now + config.SESSION_DAYS * 86400),
        )
        c.execute("UPDATE users SET last_login=? WHERE id=?", (now, user_id))
    return token


def destroy_session(token: str | None) -> None:
    if token:
        with db.session() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def end_user_sessions(c, user_id: int, keep_token: str | None = None) -> None:
    """Log a user out everywhere (optionally keeping the session making the change)."""
    c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash != ?",
              (user_id, _token_hash(keep_token) if keep_token else ""))


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    with db.session() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash=? AND s.expires>=? AND u.status != 'disabled'",
            (_token_hash(token), int(time.time())),
        ).fetchone()
    return dict(row) if row else None


def public_user(u: dict) -> dict:
    return {k: u[k] for k in ("id", "username", "callsign", "name", "email", "role", "status")} | {
        "can_config": bool(u["can_config"]) or u["role"] == "admin"}


# ── FastAPI dependencies ─────────────────────────────────────────────────────
def _csrf(request: Request) -> None:
    """Write requests must carry X-Requested-With. With SameSite=Strict
    cookies, that blocks cross-site form posts (CSRF)."""
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403, "Missing X-Requested-With header")


def require_user(request: Request) -> dict:
    """Any logged-in user, including one still waiting for approval."""
    u = current_user(request)
    if u is None:
        raise HTTPException(401, "Login required")
    _csrf(request)
    return u


def require_active(request: Request) -> dict:
    u = require_user(request)
    if u["status"] != "active":
        raise HTTPException(403, "Your account is waiting for approval")
    return u


def require_admin(request: Request) -> dict:
    u = require_active(request)
    if u["role"] != "admin":
        raise HTTPException(403, "Admins only")
    return u


def require_config(request: Request) -> dict:
    """Remote node config: admins, and node owners an admin has allowed."""
    u = require_active(request)
    if u["role"] != "admin" and not u["can_config"]:
        raise HTTPException(403, "Remote config isn't enabled for your account. Ask an admin.")
    return u
