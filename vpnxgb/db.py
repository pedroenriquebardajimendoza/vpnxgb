"""Base de datos SQLite: planes, clientes y ventas."""

import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    gb          REAL    NOT NULL,
    price_cup   INTEGER NOT NULL,
    days        INTEGER NOT NULL DEFAULT 0,   -- 0 = no caduca
    down_mbps   REAL    NOT NULL DEFAULT 0,   -- 0 = sin límite
    up_mbps     REAL    NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS clients (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    note           TEXT    NOT NULL DEFAULT '',
    private_key    TEXT    NOT NULL,
    public_key     TEXT    NOT NULL UNIQUE,
    preshared_key  TEXT    NOT NULL,
    address        TEXT    NOT NULL UNIQUE,
    quota_bytes    INTEGER NOT NULL DEFAULT 0,
    used_bytes     INTEGER NOT NULL DEFAULT 0,
    down_mbps      REAL    NOT NULL DEFAULT 0,
    up_mbps        REAL    NOT NULL DEFAULT 0,
    status         TEXT    NOT NULL DEFAULT 'activo', -- activo|pausado|agotado|vencido
    expires_at     TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    last_rx        INTEGER NOT NULL DEFAULT 0,
    last_tx        INTEGER NOT NULL DEFAULT 0,
    last_handshake INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sales (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER,
    client_name TEXT    NOT NULL,
    plan_name   TEXT    NOT NULL,
    kind        TEXT    NOT NULL,             -- nuevo|recarga
    gb          REAL    NOT NULL,
    price_cup   INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

DEFAULT_PLANS = [
    ("3 GB", 3, 1000),
    ("6 GB", 6, 1500),
    ("10 GB", 10, 3000),
]


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            if conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0:
                conn.executemany(
                    "INSERT INTO plans (name, gb, price_cup) VALUES (?, ?, ?)",
                    DEFAULT_PLANS,
                )
