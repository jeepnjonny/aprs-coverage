"""Runtime configuration, read from environment variables (and ./.env if present).

On the server the app reads /srv/aprs-coverage/.env itself (_load_dotenv);
values already in the environment win.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if val[:1] in ('"', "'") and val[1:].find(val[0]) >= 0:
            val = val[1:1 + val[1:].find(val[0])]  # quoted: take it as written
        else:
            val = val.split(" #", 1)[0].split("\t#", 1)[0].strip()  # KEY=value  # comment
        os.environ.setdefault(key.strip(), val)


def _num(name: str, default: str, kind=int):
    """A number from the environment, with a clear message if it isn't one."""
    raw = os.environ.get(name, default)
    try:
        return kind(raw)
    except ValueError:
        raise SystemExit(f"Configuration error: {name}={raw!r} is not a valid "
                         f"{'whole number' if kind is int else 'number'} (check .env)") from None


_load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _bbox(name: str):
    """'min_lat,min_lon,max_lat,max_lon' -> tuple, or None."""
    v = os.environ.get(name, "").strip()
    if not v:
        return None
    parts = [float(p) for p in v.split(",")]
    if len(parts) != 4:
        raise ValueError(f"{name} must be min_lat,min_lon,max_lat,max_lon")
    return tuple(parts)


# APRS-IS (read-only: passcode is always -1)
APRS_LOGIN = os.environ.get("APRS_LOGIN", "").strip().upper()  # e.g. K7ABC-CV; empty = feed off
APRS_SERVER = os.environ.get("APRS_SERVER", "noam.aprs2.net")  # North America pool; rotate.aprs2.net is worldwide
APRS_PORT = _num("APRS_PORT", "14580")
APRS_ENABLED = _bool("APRS_ENABLED", True)

# Storage
DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "coverage.sqlite")))

# Grid: cell widths in longitude are computed at this fixed reference latitude so
# cell boundaries never move. 43.6 = Boise/Meridian.
GRID_REF_LAT = _num("GRID_REF_LAT", "43.6", float)
CELL_SIZES_M = (100, 250, 500, 1000, 2000)

# Data-quality filters
MAX_SPEED_KMH = _num("MAX_SPEED_KMH", "250", float)
DUPLICATE_WINDOW_S = _num("DUPLICATE_WINDOW_S", "30")
REGION_BBOX = _bbox("REGION_BBOX")  # optional; packets outside are rejected

# Backups: one copy of the database a day, the newest BACKUP_KEEP kept
# (0 = off). Each is a full copy, so allow BACKUP_KEEP x the database size.
BACKUP_KEEP = _num("BACKUP_KEEP", "3")
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "") or DB_PATH.parent / "backups")

# Web / admin
COOKIE_SECURE = _bool("COOKIE_SECURE", True)
SESSION_DAYS = _num("SESSION_DAYS", "30")
