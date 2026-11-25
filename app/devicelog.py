"""Optional device logs uploaded by an app (for example the LoRa FieldOps config
app, listening to a tracker over Bluetooth). Nothing on the site depends on
them: without uploads, comparisons fall back to estimates from APRS-IS alone.

A mobile's own log says what it transmitted. Matching each logged transmission
to a stored packet turns "how much got through" from an estimate into a count.
Its log of what it heard adds digis repeating its packets, with signal strength.
"""
import csv
import hashlib
import io
import secrets
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import aprslib

from . import db
from .packets import POSITION_FORMATS, is_alias, mobile_matches, object_key, used_hops

KEY_PREFIX = "acv_"
MAX_KEYS = 5                      # per user
MAX_ENTRIES = 1000                # per upload
MAX_AGE_S = 400 * 86400           # oldest entry accepted
MAX_FUTURE_S = 300                # clock slack for entries "from the future"
MAX_NOTE_LEN = 300                # characters in a dated note (from the app or the web)
# A logged transmission matches a stored packet with the same information field
# received this many seconds before/after it (phone and server clocks, digi and
# igate delays).
MATCH_BEFORE_S, MATCH_AFTER_S = 30, 90
SETTLE_S = MATCH_AFTER_S + 30  # a transmission this old has had its chance to be heard
# APRS-IS servers drop a packet identical (same source and information field)
# to one they passed within this many seconds. A tracker beaconing faster than
# that from the same spot repeats itself, and those repeats never reach APRS-IS
# however well they were heard, so they are left out of the counts.
APRSIS_DUPE_S = 30
# Background re-matching looks this far back. An upload is matched as it
# arrives; re-matching only catches live uploads that beat APRS-IS, whose
# packets arrive within MATCH_AFTER_S. The loop runs every 5 minutes, so this
# gives each entry several tries without rescanning days of logs each time.
REMATCH_WINDOW_S = 30 * 60
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
    used = used_hops(path)
    if not used:
        return False, None
    calls = [h for h in used if not is_alias(h)]
    return True, (calls[-1] if calls else None)


# ── upload ───────────────────────────────────────────────────────────────────
def device_kind(c, user: dict, device: str) -> str:
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
    mine = ", ".join(r["callsign"] for r in rows) or "none"
    raise UploadError(404, f"{device} is not an approved device of yours (approved: {mine}). "
                           "A mobile registered as a pattern uploads as the callsign it matches, "
                           "e.g. K7ABC-9 for K7ABC-*, or K7SWI:CAR03 for K7SWI:CAR*.")


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
    kind = device_kind(c, user, device)
    sender = device.split(":", 1)[0]
    nodes = {call.upper() for call in db.infra_calls(c)}
    res = {"accepted": 0, "duplicates": 0, "ignored": 0, "errors": []}
    tx_span = [None, None]
    tx_ids: list[int] = []

    def err(i, msg):
        res["errors"].append({"index": i, "error": msg})

    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            err(i, "not an object")
            continue
        t = e.get("type")
        ts = _number(e.get("ts"), now - MAX_AGE_S, now + MAX_FUTURE_S)
        if ts is None:
            err(i, f"ts must be unix seconds within the last {MAX_AGE_S // 86400} days")
            continue
        ts = int(ts)
        if t == "event":
            text = str(e.get("text") or "").strip()[:MAX_NOTE_LEN]
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
            cur = c.execute(
                "INSERT OR IGNORE INTO device_log(device, kind, ts, packet, info, is_pos, lat, lon, echo, via, "
                "rssi, snr, user_id, uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (device, t, ts, line, p["info"], int(is_pos), p["lat"] if is_pos else None,
                 p["lon"] if is_pos else None, echo, via, _number(e.get("rssi"), -200, 50),
                 _number(e.get("snr"), -50, 50), user["id"], now))
            n = cur.rowcount
            if n and t == "tx" and is_pos:
                tx_ids.append(cur.lastrowid)
                tx_span = [min(ts, tx_span[0] or ts), max(ts, tx_span[1] or ts)]
        else:
            err(i, "type must be tx, rx or event")
            continue
        res["accepted" if n else "duplicates"] += 1
    if tx_span[0] is not None:
        match(c, device, tx_span[0] - MATCH_AFTER_S - MATCH_BEFORE_S, tx_span[1] + MATCH_AFTER_S + MATCH_BEFORE_S)
    # New position transmissions in this batch, and how many were heard on APRS-IS
    # so far (more can match later, once their packets arrive).
    res["tx"] = len(tx_ids)
    res["heard"] = sum(c.execute(f"SELECT COUNT(*) FROM device_log WHERE txid IS NOT NULL AND id IN "
                                 f"({db.placeholders(chunk)})", chunk).fetchone()[0]
                       for chunk in (tx_ids[i:i + 500] for i in range(0, len(tx_ids), 500)))
    res["errors"] = res["errors"][:20]
    return res


# ── log files copied off a tracker's SD card ────────────────────────────────
# Uploaded on the account page by the device's owner (an admin for any device),
# a file at a time. Only what the tracker sent is taken: each row becomes a tx
# entry for upload(), so matching, reliability and comparisons treat it exactly
# like an app's live upload. Receive logs are refused; for coverage they add
# little beyond the transmit log, and most of their rows are other traffic.
#
# The LoRanger V2 writes one directory per power-on, /log/<session>/, with
# tx.csv (utc,millis,packet): packet RFC-4180 quoted, utc ISO 8601 and empty
# while GPS has no fix. Its rx.csv (with rssi,snr) and its other channels
# (power, events, battery, gps, accel) are refused. The parser goes by column names, not positions, so other trackers'
# logs with the usual names work too.
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILE_ROWS = 100_000
FILE_COLUMNS = {
    "time": ("utc", "time", "timestamp", "ts", "datetime", "date_time", "unix", "epoch", "gps_time"),
    "type": ("type", "dir", "direction", "kind", "event"),
    "packet": ("packet", "raw", "tnc2", "frame", "aprs", "line", "data"),
}
RX_COLUMNS = ("rssi", "snr")                 # a receive log: refused
TX_TYPES = ("tx", "t", "sent", "send", "beacon")  # in a type column, the rows kept


def parse_time(v: str) -> int | None:
    """Unix seconds, or ISO 8601 / 'YYYY-MM-DD HH:MM:SS' (UTC unless it carries
    an offset). None if it's neither."""
    v = v.strip()
    if not v:
        return None
    try:
        return int(float(v))
    except ValueError:
        pass
    try:
        t = datetime.fromisoformat(v[:-1] + "+00:00" if v[-1:] in "Zz" else v)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return int(t.timestamp())


def parse_log_file(text: str) -> tuple[list[dict], list[dict], int, int]:
    """(entries, errors, skipped, untimed) from a CSV transmit log. Each
    entry carries the file `line` it came from. The first non-blank, non-'#'
    row is the header and needs a time column and a packet column; a file with
    signal-strength columns is a receive log and is refused. With a type
    column, only transmissions are kept and other rows are skipped; so are rows
    with no time (sent before GPS had a fix: they can't be placed). When the packet is the last
    column, commas in an unquoted packet are kept. Raises UploadError if the
    file can't be read at all."""
    text = text.lstrip("\ufeff")
    rows = csv.reader(io.StringIO(text, newline=""))
    cols: dict[str, int] | None = None
    entries, errors, skipped, untimed = [], [], 0, 0
    for row in rows:
        line = rows.line_num
        if not row or not "".join(row).strip() or row[0].lstrip().startswith("#"):
            continue
        if cols is None:
            names = [h.strip().lower().replace(" ", "_") for h in row]
            cols = {}
            for col, aliases in FILE_COLUMNS.items():
                for i, n in enumerate(names):
                    if n in aliases:
                        cols[col] = i
                        break
            if "time" not in cols or "packet" not in cols:
                raise UploadError(422, "This file has no packets. Upload a tracker's tx.csv (its first row must "
                                       "name a time column, such as utc, and a packet column). Its "
                                       "columns: " + ", ".join(row)[:200])
            if any(n in RX_COLUMNS for n in names):
                raise UploadError(422, "This is a receive log (rx.csv); only transmit logs are taken. "
                                       "Upload the tracker's tx.csv.")
            continue
        if len(entries) >= MAX_FILE_ROWS:
            raise UploadError(413, f"At most {MAX_FILE_ROWS:,} rows per file")
        pi = cols["packet"]
        if len(row) <= pi:
            errors.append({"line": line, "error": "too few columns"})
            continue
        # An unquoted packet with commas in its path or comment spills into
        # extra columns; when the packet is the last column, put it back together.
        packet = ",".join(row[pi:]) if pi == max(cols.values()) else row[pi]
        if "type" in cols and row[cols["type"]].strip().lower() not in TX_TYPES:
            skipped += 1
            continue
        if not row[cols["time"]].strip():
            untimed += 1
            continue
        ts = parse_time(row[cols["time"]])
        if ts is None:
            errors.append({"line": line, "error": f"can't read the time {row[cols['time']][:40]!r}"})
            continue
        entries.append({"type": "tx", "ts": ts, "packet": packet, "line": line})
    if cols is None:
        raise UploadError(422, "The file is empty")
    return entries, errors, skipped, untimed


def upload_file(c, user: dict, device: str, text: str, now: int | None = None) -> dict:
    """Store a whole log file for one mobile, in batches of MAX_ENTRIES in
    time order (each batch is matched as it goes in). Like upload(), safe to
    repeat: rows already stored count as duplicates. Transmissions from other
    stations (a tracker that also digipeats logs what it relayed) are ignored,
    but a file with no transmissions of this device's at all is refused: it's
    most likely another tracker's card."""
    device = device.strip().upper()
    if not device:
        raise UploadError(422, "device is required")
    if device_kind(c, user, device) != "mobile":
        raise UploadError(422, f"{device} is an igate/digi; log files are for mobiles")
    entries, errors, skipped, untimed = parse_log_file(text)
    sender = device.split(":", 1)[0]
    sources = Counter(e["packet"].partition(">")[0].strip().upper() for e in entries)
    if sources and not sources[sender]:
        top = ", ".join(f"{call} ({n})" for call, n in sources.most_common(3))
        raise UploadError(422, f"None of the transmissions in this file are from {device}. They are from {top}. "
                               "Pick the device whose card this is.")
    relayed = sum(n for call, n in sources.items() if call != sender)
    entries = [e for e in entries if e["packet"].partition(">")[0].strip().upper() == sender]
    entries.sort(key=lambda e: e["ts"])
    res = {"rows": len(entries) + relayed + len(errors) + skipped + untimed, "accepted": 0, "duplicates": 0, "ignored": relayed, "skipped": skipped, "untimed": untimed,
           "rejected": len(errors), "tx": 0, "heard": 0, "first": None, "last": None}
    for i in range(0, len(entries), MAX_ENTRIES):
        batch = entries[i:i + MAX_ENTRIES]
        r = upload(c, user, device, batch, now)
        for k in ("accepted", "duplicates", "ignored", "tx", "heard"):
            res[k] += r[k]
        res["rejected"] += len(batch) - r["accepted"] - r["duplicates"] - r["ignored"]
        errors += [{"line": batch[e["index"]]["line"], "error": e["error"]} for e in r["errors"]]
    if entries:
        res["first"], res["last"] = entries[0]["ts"], entries[-1]["ts"]
    res["errors"] = sorted(errors, key=lambda e: e["line"])[:20]
    return res


# ── matching logged transmissions to stored packets ─────────────────────────
def match(c, device: str, start: int, end: int) -> int:
    """Re-match a device's logged position transmissions in [start, end] to
    the transmissions stored from APRS-IS. Pairs need the same information
    field and nearby times; the closest pairs are taken first, and each
    stored transmission matches one log entry at most, so a lost beacon
    doesn't claim the next identical one (a parked tracker repeats itself)."""
    ents = c.execute("SELECT id, ts, info, txid, repeat_of FROM device_log WHERE device=? AND kind='tx' AND is_pos=1 "
                     "AND ts BETWEEN ? AND ?", (device, start, end)).fetchall()
    if not ents:
        return 0
    lo, hi = start - MATCH_BEFORE_S, end + MATCH_AFTER_S
    claimed = {r[0] for r in c.execute(
        "SELECT txid FROM device_log WHERE device=? AND kind='tx' AND txid IS NOT NULL "
        "AND (ts < ? OR ts > ?) AND ts BETWEEN ? AND ?",
        (device, start, end, lo - MATCH_AFTER_S, hi + MATCH_BEFORE_S))}
    by_info: dict[str, list[tuple[int, int]]] = defaultdict(list)
    every: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for r in c.execute("SELECT txid, MIN(ts) AS ts, raw FROM packets WHERE mobile=? AND ts BETWEEN ? AND ? "
                       "GROUP BY txid", (device, lo - APRSIS_DUPE_S, hi)):
        every[info_of(r["raw"])].append((r["ts"], r["txid"]))
        if r["txid"] not in claimed and r["ts"] >= lo:
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
    # An unmatched beacon identical to a packet APRS-IS passed just before it
    # (arriving up to 5 s after it was sent) would have been dropped as a duplicate.
    repeat: dict[int, int] = {}
    for e in ents:
        if e["id"] not in got:
            near = [txid for pts, txid in every.get(e["info"], ()) if -APRSIS_DUPE_S <= pts - e["ts"] <= 5]
            if near:
                repeat[e["id"]] = near[0]
    changed = [(got.get(e["id"]), repeat.get(e["id"]), e["id"]) for e in ents
               if (e["txid"], e["repeat_of"]) != (got.get(e["id"]), repeat.get(e["id"]))]
    c.executemany("UPDATE device_log SET txid=?, repeat_of=? WHERE id=?", changed)
    return len(got)


def _ago(s: int) -> str:
    s = abs(s)
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h"
    return f"{s // 86400} days"


def explain_unheard(c, device: str, ts: int, info: str, now: int | None = None) -> str:
    """Why a logged transmission has no matching packet, for the admin log:
    rejected by a quality check, stored with different text (an app decoding
    problem), the station isn't set up to be collected, or nothing arrived."""
    now = now or int(time.time())
    if ts > now - SETTLE_S:
        return "not heard yet (its packet may still be on the way)"
    rej = c.execute("SELECT ts, reason FROM rejects WHERE source=? AND ts BETWEEN ? AND ? ORDER BY ABS(ts-?) LIMIT 1",
                    (device, ts - MATCH_BEFORE_S, ts + MATCH_AFTER_S, ts)).fetchone()
    if rej:
        return f"not heard: APRS-IS delivered a packet {rej['ts'] - ts:+d} s from it, rejected as {rej['reason']}"
    near = c.execute("SELECT ts, raw FROM packets WHERE mobile=? AND ts BETWEEN ? AND ? ORDER BY ABS(ts-?) LIMIT 1",
                     (device, ts - MATCH_BEFORE_S, ts + MATCH_AFTER_S, ts)).fetchone()
    if near:
        if info_of(near["raw"]) == info:
            return (f"not heard: an identical packet stored {near['ts'] - ts:+d} s from it was matched to "
                    "another logged beacon with the same text")
        return (f"not heard: a packet from {device} stored {near['ts'] - ts:+d} s from it has different text "
                f"(logged {info[:40]!r}, APRS-IS {info_of(near['raw'])[:40]!r}); the app may be decoding it wrongly")
    stations = c.execute("SELECT callsign, kind, active, status FROM stations").fetchall()
    infra = [r for r in stations if r["kind"] != "mobile" and r["callsign"].upper() == device.split(":")[0]]
    if infra:
        return (f"not heard: {infra[0]['callsign']} is registered as an igate/digi, so its packets are never "
                "collected as a mobile's")
    pats = [r for r in stations if r["kind"] == "mobile" and mobile_matches(device, r["callsign"])]
    if not any(r["active"] and r["status"] == "approved" for r in pats):
        return f"not heard: no active, approved mobile registration matches {device}, so its packets aren't collected"
    last = c.execute("SELECT MAX(ts) FROM packets WHERE mobile=?", (device,)).fetchone()[0]
    when = f"the last one was stored {_ago(ts - last)} {'before' if last < ts else 'after'} it" if last \
        else "none has ever been stored"
    return f"not heard: no packet from {device} reached APRS-IS near this time ({when})"


# Live "missed beacon" flashes: a logged position transmission that has had its
# chance to be heard (SETTLE_S) and wasn't. Only ones sent this recently are
# announced; older uploads are history, not live.
MISSED_LIVE_S = 10 * 60


def newly_missed(c, seen: set[int], now: int | None = None) -> list[dict]:
    """Settled, unheard position transmissions from the last MISSED_LIVE_S not
    already in `seen` (ids announced before; updated here and pruned of rows
    too old to come up again). Re-matches first, so a live upload that beat
    APRS-IS isn't reported lost when its packet has since arrived."""
    now = now or int(time.time())
    lo, hi = now - MISSED_LIVE_S, now - SETTLE_S
    q = ("SELECT id, device, ts, lat, lon FROM device_log WHERE kind='tx' AND is_pos=1 AND txid IS NULL "
         "AND repeat_of IS NULL AND lat IS NOT NULL AND ts BETWEEN ? AND ?")
    devices = {r["device"] for r in c.execute(q, (lo, hi)) if r["id"] not in seen}
    for d in devices:
        match(c, d, lo - MATCH_AFTER_S - MATCH_BEFORE_S, now)
    out = []
    ids = set()
    for r in c.execute(q, (lo, hi)):
        ids.add(r["id"])
        if r["id"] not in seen:
            out.append({"mobile": r["device"], "ts": r["ts"], "lat": r["lat"], "lon": r["lon"]})
    seen.intersection_update(ids)
    seen.update(ids)
    return out


def rematch_recent(c, now: int | None = None) -> int:
    """Logs can be uploaded live, before APRS-IS has delivered the packets.
    Re-match recent unmatched transmissions; the ingester's run loop calls
    this every few minutes (main._rematch_loop)."""
    now = now or int(time.time())
    n = 0
    for r in c.execute("SELECT device, MIN(ts) FROM device_log WHERE kind='tx' AND is_pos=1 AND txid IS NULL "
                       "AND repeat_of IS NULL "
                       "AND ts >= ? GROUP BY device", (now - REMATCH_WINDOW_S,)).fetchall():
        n += match(c, r[0], r[1] - MATCH_AFTER_S - MATCH_BEFORE_S, now)
    return n
