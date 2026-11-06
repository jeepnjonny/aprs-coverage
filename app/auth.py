"""Single admin account: an scrypt password hash in settings, plus server-side sessions."""
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from . import config, db

COOKIE = "acov_session"
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)

# Login rate limit: at most MAX_FAILS failed attempts per IP per WINDOW seconds.
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


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if peer in ("127.0.0.1", "::1"):  # behind nginx on the same host
        return request.headers.get("x-real-ip", peer)
    return peer


def check_rate_limit(ip: str) -> None:
    q = _fails[ip]
    now = time.time()
    while q and q[0] < now - WINDOW:
        q.popleft()
    if len(q) >= MAX_FAILS:
        raise HTTPException(429, "Too many failed logins, try again later")


def record_failure(ip: str) -> None:
    _fails[ip].append(time.time())


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with db.session() as c:
        c.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        c.execute(
            "INSERT INTO sessions(token_hash, expires) VALUES(?,?)",
            (_token_hash(token), now + config.SESSION_DAYS * 86400),
        )
    return token


def destroy_session(token: str | None) -> None:
    if token:
        with db.session() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def is_logged_in(request: Request) -> bool:
    token = request.cookies.get(COOKIE)
    if not token:
        return False
    with db.session() as c:
        row = c.execute(
            "SELECT 1 FROM sessions WHERE token_hash=? AND expires>=?",
            (_token_hash(token), int(time.time())),
        ).fetchone()
    return row is not None


def require_admin(request: Request) -> None:
    """FastAPI dependency for admin routes. Write requests must also carry the
    X-Requested-With header. With SameSite=Strict cookies, that blocks
    cross-site form posts (CSRF)."""
    if not is_logged_in(request):
        raise HTTPException(401, "Login required")
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403, "Missing X-Requested-With header")
