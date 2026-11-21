"""How often a tracker beacons, worked out from the packets heard from it, so
lost beacons can be estimated for trackers that upload no transmit log.

Most trackers use smart beaconing rather than a fixed rate. Its interval
depends on speed:

    interval(v) = clamp(spacing / v, fast_s, slow_s)

  * at or above the fast speed: every fast_s seconds
  * between the slow and fast speeds: every `spacing` km driven
    (spacing = fast speed x fast rate)
  * at or below the slow speed: every slow_s seconds
  * plus corner pegging: an extra beacon after each turn

A fixed-rate tracker is the case fast_s == slow_s. Nobody enters these
settings: they are fitted per tracker from the gaps between its heard packets.
Each gap is a whole number of intervals (n - 1 beacons lost in between), so the
fit looks for the model under which the gaps line up with whole multiples of
the interval at the speed driven, with as few losses as possible. Gaps across a
turn are left out of the fit (corner pegging makes them short). A smart
beaconing model is kept only when it explains the gaps clearly better than one
fixed interval does.

Stopped trackers are fitted on their own: many beacon at a steady rate when
parked, many go quiet or switch off. Gaps while stopped are only counted as
losses for a tracker seen beaconing steadily while stopped.
"""
import math
import time
from dataclasses import dataclass

from . import config
from .grid import approx_km

STOP_KMH = 3.0          # slower than this on average between two fixes: stopped
MIN_MOVE_KM = 0.1       # ends of a gap closer than this: stopped
TURN_DEG = 30           # course change counted as a turn (corner pegging)
MAX_PATH_RATIO = 1.5    # road distance at most this much longer than the straight line
FIT_MIN_S, FIT_MAX_S = 5, 1800  # gaps used for fitting
MAX_GAP_S = 3600        # longer silences are trips apart, not lost beacons
STOP_MAX_MISSED = 4     # more lost in a row while stopped: switched off, not lost
MIN_PAIRS = 5           # fewer gaps: no model
MIN_PAIRS_SMART = 30    # fewer moving gaps: fixed interval only
MAX_PAIRS = 1500        # most recent gaps used for fitting

# Fit cost of one gap: squared log error against the nearest whole multiple of
# the interval (SIGMA = typical timing scatter), plus LOSS_COST per beacon the
# gap implies was lost, capped so a stray gap can't dominate.
SIGMA = 0.12
COARSE_SIGMA = 0.25
LOSS_COST = 2.0
CAP = 6.0
PEG_WEIGHT = 0.6


def _turned(p: dict, h: dict) -> bool:
    a, b = p.get("course"), h.get("course")
    if not a or not b:  # 0 is "unknown" in APRS
        return False
    return abs((a - b + 180) % 360 - 180) > TURN_DEG


def pair_motion(p: dict, h: dict) -> tuple[float, float]:
    """(straight-line km, speed km/h) between two fixes. Road distance is at
    least the straight line; where both packets carry a GPS speed, their mean
    times the gap is used, up to MAX_PATH_RATIO times the straight line (a
    stop at a light in between would otherwise inflate it)."""
    gap = h["ts"] - p["ts"]
    km = approx_km(p["lat"], p["lon"], h["lat"], h["lon"])
    path = km
    sp, sh = p.get("speed"), h.get("speed")
    if sp is not None and sh is not None:
        path = min(max(gap * (sp + sh) / 2 / 3600, km), km * MAX_PATH_RATIO)
    return km, path / gap * 3600


def _stopped(km: float, v: float) -> bool:
    return km < MIN_MOVE_KM or v < STOP_KMH


def _cost(r: float, sigma: float = SIGMA) -> float:
    """Cost of a gap that is r intervals long."""
    k = max(1, round(r))
    e = math.log(r / k) / sigma
    return min(CAP, e * e + LOSS_COST * (k - 1))


def _grid(lo: float, hi: float, step: float) -> list[float]:
    out, x = [], lo
    while x <= hi:
        out.append(x)
        x *= step
    return out


def _fit_scale(values: list[float], lo: float, hi: float) -> tuple[float, float]:
    """The scale T under which values are best whole multiples of T: coarse
    grid, then a fine one around the best. Returns (T, total cost)."""
    best = min(((sum(_cost(v / t) for v in values), t) for t in _grid(lo, hi, 1.04)))
    fine = _grid(best[1] / 1.05, best[1] * 1.05, 1.005)
    cost, t = min(((sum(_cost(v / t) for v in values), t) for t in fine))
    return t, cost


def _interval(v: float, fast: float, slow: float, spacing: float) -> float:
    return slow if v <= 0 else min(max(spacing / v * 3600, fast), slow)


def _smart_cost(pairs, fast, slow, spacing, sigma: float = SIGMA) -> float:
    return sum(_cost(g / _interval(v, fast, slow, spacing), sigma) for g, v in pairs)


def _descend(pairs, p: list[float]) -> tuple[float, list[float]]:
    """Coordinate descent from p, each parameter within +-40% per step."""
    best = _smart_cost(pairs, *p)
    for _ in range(3):
        for i in range(3):
            for f in _grid(0.6, 1.6, 1.03):
                x = list(p)
                x[i] *= f
                if x[0] <= x[1]:
                    c = _smart_cost(pairs, *x)
                    if c < best:
                        best, p = c, x
    return best, p


def _fit_smart(pairs: list[tuple[int, float]]) -> tuple[float, float, float, float]:
    """(fast_s, slow_s, spacing_km, cost) for moving gaps (gap s, speed km/h).
    A coarse grid over all three on a sample finds the right neighbourhood
    (the cost has local minima at half and double intervals), scored with a
    looser timing tolerance to suit its coarse steps; then descent from the
    best few starts on all gaps."""
    sample = pairs[::max(1, len(pairs) // 300)]
    coarse = []
    for fast in _grid(10, 400, 1.35):
        for slow in _grid(fast, FIT_MAX_S, 1.45):
            for spacing in _grid(0.1, 20, 1.35):
                coarse.append((_smart_cost(sample, fast, slow, spacing, COARSE_SIGMA), [fast, slow, spacing]))
    coarse.sort(key=lambda c: c[0])
    cost, p = min((_descend(pairs, x) for _, x in coarse[:5]), key=lambda c: c[0])
    return p[0], p[1], p[2], cost


@dataclass(frozen=True)
class Model:
    fast_s: float | None = None    # None: too few moving gaps to tell
    slow_s: float | None = None
    spacing_km: float | None = None  # None: fixed rate (fast_s == slow_s)
    stop_s: float | None = None    # interval while stopped; None: not steady
    peg_share: float = 0.0         # share of beacons sent early (corner pegging)
    pairs: int = 0

    @property
    def smart(self) -> bool:
        return self.spacing_km is not None

    def interval(self, v: float) -> float:
        if not self.smart:
            return self.fast_s
        return _interval(v, self.fast_s, self.slow_s, self.spacing_km)

    def missed(self, p: dict, h: dict) -> int:
        """Beacons lost between two consecutive heard packets."""
        gap = h["ts"] - p["ts"]
        # gap 0: two packets stored the same second (a digi's late copy of an
        # older beacon next to a fresh one); nothing to estimate between them
        if gap <= 0 or gap > MAX_GAP_S:
            return 0
        km, v = pair_motion(p, h)
        if _stopped(km, v):
            if not self.stop_s:
                return 0
            n = round(gap / self.stop_s) - 1
            return n if 0 < n <= STOP_MAX_MISSED else 0
        if self.fast_s is None:
            return 0
        # timed beacons over the gap, plus the corner pegs that come with them
        return max(0, round(gap / self.interval(v) / (1 - self.peg_share)) - 1)

    def describe(self) -> dict:
        r = lambda x, n=0: None if x is None else round(x, n) if n else round(x)  # noqa: E731
        d = {"kind": "smart" if self.smart else "fixed" if self.fast_s else None,
             "fast_s": r(self.fast_s), "slow_s": r(self.slow_s), "stop_s": r(self.stop_s),
             "peg_pct": round(self.peg_share * 100), "gaps": self.pairs}
        if self.smart:
            d["spacing_km"] = r(self.spacing_km, 2)
            d["fast_kmh"] = r(self.spacing_km / self.fast_s * 3600)
            d["slow_kmh"] = r(self.spacing_km / self.slow_s * 3600)
        return d


def fit(rows: list[dict]) -> Model | None:
    """Fit a tracker's beaconing from its heard packets, in time order
    (ts, lat, lon, speed, course). None when there is too little to go on."""
    moving, stopped, turns = [], [], []
    for p, h in zip(rows, rows[1:]):
        gap = h["ts"] - p["ts"]
        if not FIT_MIN_S <= gap <= FIT_MAX_S:
            continue
        km, v = pair_motion(p, h)
        if _stopped(km, v):
            stopped.append(gap)
        else:
            (turns if _turned(p, h) else moving).append((gap, v))
    moving, stopped, turns = moving[-MAX_PAIRS:], stopped[-MAX_PAIRS:], turns[-MAX_PAIRS:]

    fast = slow = spacing = stop = None
    peg = 0.0
    if len(moving) >= MIN_PAIRS:
        fast, cost = _fit_scale([g for g, _ in moving], FIT_MIN_S, FIT_MAX_S)
        slow = fast
        if len(moving) >= MIN_PAIRS_SMART:
            f, s, d, c = _fit_smart(moving)
            # two more parameters: worth it only if clearly better (BIC)
            if c + 2 * math.log(len(moving)) < cost:
                fast, slow, spacing = f, s, d
        # Corner pegging: of the gaps with nothing lost (under 1.5 intervals),
        # the share cut short is about the share of beacons sent early. Some
        # short gaps are speeding up rather than a turn (no extra beacon), so
        # only PEG_WEIGHT of it counts (calibrated on simulated drives).
        iv = lambda v: _interval(v, fast, slow, spacing) if spacing else fast  # noqa: E731
        r = [g / iv(v) for g, v in moving + turns]
        whole = sum(x < 1.5 for x in r)
        peg = sum(x < 0.75 for x in r) / whole if whole >= MIN_PAIRS_SMART else 0.0
        peg = min(peg * PEG_WEIGHT, 0.5)
    if len(stopped) >= MIN_PAIRS:
        t, _ = _fit_scale(stopped, FIT_MIN_S, FIT_MAX_S)
        # steady only if most stopped gaps are one interval: a tracker that goes
        # quiet when parked leaves gaps of any length
        if sum(abs(math.log(g / t)) < 2 * SIGMA for g in stopped) >= len(stopped) / 2:
            stop = t
    if fast is None and stop is None:
        return None
    return Model(fast, slow, spacing, stop, peg, len(moving) + len(stopped) + len(turns))


# Fits take up to a second, so a recent one is reused while the tracker's
# packets have grown little: (db, mobile, window days) -> (fitted at, rows, model)
_cache: dict[tuple, tuple[float, int, Model | None]] = {}
REFIT_S = 600
REFIT_GROWTH = 0.1


def fit_rows(c, mobile: str, end: int, start: int | None = None) -> list[dict]:
    """Heard packets to fit from: at least the 30 days up to end."""
    lo = end - 30 * 86400 if start is None else min(start, end - 30 * 86400)
    return [dict(r) for r in c.execute(
        "SELECT MIN(ts) AS ts, MAX(lat) AS lat, MAX(lon) AS lon, MAX(speed) AS speed, MAX(course) AS course "
        "FROM packets WHERE mobile=? AND ts BETWEEN ? AND ? GROUP BY txid ORDER BY ts", (mobile, lo, end))]


def model_for(c, mobile: str, end: int, start: int | None = None) -> Model | None:
    """The fitted model for a mobile, cached (see REFIT_S)."""
    rows = fit_rows(c, mobile, end, start)
    if len(rows) < 2:
        return None
    lo = rows[0]["ts"]
    key = (str(config.DB_PATH), mobile, lo // 86400, end // 86400)
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[2] and now - hit[0] < REFIT_S and abs(len(rows) - hit[1]) <= hit[1] * REFIT_GROWTH:
        return hit[2]
    if len(_cache) > 512:
        _cache.clear()
    model = fit(rows)
    _cache[key] = (now, len(rows), model)
    return model
