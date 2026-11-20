"""FastAPI app: startup and background tasks, middleware, and the HTML pages.
The API itself is in app/routes/ (public, accounts, admin, remote)."""
import asyncio
import logging
import signal
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, devicelog, logbuf
from .ingest import ingester
from .routes import accounts, admin, public
from .routes import remote as remote_routes
from .routes.remote import remote

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
    rematch = asyncio.create_task(_rematch_loop())
    backups = asyncio.create_task(_backup_loop())
    missed = asyncio.create_task(_missed_loop())
    yield
    task.cancel()
    rematch.cancel()
    backups.cancel()
    missed.cancel()
    await remote.sender.close()


async def _rematch_loop():
    """Uploaded transmit logs can arrive before APRS-IS delivers the packets
    they should match; try again every few minutes."""
    def run():
        with db.session() as c:
            n = devicelog.rematch_recent(c)
        if n:
            logbuf.DEVICES.add("match", "", f"re-matched recent logged transmissions: {n} now heard on APRS-IS")
    while True:
        await asyncio.sleep(300)
        try:
            await asyncio.to_thread(run)
        except Exception:
            logging.getLogger("devicelog").exception("re-matching device logs failed")


async def _missed_loop():
    """Announce on the live feed each logged beacon that APRS-IS never heard,
    once it has had its chance (devicelog.SETTLE_S), for the map's red X."""
    seen: set[int] = set()

    def run():
        with db.session() as c:
            return devicelog.newly_missed(c, seen)
    first = True
    while True:
        await asyncio.sleep(30)
        try:
            missed = await asyncio.to_thread(run)
        except Exception:
            logging.getLogger("devicelog").exception("checking for missed beacons failed")
            continue
        if first:  # after a restart, don't replay the last few minutes
            first = False
            continue
        for ev in missed:
            ingester._fan_out({**ev, "missed": True})


async def _backup_loop():
    """A copy of the database a day (the first check is a minute after start)."""
    if config.BACKUP_KEEP <= 0:
        return
    log = logging.getLogger("backup")
    await asyncio.sleep(60)
    while True:
        try:
            today = time.strftime("%Y-%m-%d")
            out = await asyncio.to_thread(db.backup, config.BACKUP_DIR, config.BACKUP_KEEP, today)
            if out:
                log.info("database backed up to %s", out)
        except Exception:
            log.exception("database backup failed")
        await asyncio.sleep(3600)


app = FastAPI(title="APRS Coverage", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Scripts only from here and the Leaflet CDN (no inline script). Inline
    # style attributes are allowed: the pages build swatches with them. Images
    # are the base-map tile servers in static/map.js.
    "Content-Security-Policy": "; ".join([
        "default-src 'self'",
        "script-src 'self' https://unpkg.com",
        "style-src 'self' 'unsafe-inline' https://unpkg.com",
        "img-src 'self' data: https://unpkg.com https://tile.openstreetmap.org https://*.tile.opentopomap.org "
        "https://basemap.nationalmap.gov",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]),
    # Cross-origin requests carry only the site's origin. Not "same-origin":
    # OpenStreetMap's tile policy requires a Referer, and refuses tiles without.
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


@app.middleware("http")
async def revalidate_pages(request: Request, call_next):
    """Pages and static files must be revalidated on every load (a cheap 304
    when unchanged), so browsers pick up a deploy without a hard refresh."""
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/admin", "/compare") or path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    # Basic hardening. No framing: the admin page can make nodes transmit, so
    # it must not be clickjackable from another site.
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC / "admin.html")


@app.get("/compare", include_in_schema=False)
def compare_page():
    return FileResponse(STATIC / "compare.html")


for _r in (public.router, accounts.router, admin.router, remote_routes.router):
    app.include_router(_r)
