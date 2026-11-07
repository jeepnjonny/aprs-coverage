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
    lat         REAL,                          -- manual position (igate/digi)
    lon         REAL,
    auto_lat    REAL,                          -- last position beaconed by the station itself
    auto_lon    REAL,
    last_beacon INTEGER,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
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
    raw       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_packets_ts       ON packets(ts);
CREATE INDEX IF NOT EXISTS ix_packets_mobile   ON packets(mobile, ts);
CREATE INDEX IF NOT EXISTS ix_packets_heard_by ON packets(heard_by, ts);

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

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    expires    INTEGER NOT NULL
);
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
