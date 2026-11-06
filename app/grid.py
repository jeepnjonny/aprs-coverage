"""Fixed square grid measured in meters.

Cell height is cell_m of latitude; cell width is cell_m of longitude at
GRID_REF_LAT. Cells are anchored to 0,0 so the same location always falls in the
same cell, and cell (cx, cy) spans
    lat  [cy*dlat, (cy+1)*dlat)
    lon  [cx*dlon, (cx+1)*dlon)
"""
import math

from . import config

M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON_EQ = 111_320.0

# Added before integer truncation in SQL so CAST(... AS INTEGER) acts as floor()
# for negative longitudes. Big enough for 100 m cells at +/-180 deg.
SQL_FLOOR_OFFSET = 10_000_000


def cell_deg(cell_m: int) -> tuple[float, float]:
    dlat = cell_m / M_PER_DEG_LAT
    dlon = cell_m / (M_PER_DEG_LON_EQ * math.cos(math.radians(config.GRID_REF_LAT)))
    return dlat, dlon


def cell_of(lat: float, lon: float, cell_m: int) -> tuple[int, int]:
    dlat, dlon = cell_deg(cell_m)
    return math.floor(lon / dlon), math.floor(lat / dlat)


def sql_cell_exprs(cell_m: int) -> tuple[str, str]:
    """SQL expressions (cx, cy) for the packets table at this cell size."""
    dlat, dlon = cell_deg(cell_m)
    o = SQL_FLOOR_OFFSET
    cx = f"(CAST(lon / {dlon!r} + {o} AS INTEGER) - {o})"
    cy = f"(CAST(lat / {dlat!r} + {o} AS INTEGER) - {o})"
    return cx, cy


def approx_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in km."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
