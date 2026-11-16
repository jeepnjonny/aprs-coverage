"""Optional device logs uploaded by an app (for example the LoRa FieldOps config
app, listening to a tracker over Bluetooth). Nothing on the site depends on
them: without uploads, comparisons fall back to estimates from APRS-IS alone.

A mobile's own log says what it transmitted. Matching each logged transmission
to a stored packet turns "how much got through" from an estimate into a count.
Its log of what it heard adds digis repeating its packets, with signal strength.
"""
import hashlib
import secrets
import time
from collections import defaultdict

import aprslib

from .packets import POSITION_FORMATS, is_alias, mobile_matches, object_key

KEY_PREFIX = "acv_"
MAX_KEYS = 5                      # per user
MAX_ENTRIES = 1000                # per upload
MAX_AGE_S = 400 * 86400           # oldest entry accepted
MAX_FUTURE_S = 300                # clock slack for entries "from the future"
# A logged transmission matches a stored packet with the same information field
# received this many seconds before/after it (phone and server clocks, digi and
# igate delays).
MATCH_BEFORE_S, MATCH_AFTER_S = 30, 90
REMATCH_WINDOW_S = 2 * 86400      # background re-matching looks this far back
LORA_PREFIX = "<\xff\x01"


class UploadError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


# ── API keys ─────────────────────────────────────────────────────────────────
def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(30)


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def user_for_key(c, key: str, now: int) -> dict | None:
    """The active user a key belongs to, or None."""
    if not key.startswith(KEY_PREFIX):
        return None
    row = c.execute(
        "SELECT u.*, k.id AS key_id FROM api_keys k JOIN users u ON u.id = k.user_id "
        "WHERE k.key_hash = ? AND u.status = 'active'", (key_hash(key),)).fetchone()
    if row is None:
        return None
    c.execute("UPDATE api_keys SET last_used=? WHERE id=?", (now, row["key_id"]))
    return dict(row)


# ── packets from a device's log ──────────────────────────────────────────────
def clean_line(line: str) -> str:
    line = line.strip("\r\n\0")
    if line.startswith(LORA_PREFIX):
        line = line[len(LORA_PREFIX):]
    return line


def info_of(line: str) -> str:
    """Information field: everything after the first ':', trailing spaces
    dropped (APRS-IS servers and loggers don't agree on keeping them)."""
    return line.partition(":")[2].rstrip()


def parse(line: str) -> dict | None:
    """Source, mobile key, path and position of a TNC2 line, or None if it
    isn't one. Unparseable payloads still count; they just have no position."""
    head, sep, _ = line.partition(":")
    if not sep or ">" not in head:
        return None
    src, _, rest = head.partition(">")
    src = src.strip().upper()
    if not src:
        return None
    out = {"src": src, "key": src, "info": info_of(line), "path": rest.split(",")[1:],
           "is_pos": False, "lat": None, "lon": None}
    try:
        pkt = aprslib.parse(line)
    except Exception:  # aprslib raises several exception types
        return out
    fmt = pkt.get("format")
    if fmt == "object":
        out["key"] = object_key(src, pkt.get("object_name", ""))
        pos = "latitude" in pkt and pkt.get("alive", True)
    else:
        pos = fmt in POSITION_FORMATS and "latitude" in pkt
    if pos and not pkt.get("posambiguity", 0):
        out.update(is_pos=True, lat=float(pkt["latitude"]), lon=float(pkt["longitude"]))
    return out


def last_digi(path: list[str]) -> tuple[bool, str | None]:
    """(was digipeated, callsign of the digi that last sent it). The '*' marks
    the last used hop; the sender is the last traced callsign among used hops,
    None if only aliases were used (an untraced digi)."""
    used_to = max((i for i, h in enumerate(path) if h.endswith("*")), default=-1)
    if used_to < 0:
        return False, None
    used = [h.rstrip("*").upper() for h in path[: used_to + 1]]
    calls = [h for h in used if not is_alias(h)]
    return True, (calls[-1] if calls else None)


# ── upload ───────────────────────────────────────────────────────────────────
def _device_kind(c, user: dict, device: str) -> str:
    """'mobile' or 'node' for an approved device of this user's (any for an
    admin). A mobile registered as a pattern (K7ABC-*) uploads per SSID."""
    admin = user["role"] == "admin"
    rows = c.execute(
        f"SELECT callsign, kind FROM stations WHERE status='approved' {'' if admin else 'AND owner_id=?'}",
        () if admin else (user["id"],)).fetchall()
    for r in rows:
        if r["kind"] == "mobile" and mobile_matches(device, r["callsign"]):
            return "mobile"
    for r in rows:
        if r["kind"] != "mobile" and r["callsign"].upper() == device:
            return "node"
    raise UploadError(404, f"{device} is not an approved device of yours")


def _number(v, lo, hi):
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def upload(c, user: dict, device: str, entries: list[dict], now: int | None = None) -> dict:
    """Store a batch of log entries for one device. Safe to retry: an entry
    already stored is counted as a duplicate, not stored again."""
    now = now or int(time.time())
    device = device.strip().upper()
    if not device:
        raise UploadError(422, "device is required")
    if len(entries) > MAX_ENTRIES:
        raise UploadError(413, f"At most {MAX_ENTRIES} entries per upload")
    kind = _device_kind(c, user, device)
    sender = device.split(":", 1)[0]
    nodes = {r[0].upper() for r in c.execute(
        "SELECT callsign FROM stations WHERE kind != 'mobile' AND status='approved'")}
    res = {"accepted": 0, "duplicates": 0, "ignored": 0, "errors": []}
    tx_span = [None, None]

    def err(i, msg):
        res["errors"].append({"index": i, "error": msg})

    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            err(i, "not an object")
            continue
        t = e.get("type")
        ts = _number(e.get("ts"), now - MAX_AGE_S, now + MAX_FUTURE_S)
        if ts is None:
            err(i, "ts must be unix seconds within the last 400 days")
            continue
        ts = int(ts)
        if t == "event":
            text = str(e.get("text") or "").strip()[:300]
            if not text:
                err(i, "event needs text")
                continue
            n = c.execute("INSERT OR IGNORE INTO device_events(device, ts, text, source, user_id) "
                          "VALUES(?,?,?,'app',?)", (device, ts, text, user["id"])).rowcount
        elif t in ("tx", "rx"):
            if kind != "mobile":
                err(i, "tx and rx entries are only for mobiles")
                continue
            line = clean_line(str(e.get("packet") or ""))
            p = parse(line) if len(line) <= 600 else None
            if p is None:
                err(i, "packet must be one TNC2 line, SOURCE>DEST,PATH:info")
                continue
            echo, via, is_pos = 0, None, p["is_pos"]
            if t == "tx":
                if p["key"] != device:
                    if p["src"] != sender:
                        err(i, f"tx packet is from {p['key']}, not {device}")
                        continue
                    is_pos = False  # the sender's other traffic (status, another object)
            elif p["key"] == device:
                digipeated, via = last_digi(p["path"])
                if not digipeated:
                    res["ignored"] += 1  # can't hear yourself; nothing to learn
                    continue
                echo = 1
            elif p["src"] in nodes:
                digipeated, via = last_digi(p["path"])
                via = via if digipeated else p["src"]
            else:
                res["ignored"] += 1  # other stations' traffic isn't kept
                continue
            n = c.execute(
                "INSERT OR IGNORE INTO device_log(device, kind, ts, packet, info, is_pos, lat, lon, echo, via, "
                "rssi, snr, user_id, uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (device, t, ts, line, p["info"], int(is_pos), p["lat"] if is_pos else None,
                 p["lon"] if is_pos else None, echo, via, _number(e.get("rssi"), -200, 50),
                 _number(e.get("snr"), -50, 50), user["id"], now)).rowcount
            if n and t == "tx" and is_pos:
                tx_span = [min(ts, tx_span[0] or ts), max(ts, tx_span[1] or ts)]
        else:
            err(i, "type must be tx, rx or event")
            continue
        res["accepted" if n else "duplicates"] += 1
    if tx_span[0] is not None:
        match(c, device, tx_span[0] - MATCH_AFTER_S - MATCH_BEFORE_S, tx_span[1] + MATCH_AFTER_S + MATCH_BEFORE_S)
    res["errors"] = res["errors"][:20]
    return res


# ── matching logged transmissions to stored packets ─────────────────────────
def match(c, device: str, start: int, end: int) -> int:
    """Re-match a device's logged position transmissions in [start, end] to
    the transmissions stored from APRS-IS. Pairs need the same information
    field and nearby times; the closest pairs are taken first, and each
    stored transmission matches one log entry at most, so a lost beacon
    doesn't claim the next identical one (a parked tracker repeats itself)."""
    ents = c.execute("SELECT id, ts, info FROM device_log WHERE device=? AND kind='tx' AND is_pos=1 "
                     "AND ts BETWEEN ? AND ?", (device, start, end)).fetchall()
    if not ents:
        return 0
    lo, hi = start - MATCH_BEFORE_S, end + MATCH_AFTER_S
    claimed = {r[0] for r in c.execute(
        "SELECT txid FROM device_log WHERE device=? AND kind='tx' AND txid IS NOT NULL "
        "AND (ts < ? OR ts > ?) AND ts BETWEEN ? AND ?",
        (device, start, end, lo - MATCH_AFTER_S, hi + MATCH_BEFORE_S))}
    by_info: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for r in c.execute("SELECT txid, MIN(ts) AS ts, raw FROM packets WHERE mobile=? AND ts BETWEEN ? AND ? "
                       "GROUP BY txid", (device, lo, hi)):
        if r["txid"] not in claimed:
            by_info[info_of(r["raw"])].append((r["ts"], r["txid"]))
    pairs = []
    for e in ents:
        for pts, txid in by_info.get(e["info"], ()):
            dt = pts - e["ts"]
            if -MATCH_BEFORE_S <= dt <= MATCH_AFTER_S:
                pairs.append((abs(dt), e["id"], txid))
    pairs.sort()
    got: dict[int, int] = {}
    taken: set[int] = set()
    for _, eid, txid in pairs:
        if eid not in got and txid not in taken:
            got[eid] = txid
            taken.add(txid)
    c.executemany("UPDATE device_log SET txid=? WHERE id=?", [(got.get(e["id"]), e["id"]) for e in ents])
    return len(got)


def rematch_recent(c, now: int | None = None) -> int:
    """Logs can be uploaded live, before APRS-IS has delivered the packets.
    Re-match recent unmatched transmissions; the ingester's run loop calls
    this every few minutes."""
    now = now or int(time.time())
    n = 0
    for r in c.execute("SELECT device, MIN(ts) FROM device_log WHERE kind='tx' AND is_pos=1 AND txid IS NULL "
                       "AND ts >= ? GROUP BY device", (now - REMATCH_WINDOW_S,)).fetchall():
        n += match(c, r[0], r[1] - MATCH_AFTER_S - MATCH_BEFORE_S, now)
    return n
