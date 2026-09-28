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

CREATE TABLE IF NOT EXISTS usage_daily (
    client_id   INTEGER NOT NULL,
    day         TEXT    NOT NULL,             -- YYYY-MM-DD
    down_bytes  INTEGER NOT NULL DEFAULT 0,
    up_bytes    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (client_id, day)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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

TRIAL_PLAN = ("Prueba gratis", 1, 0)

DEFAULT_PLANS = [
    TRIAL_PLAN,
    ("3 GB", 3, 1000),
    ("6 GB", 6, 1500),
    ("10 GB", 10, 3000),
]

# Velocidades recomendadas (bajada, subida) en Mbps. Se ponen una sola vez y sólo
# si el plan aún no tiene límite, para no pisar lo que el dueño haya configurado.
RECOMMENDED_SPEEDS = {
    "Prueba gratis": (1, 1),
    "3 GB": (5, 2),
    "6 GB": (10, 3),
    "10 GB": (50, 10),
}


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
            self._migrate(conn)
            if conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0:
                conn.executemany(
                    "INSERT INTO plans (name, gb, price_cup) VALUES (?, ?, ?)",
                    DEFAULT_PLANS,
                )
                self._mark(conn, "seed_trial_plan")
            if not self._done(conn, "seed_trial_plan"):
                # Instalaciones anteriores: se añade una sola vez el plan gratis.
                conn.execute(
                    "INSERT INTO plans (name, gb, price_cup) VALUES (?, ?, ?)", TRIAL_PLAN
                )
                self._mark(conn, "seed_trial_plan")
            if not self._done(conn, "seed_plan_speeds"):
                self._seed_speeds(conn)
                self._mark(conn, "seed_plan_speeds")

    @staticmethod
    def _seed_speeds(conn) -> None:
        for name, (down, up) in RECOMMENDED_SPEEDS.items():
            conn.execute(
                "UPDATE plans SET down_mbps = ?, up_mbps = ? "
                "WHERE name = ? AND down_mbps = 0 AND up_mbps = 0",
                (down, up, name),
            )
            # Los clientes que ya tenían ese plan y seguían sin límite también.
            conn.execute(
                "UPDATE clients SET down_mbps = ?, up_mbps = ? WHERE down_mbps = 0 "
                "AND up_mbps = 0 AND plan_id IN (SELECT id FROM plans WHERE name = ?)",
                (down, up, name),
            )

    @staticmethod
    def _migrate(conn) -> None:
        """Añade columnas nuevas a bases de datos creadas por versiones anteriores."""
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(clients)")}
        for name, ddl in (
            ("down_bytes", "INTEGER NOT NULL DEFAULT 0"),
            ("up_bytes", "INTEGER NOT NULL DEFAULT 0"),
            ("endpoint", "TEXT NOT NULL DEFAULT ''"),
            ("exit", "TEXT NOT NULL DEFAULT 'server'"),  # server | warp
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE clients ADD COLUMN {name} {ddl}")
        if "plan_id" not in columns:
            # Último plan comprado por cada cliente, deducido de sus ventas.
            conn.execute("ALTER TABLE clients ADD COLUMN plan_id INTEGER")
            conn.execute(
                "UPDATE clients SET plan_id = (SELECT p.id FROM sales s "
                "JOIN plans p ON p.name = s.plan_name WHERE s.client_id = clients.id "
                "ORDER BY s.id DESC LIMIT 1)"
            )

    @staticmethod
    def _done(conn, key: str) -> bool:
        return conn.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone() is not None

    @staticmethod
    def _mark(conn, key: str) -> None:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')", (key,))
