"""Head-to-head comparisons of two mobiles, or two igates/digis, over a long
period, bucketed by day, week or month so trends show rather than single days.

Mobiles: what share of transmissions reached APRS-IS. Exact where the mobile's
own transmit log was uploaded (see devicelog.py), otherwise estimated from gaps
in what was heard, at a known or estimated beacon interval.

Igates/digis: of the transmissions from registered mobiles that anyone heard,
which ones each station heard. Only fair with the aprsc duplicates feed, which
records every station that heard a packet, not just the first.
"""
import bisect
import math
import time
from datetime import datetime, timezone

from . import db, grid
from .grid import approx_km
from .reliability import beacon_interval, reliability

BUCKETS = ("day", "week", "month")
MAX_GAP_S = 900          # a longer silence is parked or switched off, not lost packets
POS_GAP_S = 300          # a position is interpolated between fixes at most this far apart
ECHO_WINDOW_S = 60       # a digi echo is heard this soon after the transmission
DIST_BANDS_KM = (0, 2, 5, 10, 20, 40)


# ── statistics ───────────────────────────────────────────────────────────────
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% confidence interval for a proportion k/n."""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, mid - half), min(1.0, mid + half)


def two_prop_p(k1: int, n1: int, k2: int, n2: int) -> float | None:
    """Two-sided p-value that two proportions differ (pooled z-test)."""
    if not n1 or not n2:
        return None
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = (k1 / n1 - k2 / n2) / se
    return math.erfc(abs(z) / math.sqrt(2))


def mcnemar_p(b: int, c: int) -> float | None:
    """Two-sided p-value for paired data: b = only A heard it, c = only B did.
    Exact binomial for small counts, chi-square with continuity correction above."""
    n = b + c
    if not n:
        return None
    if n <= 50:
        k = min(b, c)
        return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
    x = (abs(b - c) - 1) ** 2 / n
    return math.erfc(math.sqrt(x / 2))


def bucket_start(ts: int, bucket: str, tz_s: int = 0) -> int:
    """Start of the day, week (Monday) or month holding ts, in the viewer's
    time zone (tz_s = seconds east of UTC)."""
    t = ts + tz_s
    if bucket == "day":
        b = t - t % 86400
    elif bucket == "week":
        b = t - (t - 4 * 86400) % (7 * 86400)  # 1970-01-05 was a Monday
    else:
        d = datetime.fromtimestamp(t, timezone.utc)
        b = int(datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp())
    return b - tz_s


def _ratio(k, n):
    return k / n if n else None


def events(c, devices: list[str], start: int, end: int) -> list[dict]:
    if not devices:
        return []
    q = db.placeholders(devices)
    return [dict(r) for r in c.execute(
        f"SELECT id, device, ts, text, source, user_id FROM device_events WHERE device IN ({q}) "
        f"AND ts BETWEEN ? AND ? ORDER BY ts", (*devices, start, end))]


# ── mobiles ──────────────────────────────────────────────────────────────────
class Track:
    """Where a mobile was, from every fix known: heard packets and its own log."""

    def __init__(self, fixes: list[tuple[int, float, float]]):
        self.fixes = sorted(fixes)
        self.ts = [f[0] for f in self.fixes]

    def at(self, t: int) -> tuple[float, float] | None:
        i = bisect.bisect_left(self.ts, t)
        nxt = self.fixes[i] if i < len(self.fixes) else None
        prv = self.fixes[i - 1] if i > 0 else None
        if nxt and nxt[0] == t:
            return nxt[1], nxt[2]
        if prv and nxt and nxt[0] - prv[0] <= POS_GAP_S:
            f = (t - prv[0]) / (nxt[0] - prv[0])
            return prv[1] + f * (nxt[1] - prv[1]), prv[2] + f * (nxt[2] - prv[2])
        near = [x for x in (prv, nxt) if x and abs(x[0] - t) <= 60]
        return (near[0][1], near[0][2]) if near else None


def _heard(c, mobile, start, end) -> list[dict]:
    """Transmissions heard, one row each: first receive time, position,
    whether any igate heard it directly, how many stations heard it."""
    return [dict(r) for r in c.execute(
        "SELECT txid, MIN(ts) AS ts, MAX(lat) AS lat, MAX(lon) AS lon, MIN(hops) AS hops, "
        "COUNT(DISTINCT COALESCE(heard_by, '')) AS stations FROM packets "
        "WHERE mobile=? AND ts BETWEEN ? AND ? GROUP BY txid ORDER BY ts", (mobile, start, end))]


def _txlog(c, mobile, start, end) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT id, ts, txid, lat, lon, info FROM device_log WHERE device=? AND kind='tx' AND is_pos=1 "
        "AND repeat_of IS NULL AND ts BETWEEN ? AND ? ORDER BY ts", (mobile, start, end))]


def _rx_summary(c, mobile, start, end, log: list[dict]) -> dict:
    """From the mobile's receive log: digis repeating its own packets (with how
    often, and how strongly), and igate/digi beacons it heard."""
    rows = c.execute("SELECT ts, info, echo, via, rssi, snr FROM device_log WHERE device=? AND kind='rx' "
                     "AND ts BETWEEN ? AND ?", (mobile, start, end)).fetchall()
    tx_times: dict[str, list[int]] = {}
    for e in log:
        tx_times.setdefault(e["info"], []).append(e["ts"])
    out = {"echoes": {}, "beacons": {}}
    for r in rows:
        d = out["echoes" if r["echo"] else "beacons"].setdefault(
            r["via"] or "(untraced)", {"via": r["via"] or "(untraced)", "n": 0, "rssi": [], "snr": [], "tx": set()})
        d["n"] += 1
        if r["rssi"] is not None:
            d["rssi"].append(r["rssi"])
        if r["snr"] is not None:
            d["snr"].append(r["snr"])
        if r["echo"]:
            for t in tx_times.get(r["info"], ()):
                if 0 <= r["ts"] - t <= ECHO_WINDOW_S:
                    d["tx"].add(t)
    res = {}
    for k, group in out.items():
        lst = []
        for d in group.values():
            item = {"via": d["via"], "n": d["n"],
                    "rssi": round(sum(d["rssi"]) / len(d["rssi"]), 1) if d["rssi"] else None,
                    "snr": round(sum(d["snr"]) / len(d["snr"]), 1) if d["snr"] else None}
            if k == "echoes":
                item["echo_rate"] = round(len(d["tx"]) / len(log), 4) if log else None
            lst.append(item)
        res[k] = sorted(lst, key=lambda x: -x["n"])
    return res


def mobile_series(c, mobile: str, start: int, end: int, bucket: str, tz_s: int,
                  interval: int | None = None, keep=None) -> dict:
    """Per-bucket share of transmissions heard for one mobile. keep(ts, lat,
    lon) limits which transmissions count (e.g. only when beside the other)."""
    heard = _heard(c, mobile, start, end)
    log = _txlog(c, mobile, start, end)
    est_interval = interval or beacon_interval(heard)
    # With a filter, gaps are still measured on everything heard (a packet
    # filtered out was heard, not lost), and count only when both ends pass.
    kept = [not keep or keep(h["ts"], h["lat"], h["lon"]) for h in heard]
    if keep:
        log = [e for e in log if e["lat"] is not None and keep(e["ts"], e["lat"], e["lon"])]
    b: dict[int, dict] = {}

    def bk(ts):
        k = bucket_start(ts, bucket, tz_s)
        return b.setdefault(k, {"t": k, "sent": 0, "heard": 0, "packets": 0, "direct": 0, "stations": 0,
                                "method": None})

    for e in log:
        x = bk(e["ts"])
        x["method"] = "exact"
        x["sent"] += 1
        x["heard"] += e["txid"] is not None
    for i, h in enumerate(heard):
        if not kept[i]:
            continue
        x = bk(h["ts"])
        x["packets"] += 1
        x["direct"] += h["hops"] == 0
        x["stations"] += h["stations"]
        if x["method"] != "exact":
            x["method"] = "estimated" if est_interval else None
            x["heard"] += 1
            x["sent"] += 1
            if i and kept[i - 1] and est_interval:
                gap = h["ts"] - heard[i - 1]["ts"]
                if gap <= MAX_GAP_S:
                    x["sent"] += max(0, round(gap / est_interval) - 1)
    rows = []
    for x in sorted(b.values(), key=lambda r: r["t"]):
        if x["method"] is None:
            x["sent"] = x["heard"] = 0
        ci = wilson(x["heard"], x["sent"])
        rows.append({
            "t": x["t"], "method": x["method"], "sent": x["sent"], "heard": x["heard"],
            "pct": _ratio(x["heard"], x["sent"]), "lo": ci[0] if ci else None, "hi": ci[1] if ci else None,
            "packets": x["packets"], "direct_pct": _ratio(x["direct"], x["packets"]),
            "stations": round(x["stations"] / x["packets"], 2) if x["packets"] else None,
        })
    sent = sum(r["sent"] for r in rows)
    got = sum(r["heard"] for r in rows)
    methods = {r["method"] for r in rows if r["method"]}
    ci = wilson(got, sent)
    pk = sum(r["packets"] for r in rows)
    return {
        "mobile": mobile, "buckets": rows, "interval_s": est_interval, "interval_given": bool(interval),
        "total": {
            "sent": sent, "heard": got, "pct": _ratio(got, sent), "lo": ci[0] if ci else None,
            "hi": ci[1] if ci else None, "packets": pk,
            "method": "mixed" if len(methods) > 1 else (methods.pop() if methods else None),
            "direct_pct": _ratio(sum(x["direct"] for x in b.values()), pk),
            "stations": round(sum(x["stations"] for x in b.values()) / pk, 2) if pk else None,
            "logged_tx": len(log),
        },
        "rx": _rx_summary(c, mobile, start, end, log),
    }


def compare_mobiles(c, a: str, b: str, start: int, end: int, bucket: str = "week", tz_s: int = 0,
                    together: bool = False, sep_m: float = 200, interval: int | None = None) -> dict:
    keep = {a: None, b: None}
    if together:
        tracks = {}
        for m in (a, b):
            fixes = [(h["ts"], h["lat"], h["lon"]) for h in _heard(c, m, start, end)]
            fixes += [(e["ts"], e["lat"], e["lon"]) for e in _txlog(c, m, start, end) if e["lat"] is not None]
            tracks[m] = Track(fixes)

        def near(other):
            def f(ts, lat, lon):
                p = tracks[other].at(ts)
                return p is not None and approx_km(lat, lon, p[0], p[1]) * 1000 <= sep_m
            return f
        keep = {a: near(b), b: near(a)}
    sa = mobile_series(c, a, start, end, bucket, tz_s, interval, keep[a])
    sb = mobile_series(c, b, start, end, bucket, tz_s, interval, keep[b])
    ta, tb = sa["total"], sb["total"]
    return {
        "kind": "mobiles", "a": sa, "b": sb, "start": start, "end": end, "bucket": bucket,
        "together": together, "sep_m": sep_m,
        "p_value": two_prop_p(ta["heard"], ta["sent"], tb["heard"], tb["sent"]),
        "events": events(c, [a, b], start, end),
    }


# ── igates / digis ───────────────────────────────────────────────────────────
def compare_nodes(c, a: str, b: str, start: int, end: int, bucket: str = "week", tz_s: int = 0,
                  match: str = "first", mobiles: list[str] | None = None) -> dict:
    """Of the transmissions anyone heard, which each station heard.
    match 'first': heard the mobile directly; 'any': anywhere in the path."""
    expr, pa = db.heard_expr(a, match)
    _, pb = db.heard_expr(b, match)
    where, args = "ts BETWEEN ? AND ?", [start, end]
    if mobiles:
        where += f" AND mobile IN ({db.placeholders(mobiles)})"
        args += mobiles
    rows = c.execute(
        f"SELECT txid, MIN(ts) AS ts, MAX(lat) AS lat, MAX(lon) AS lon, "
        f"MAX({expr}) AS ha, MAX({expr}) AS hb FROM packets WHERE {where} GROUP BY txid",
        [pa, pb, *args]).fetchall()
    dupes = c.execute("SELECT COUNT(*) FROM packets WHERE dupe=1 AND ts BETWEEN ? AND ?",
                      (start, end)).fetchone()[0]
    pos = {}
    for call in (a, b):
        s = c.execute("SELECT lat, lon, auto_lat, auto_lon FROM stations WHERE callsign=?", (call,)).fetchone()
        if s is not None:
            lat, lon = db.station_pos(s)
            if lat is not None and lon is not None:
                pos[call] = (lat, lon)

    bk: dict[int, dict] = {}
    edges = list(DIST_BANDS_KM) + [math.inf]
    bands = {call: [{"from_km": lo, "to_km": None if hi == math.inf else hi, "n": 0, "heard": 0}
                    for lo, hi in zip(edges, edges[1:])] for call in pos}
    for r in rows:
        k = bucket_start(r["ts"], bucket, tz_s)
        x = bk.setdefault(k, {"t": k, "total": 0, "both": 0, "a_only": 0, "b_only": 0, "neither": 0})
        x["total"] += 1
        ha, hb = bool(r["ha"]), bool(r["hb"])
        x["both" if ha and hb else "a_only" if ha else "b_only" if hb else "neither"] += 1
        for call, heard in ((a, ha), (b, hb)):
            if call in pos:
                d = approx_km(r["lat"], r["lon"], *pos[call])
                band = bands[call][bisect.bisect_right(DIST_BANDS_KM, d) - 1]
                band["n"] += 1
                band["heard"] += heard
    out = []
    for x in sorted(bk.values(), key=lambda r: r["t"]):
        n = x["total"]
        ka, kb = x["both"] + x["a_only"], x["both"] + x["b_only"]
        ca, cb = wilson(ka, n), wilson(kb, n)
        out.append({**x, "a_pct": _ratio(ka, n), "b_pct": _ratio(kb, n),
                    "a_lo": ca and ca[0], "a_hi": ca and ca[1], "b_lo": cb and cb[0], "b_hi": cb and cb[1]})
    tot = {k: sum(x[k] for x in out) for k in ("total", "both", "a_only", "b_only", "neither")}
    for call, side in ((a, "a"), (b, "b")):
        k = tot["both"] + tot[f"{side}_only"]
        ci = wilson(k, tot["total"])
        tot[f"{side}_pct"] = _ratio(k, tot["total"])
        tot[f"{side}_lo"], tot[f"{side}_hi"] = (ci or (None, None))
    for lst in bands.values():
        for band in lst:
            band["pct"] = _ratio(band["heard"], band["n"])
    return {
        "kind": "nodes", "a": a, "b": b, "start": start, "end": end, "bucket": bucket, "match": match,
        "buckets": out, "total": tot, "p_value": mcnemar_p(tot["a_only"], tot["b_only"]),
        "dupes_feed": dupes > 0, "bands": bands, "positions": pos,
        "events": events(c, [a, b], start, end),
    }


# ── one station, before vs after a change ────────────────────────────────────
CHANGE_MIN_BEACONS = 3   # a square needs this many beacons on both sides to list as changed


def _side(heard: int, sent: int, start: int, end: int) -> dict:
    ci = wilson(heard, sent)
    return {"start": start, "end": end, "heard": heard, "sent": sent, "pct": _ratio(heard, sent),
            "lo": ci[0] if ci else None, "hi": ci[1] if ci else None}


def _node_cells(c, station, start, end, cell_m, match, day_of):
    """Per square (of where each transmission was sent from): of the
    transmissions anyone heard, how many this station heard, also per day."""
    dlat, dlon = grid.cell_deg(cell_m)
    expr, arg = db.heard_expr(station, match)
    cells: dict = {}
    days: dict = {}
    for r in c.execute(f"SELECT MIN(ts) AS ts, MAX(lat) AS lat, MAX(lon) AS lon, MAX({expr}) AS h FROM packets "
                       f"WHERE ts BETWEEN ? AND ? GROUP BY txid", (arg, start, end)):
        k = grid.cell_at(r["lat"], r["lon"], dlat, dlon)
        e = cells.setdefault(k, [0, 0])
        e[0] += bool(r["h"])
        e[1] += 1
        d = days.setdefault(k, {}).setdefault(day_of(r["ts"]), [0, 0])
        d[0] += bool(r["h"])
        d[1] += 1
    return cells, days


def compare_change(c, station: str, at: int, days: int | None, cell_m: int = 500, tz_s: int = 0,
                   same_route: bool = True, match: str = "first", now: int | None = None) -> dict:
    """The same station before and after a dated change (new antenna, moved,
    new firmware). days: length of each period; None = from the change until
    now, with a before period just as long. same_route: only squares the
    station was in during both periods, so a detour doesn't count as a change."""

    now = now or int(time.time())
    span = days * 86400 if days else max(86400, now - at)
    b0, b1, a0, a1 = at - span, at - 1, at, min(at + span, now)
    row = c.execute("SELECT kind FROM stations WHERE callsign=? AND kind != 'mobile'", (station,)).fetchone()
    kind = "node" if row else "mobile"
    day_of = lambda ts: bucket_start(ts, "day", tz_s)  # noqa: E731
    if kind == "mobile":
        rb = reliability(c, b0, b1, cell_m, [station], None, True, day_of=day_of)
        ra = reliability(c, a0, a1, cell_m, [station], None, True, day_of=day_of)
        cb = {(x, y): [h, n] for x, y, h, n, *_ in rb["cells"]}
        ca = {(x, y): [h, n] for x, y, h, n, *_ in ra["cells"]}
        db, da = rb.get("by_day", {}), ra.get("by_day", {})
        exact = sum(r[5] for r in rb["cells"] + ra["cells"])
    else:
        cb, db = _node_cells(c, station, b0, b1, cell_m, match, day_of)
        ca, da = _node_cells(c, station, a0, a1, cell_m, match, day_of)
        exact = 0
    both = set(cb) & set(ca)
    route_b = both if same_route else set(cb)
    route_a = both if same_route else set(ca)

    def total(cells, keys):
        return sum(cells[k][0] for k in keys), sum(cells[k][1] for k in keys)

    def series(by_day, keys, side):
        out: dict[int, list[int]] = {}
        for k in keys:
            for d, (h, n) in by_day.get(k, {}).items():
                e = out.setdefault(d, [0, 0])
                e[0] += h
                e[1] += n
        rows = []
        for d in sorted(out):
            h, n = out[d]
            ci = wilson(h, n)
            rows.append({"t": d, "side": side, "heard": h, "sent": n, "pct": _ratio(h, n),
                         "lo": ci[0] if ci else None, "hi": ci[1] if ci else None})
        return rows

    hb, nb = total(cb, route_b)
    ha, na = total(ca, route_a)
    dlat, dlon = grid.cell_deg(cell_m)
    changed = []
    for k in both:
        (h1, n1), (h2, n2) = cb[k], ca[k]
        if n1 >= CHANGE_MIN_BEACONS and n2 >= CHANGE_MIN_BEACONS:
            changed.append({"lat": round((k[1] + 0.5) * dlat, 5), "lon": round((k[0] + 0.5) * dlon, 5),
                            "before": [h1, n1], "after": [h2, n2], "delta": h2 / n2 - h1 / n1})
    changed.sort(key=lambda r: r["delta"])
    return {
        "kind": "change", "station": station, "station_kind": kind, "at": at, "days": days,
        "same_route": same_route, "cell_m": cell_m, "match": match,
        "before": _side(hb, nb, b0, b1), "after": _side(ha, na, a0, a1),
        "p_value": two_prop_p(ha, na, hb, nb),
        "squares": {"both": len(both), "before": len(cb), "after": len(ca)},
        "exact": exact,
        "dupes_feed": c.execute("SELECT 1 FROM packets WHERE dupe=1 AND ts BETWEEN ? AND ? LIMIT 1",
                                (b0, a1)).fetchone() is not None,
        "days_series": series(db, route_b, "before") + series(da, route_a, "after"),
        "better": [r for r in reversed(changed) if r["delta"] > 0][:5],
        "worse": [r for r in changed if r["delta"] < 0][:5],
        "events": events(c, [station], b0, a1),
    }
