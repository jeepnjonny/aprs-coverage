"""Reliability per square: of the beacons mobiles sent from a square, the share
that reached APRS-IS. This is the measure of coverage quality; the number of
packets heard mostly reflects how long someone spent there.

Beacons come from two sources:
  * exact: a tracker's uploaded transmit log (devicelog.py). Every logged
    position beacon counts as sent at its position, and as heard if it was
    matched to a stored packet. Repeats APRS-IS drops as duplicates are left out.
  * estimated: every other tracker, from gaps between the packets heard. A
    mobile that moved between two heard packets further apart than its beacon
    interval lost the beacons in between; they are placed evenly along the
    straight line between the two fixes. A gap with no movement (parked, or
    APRS-IS dropping identical repeats) says nothing and is skipped.

With a station filter, "heard" means heard by one of the selected stations, so
the map shows what those stations alone would cover.
"""
import bisect

from . import db, grid
from .grid import approx_km

LOG_NEAR_S = 120          # a heard packet this close to a logged beacon is the log's business
LOG_CATCHUP_S = 10        # ...once the log reaches past it (beacon sent a few seconds before it arrived)
MAX_GAP_S = 3600          # longer silences are trips apart, not lost beacons
INTERVAL_MAX_GAP_S = 900  # gaps used to estimate a beacon interval: longer is parked or off
MIN_MOVE_KM = 0.1         # ends of a gap closer than this: parked, nothing to estimate
MIN_SPEED_KMH = 3.0       # ...or moving slower than this on average (stopped part of the way)


def beacon_interval(heard: list[dict]) -> int | None:
    """Estimate the beacon interval from gaps between heard packets. Losses
    make some gaps 2x, 3x the interval, so take the lower quartile."""
    gaps = sorted(b["ts"] - a["ts"] for a, b in zip(heard, heard[1:]) if 5 <= b["ts"] - a["ts"] <= INTERVAL_MAX_GAP_S)
    if len(gaps) < 5:
        return None
    return gaps[len(gaps) // 4]


def reliability(c, start: int, end: int, cell_m: int, mobiles: list[str] | None,
                heard_ok: set[int] | None, estimates: bool = True, day_of=None) -> dict:
    """Per square: beacons sent and heard, split exact/estimated.

    mobiles: concrete mobile keys to include (None = all).
    heard_ok: transmission ids that count as heard (None = any station)."""
    dlat, dlon = grid.cell_deg(cell_m)
    q, args = "ts BETWEEN ? AND ?", [start, end]
    if mobiles is not None:
        if not mobiles:
            return {"cells": [], "sent": 0, "heard": 0, "exact": 0, "mobiles": {}}
        q += f" AND mobile IN ({db.placeholders(mobiles)})"
        args += mobiles

    heard: dict[str, list[dict]] = {}
    for r in c.execute(f"SELECT mobile, txid, MIN(ts) AS ts, MAX(lat) AS lat, MAX(lon) AS lon FROM packets "
                       f"WHERE {q} GROUP BY txid ORDER BY ts", args):
        heard.setdefault(r["mobile"], []).append(dict(r))
    logs: dict[str, list[dict]] = {}
    lq = q.replace("mobile IN", "device IN")
    for r in c.execute(f"SELECT device, ts, txid, lat, lon FROM device_log WHERE kind='tx' AND is_pos=1 "
                       f"AND repeat_of IS NULL AND lat IS NOT NULL AND {lq} ORDER BY ts", args):
        logs.setdefault(r["device"], []).append(dict(r))

    cells: dict[tuple[int, int], list] = {}  # [heard, sent, exact_heard, exact_sent, last_ts]

    by_day: dict[tuple[int, int], dict[int, list[int]]] = {}  # only with day_of: per square, per day [heard, sent]

    def add(lat, lon, ts, got, exact):
        k = grid.cell_at(lat, lon, dlat, dlon)
        if day_of:
            d = by_day.setdefault(k, {}).setdefault(day_of(ts), [0, 0])
            d[0] += got
            d[1] += 1
        e = cells.setdefault(k, [0, 0.0, 0, 0, 0])
        e[0] += got
        e[1] += 1
        if exact:
            e[2] += got
            e[3] += 1
        e[4] = max(e[4], ts)

    ok = (lambda txid: txid is not None) if heard_ok is None else (lambda txid: txid in heard_ok)
    per_mobile = {}
    for m in sorted(set(heard) | set(logs)):
        log = logs.get(m, [])
        hs = heard.get(m, [])
        for e in log:
            add(e["lat"], e["lon"], e["ts"], ok(e["txid"]), True)
        matched = {e["txid"] for e in log if e["txid"] is not None}
        log_ts = [e["ts"] for e in log]

        def logged(h):
            if h["txid"] in matched:
                return True
            # A packet newer than the log is still waiting for its log entry (the
            # app uploads about once a minute): count it as heard now, from the
            # packet, rather than leave it out until the upload arrives.
            if not log_ts or log_ts[-1] < h["ts"] - LOG_CATCHUP_S:
                return False
            i = bisect.bisect_left(log_ts, h["ts"] - LOG_NEAR_S)
            return i < len(log_ts) and log_ts[i] <= h["ts"] + LOG_NEAR_S

        interval = beacon_interval(hs) if estimates else None
        per_mobile[m] = {"logged": len(log), "interval_s": interval}
        if not estimates or (not interval and not log):
            # No interval, no log: losses can't be estimated, and counting only
            # what was heard would read as 100%, so its packets are left out.
            continue
        flags = [logged(h) for h in hs]
        for i, h in enumerate(hs):
            if flags[i]:
                continue
            add(h["lat"], h["lon"], h["ts"], ok(h["txid"]), False)
            if not interval or i == 0 or flags[i - 1]:
                continue
            p = hs[i - 1]
            gap = h["ts"] - p["ts"]
            # gap 0: two packets stored the same second (a digi's late copy of an
            # older beacon next to a fresh one); nothing to estimate between them
            if gap <= 0 or gap > MAX_GAP_S:
                continue
            km = approx_km(p["lat"], p["lon"], h["lat"], h["lon"])
            if km < MIN_MOVE_KM or km / (gap / 3600) < MIN_SPEED_KMH:
                continue
            missed = round(gap / interval) - 1
            for k in range(1, missed + 1):
                f = k / (missed + 1)
                add(p["lat"] + f * (h["lat"] - p["lat"]), p["lon"] + f * (h["lon"] - p["lon"]),
                    int(p["ts"] + f * gap), 0, False)

    out = [[x, y, e[0], int(e[1]), e[2], e[3], e[4]] for (x, y), e in cells.items()]
    extra = {"by_day": by_day} if day_of else {}
    return {**extra,
        # [x, y, heard, sent, exact heard, exact sent, last ts]
        "cells": out,
        "sent": sum(r[3] for r in out), "heard": sum(r[2] for r in out),
        "exact": sum(r[5] for r in out), "mobiles": per_mobile,
        "dlat": dlat, "dlon": dlon, "cell_m": cell_m, "start": start, "end": end,
    }
