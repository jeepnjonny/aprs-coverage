"""SQLite storage. One short-lived connection per operation; WAL mode lets the
ingester write while the web handlers read."""
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id          INTEGER PRIMARY KEY,
    callsign    TEXT NOT NULL UNIQUE,          -- upper case; mobiles may use a trailing * wildcard
    kind        TEXT NOT NULL CHECK (kind IN ('mobile','igate','digi','digi_igate')),
    label       TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    color       TEXT,                          -- optional #rrggbb for map colouring
    hardware    TEXT NOT NULL DEFAULT '',      -- board key from main.HARDWARE; '' = not set
    lat         REAL,                          -- manual position (igate/digi)
    lon         REAL,
    auto_lat    REAL,                          -- last position beaconed by the station itself
    auto_lon    REAL,
    last_beacon INTEGER,
    is_server   TEXT,                          -- APRS-IS server named in its last qAC packet (its own login)
    is_server_ts INTEGER,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    owner_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- NULL = managed by the admins
    status      TEXT NOT NULL DEFAULT 'approved' CHECK (status IN ('pending','approved','rejected')),
    review_note TEXT NOT NULL DEFAULT ''       -- admin's reason when rejected
);

CREATE TABLE IF NOT EXISTS packets (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,                -- unix seconds, server receive time
    mobile    TEXT NOT NULL,                   -- source callsign-SSID
    lat       REAL NOT NULL,
    lon       REAL NOT NULL,
    alt       REAL,
    speed     REAL,                            -- km/h
    course    INTEGER,
    igate     TEXT NOT NULL,                   -- station after the q-construct
    heard_by  TEXT,                            -- first RF hop that heard the mobile; NULL = untraced
    hops      INTEGER NOT NULL,                -- used digipeater hops
    chain     TEXT NOT NULL,                   -- ' DIGI1 DIGI2 IGATE ' (space padded for LIKE)
    raw       TEXT NOT NULL,
    txid      INTEGER,                         -- id of the first stored copy of this transmission
    dupe      INTEGER NOT NULL DEFAULT 0       -- 1 = came from the aprsc duplicates feed
);
CREATE INDEX IF NOT EXISTS ix_packets_ts       ON packets(ts);
CREATE INDEX IF NOT EXISTS ix_packets_mobile   ON packets(mobile, ts);
CREATE INDEX IF NOT EXISTS ix_packets_heard_by ON packets(heard_by, ts);
CREATE INDEX IF NOT EXISTS ix_packets_igate    ON packets(igate, ts);   -- last heard per igate

CREATE TABLE IF NOT EXISTS rejects (
    id      INTEGER PRIMARY KEY,
    ts      INTEGER NOT NULL,
    source  TEXT,
    reason  TEXT NOT NULL,
    raw     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Remote config (admin "Node config" tab): last values each igate/digi reported
CREATE TABLE IF NOT EXISTS node_config (
    callsign   TEXT PRIMARY KEY,
    fields     TEXT NOT NULL,                  -- JSON {code: value}
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS node_config_log (
    id       INTEGER PRIMARY KEY,
    ts       INTEGER NOT NULL,
    login    TEXT NOT NULL,                    -- operator callsign the command was sent from
    callsign TEXT NOT NULL,                    -- node
    command  TEXT NOT NULL,                    -- unlock tokens are masked
    result   TEXT NOT NULL,
    detail   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE COLLATE NOCASE,
    callsign   TEXT NOT NULL DEFAULT '',       -- base callsign, no SSID; remote config sends from it
    name       TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL DEFAULT '',       -- contact only; nothing is emailed
    pw_hash    TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'node_owner' CHECK (role IN ('admin','node_owner')),
    status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','disabled')),
    can_config INTEGER NOT NULL DEFAULT 0,     -- node owner may use remote config on their nodes
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    last_login INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    expires    INTEGER NOT NULL
);

-- Device log uploads (optional): keys a user makes for an app to upload with
CREATE TABLE IF NOT EXISTS api_keys (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL DEFAULT '',
    key_hash   TEXT NOT NULL UNIQUE,           -- sha256 of the key; the key itself is shown once
    prefix     TEXT NOT NULL,                  -- first characters, to tell keys apart
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    last_used  INTEGER
);

-- What a mobile's own log says it sent (tx) and heard (rx), uploaded by an app
CREATE TABLE IF NOT EXISTS device_log (
    id          INTEGER PRIMARY KEY,
    device      TEXT NOT NULL,                 -- mobile key, as in packets.mobile (K7ABC-9 or K7SWI:CAR01)
    kind        TEXT NOT NULL CHECK (kind IN ('tx','rx')),
    ts          INTEGER NOT NULL,              -- unix seconds, the phone's clock when logged
    packet      TEXT NOT NULL,                 -- TNC2 line as logged
    info        TEXT NOT NULL,                 -- information field (after the first ':')
    is_pos      INTEGER NOT NULL DEFAULT 0,    -- a position packet the map could use
    lat         REAL,
    lon         REAL,
    echo        INTEGER NOT NULL DEFAULT 0,    -- rx: 1 = its own packet, repeated by a digi
    via         TEXT,                          -- rx: the digi heard repeating it, or the node whose beacon it was
    rssi        REAL,
    snr         REAL,
    txid        INTEGER,                       -- tx: the transmission it reached APRS-IS as; NULL = not heard
    repeat_of   INTEGER,                       -- tx: an identical packet APRS-IS passed <30 s before, so it
                                               --     dropped this one as a duplicate; not counted either way
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    uploaded_at INTEGER NOT NULL,
    UNIQUE(device, kind, ts, packet)
);
CREATE INDEX IF NOT EXISTS ix_device_log ON device_log(device, kind, ts);

-- Dated notes on a device (antenna swap, setting change), shown on comparisons
CREATE TABLE IF NOT EXISTS device_events (
    id         INTEGER PRIMARY KEY,
    device     TEXT NOT NULL,                  -- callsign or mobile key
    ts         INTEGER NOT NULL,
    text       TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'web' CHECK (source IN ('web','app')),
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(device, ts, text)
);
CREATE INDEX IF NOT EXISTS ix_device_events ON device_events(device, ts);
"""


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with session() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def _migrate(c: sqlite3.Connection) -> None:
    cols = {r["name"] for r in c.execute("PRAGMA table_info(packets)")}
    if "txid" not in cols:
        c.execute("ALTER TABLE packets ADD COLUMN txid INTEGER")
    if "dupe" not in cols:
        c.execute("ALTER TABLE packets ADD COLUMN dupe INTEGER NOT NULL DEFAULT 0")
    scols = {r["name"] for r in c.execute("PRAGMA table_info(stations)")}
    if "is_server" not in scols:
        c.execute("ALTER TABLE stations ADD COLUMN is_server TEXT")
        c.execute("ALTER TABLE stations ADD COLUMN is_server_ts INTEGER")
    if "owner_id" not in scols:
        c.execute("ALTER TABLE stations ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
    if "status" not in scols:
        c.execute("ALTER TABLE stations ADD COLUMN status TEXT NOT NULL DEFAULT 'approved' "
                  "CHECK (status IN ('pending','approved','rejected'))")
        c.execute("ALTER TABLE stations ADD COLUMN review_note TEXT NOT NULL DEFAULT ''")
    if "hardware" not in scols:
        c.execute("ALTER TABLE stations ADD COLUMN hardware TEXT NOT NULL DEFAULT ''")
    c.execute("CREATE INDEX IF NOT EXISTS ix_stations_owner ON stations(owner_id)")
    # Sessions belong to a user now. Older ones (from the single shared admin
    # password) have none and are dropped: everyone logs in again.
    if "user_id" not in {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}:
        c.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
    c.execute("DELETE FROM sessions WHERE user_id IS NULL")
    # The old single admin password becomes the first admin account, "admin".
    old = c.execute("SELECT value FROM settings WHERE key='admin_password'").fetchone()
    if old:
        if not c.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
            c.execute("INSERT INTO users(username, pw_hash, role, status) VALUES('admin', ?, 'admin', 'active')",
                      (old["value"],))
        c.execute("DELETE FROM settings WHERE key='admin_password'")
    if "repeat_of" not in {r["name"] for r in c.execute("PRAGMA table_info(device_log)")}:
        c.execute("ALTER TABLE device_log ADD COLUMN repeat_of INTEGER")
    # Rows stored before duplicates were tracked are each their own transmission.
    # (The index is created here, after an old table has gained the column.)
    c.execute("CREATE INDEX IF NOT EXISTS ix_packets_txid ON packets(txid)")
    c.execute("UPDATE packets SET txid = id WHERE txid IS NULL")


def get_setting(key: str):
    with session() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with session() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
