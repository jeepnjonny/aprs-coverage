"""Runtime configuration, read from environment variables (and ./.env if present).

On the server, systemd loads /srv/aprs-coverage/.env via EnvironmentFile.
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
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


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
APRS_LOGIN = os.environ.get("APRS_LOGIN", "").strip().upper()  # e.g. K7SWI-CV
APRS_SERVER = os.environ.get("APRS_SERVER", "noam.aprs2.net")  # North America pool; rotate.aprs2.net is worldwide
APRS_PORT = int(os.environ.get("APRS_PORT", "14580"))
APRS_ENABLED = _bool("APRS_ENABLED", True)

# Storage
DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "coverage.sqlite")))

# Grid: cell widths in longitude are computed at this fixed reference latitude so
# cell boundaries never move. 43.6 = Boise/Meridian.
GRID_REF_LAT = float(os.environ.get("GRID_REF_LAT", "43.6"))
CELL_SIZES_M = (100, 250, 500, 1000, 2000)

# Data-quality filters
MAX_SPEED_KMH = float(os.environ.get("MAX_SPEED_KMH", "250"))
DUPLICATE_WINDOW_S = int(os.environ.get("DUPLICATE_WINDOW_S", "30"))
REGION_BBOX = _bbox("REGION_BBOX")  # optional; packets outside are rejected

# Web / admin
COOKIE_SECURE = _bool("COOKIE_SECURE", True)
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
