"""Lógica del negocio: clientes, planes, recargas, consumo y límites."""

import logging
import threading
from datetime import datetime, timedelta

from .config import Settings
from .db import Database
from .wg import (
    Shaping,
    WireGuard,
    allocate_address,
    generate_keypair,
    generate_preshared_key,
)

log = logging.getLogger("vpnxgb.service")

GB = 1024 ** 3
TIME_FMT = "%Y-%m-%d %H:%M:%S"

ACTIVE = "activo"
PAUSED = "pausado"
EXHAUSTED = "agotado"
EXPIRED = "vencido"


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def parse_time(value: str | None) -> datetime | None:
    return datetime.strptime(value, TIME_FMT) if value else None


class PanelError(Exception):
    """Error de uso que se muestra al administrador."""


class Panel:
    def __init__(self, settings: Settings, db: Database, wg: WireGuard):
        self.settings = settings
        self.db = db
        self.wg = wg
        self.lock = threading.RLock()
        self._server_public_key = settings.server_public_key

    # ------------------------------------------------------------ utilidades

    @property
    def server_public_key(self) -> str:
        if not self._server_public_key:
            self._server_public_key = self.wg.public_key()
        return self._server_public_key

    def _client(self, conn, client_id: int):
        row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if row is None:
            raise PanelError("Cliente no encontrado")
        return row

    def _plan(self, conn, plan_id: int):
        row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise PanelError("Plan no encontrado")
        return row

    def _reshape(self, conn) -> None:
        rows = conn.execute(
            "SELECT id, address, down_mbps, up_mbps FROM clients "
            "WHERE status = ? AND (down_mbps > 0 OR up_mbps > 0)",
            (ACTIVE,),
        ).fetchall()
        self.wg.apply_shaping(
            [Shaping(r["id"], r["address"], r["down_mbps"], r["up_mbps"]) for r in rows]
        )

    def _disconnect(self, conn, client) -> None:
        """Quita el peer de la interfaz. Sus contadores en WireGuard se pierden,
        por eso se reinician también los de referencia."""
        self.wg.remove_peer(client["public_key"])
        conn.execute("UPDATE clients SET last_rx = 0, last_tx = 0 WHERE id = ?", (client["id"],))

    def _connect(self, conn, client) -> None:
        self.wg.add_peer(client["public_key"], client["preshared_key"], client["address"])
        conn.execute("UPDATE clients SET last_rx = 0, last_tx = 0 WHERE id = ?", (client["id"],))

    @staticmethod
    def _natural_status(client) -> str:
        """Estado que le corresponde a un cliente que no está pausado."""
        if client["used_bytes"] >= client["quota_bytes"]:
            return EXHAUSTED
        expires = parse_time(client["expires_at"])
        if expires and expires <= now():
            return EXPIRED
        return ACTIVE

    def _set_status(self, conn, client_id: int, status: str) -> None:
        """Cambia el estado y conecta/desconecta el peer según haga falta."""
        client = self._client(conn, client_id)
        was_active = client["status"] == ACTIVE
        conn.execute("UPDATE clients SET status = ? WHERE id = ?", (status, client_id))
        if was_active and status != ACTIVE:
            self._disconnect(conn, client)
        elif not was_active and status == ACTIVE:
            self._connect(conn, client)

    # ------------------------------------------------------------ arranque

    def sync(self) -> None:
        """Deja la interfaz WireGuard igual que la base de datos.
        Se llama al arrancar el panel (tras un reinicio del VPS, etc.)."""
        with self.lock, self.db.connect() as conn:
            active = {
                r["public_key"]: r
                for r in conn.execute("SELECT * FROM clients WHERE status = ?", (ACTIVE,))
            }
            current = self.wg.stats()
            for pk in current:
                if pk not in active:
                    self.wg.remove_peer(pk)
            for pk, client in active.items():
                if pk not in current:
                    self._connect(conn, client)
            self._reshape(conn)

    # ------------------------------------------------------------ consumo

    def collect_usage(self) -> None:
        """Suma el tráfico nuevo de cada cliente y corta a quien se pase.
        Se ejecuta cada `poll_seconds` en segundo plano."""
        with self.lock, self.db.connect() as conn:
            stats = self.wg.stats()
            changed = False
            for client in conn.execute(
                "SELECT * FROM clients WHERE status = ?", (ACTIVE,)
            ).fetchall():
                peer = stats.get(client["public_key"])
                if peer is None:
                    # Falta en la interfaz (p. ej. reinicio de wg0): se vuelve a añadir.
                    self._connect(conn, client)
                    continue
                # Si el contador bajó, la interfaz se reinició: todo lo actual es nuevo.
                d_rx = peer.rx - client["last_rx"] if peer.rx >= client["last_rx"] else peer.rx
                d_tx = peer.tx - client["last_tx"] if peer.tx >= client["last_tx"] else peer.tx
                conn.execute(
                    "UPDATE clients SET used_bytes = used_bytes + ?, last_rx = ?, last_tx = ?, "
                    "last_handshake = ? WHERE id = ?",
                    (d_rx + d_tx, peer.rx, peer.tx, peer.latest_handshake, client["id"]),
                )
                client = self._client(conn, client["id"])
                status = self._natural_status(client)
                if status != ACTIVE:
                    log.info("Cliente %s -> %s", client["name"], status)
                    self._set_status(conn, client["id"], status)
                    changed = True
            if changed:
                self._reshape(conn)

    # ------------------------------------------------------------ planes

    def list_plans(self, only_active: bool = False):
        with self.db.connect() as conn:
            sql = "SELECT * FROM plans"
            if only_active:
                sql += " WHERE active = 1"
            return conn.execute(sql + " ORDER BY gb").fetchall()

    def save_plan(self, plan_id: int | None, name: str, gb: float, price_cup: int,
                  days: int, down_mbps: float, up_mbps: float, active: bool) -> None:
        if not name.strip() or gb <= 0 or price_cup < 0 or days < 0:
            raise PanelError("Datos del plan inválidos")
        values = (name.strip(), gb, price_cup, days, max(down_mbps, 0), max(up_mbps, 0), int(active))
        with self.db.connect() as conn:
            if plan_id:
                conn.execute(
                    "UPDATE plans SET name=?, gb=?, price_cup=?, days=?, down_mbps=?, up_mbps=?, "
                    "active=? WHERE id=?",
                    values + (plan_id,),
                )
            else:
                conn.execute(
                    "INSERT INTO plans (name, gb, price_cup, days, down_mbps, up_mbps, active) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    values,
                )

    def delete_plan(self, plan_id: int) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))

    # ------------------------------------------------------------ clientes

    def list_clients(self, status: str | None = None, search: str = ""):
        sql = "SELECT * FROM clients WHERE 1=1"
        args: list = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if search:
            sql += " AND (name LIKE ? OR note LIKE ? OR address LIKE ?)"
            args += [f"%{search}%"] * 3
        with self.db.connect() as conn:
            return conn.execute(sql + " ORDER BY id DESC", args).fetchall()

    def get_client(self, client_id: int):
        with self.db.connect() as conn:
            return self._client(conn, client_id)

    def _record_sale(self, conn, client_id, client_name, plan, kind) -> None:
        conn.execute(
            "INSERT INTO sales (client_id, client_name, plan_name, kind, gb, price_cup, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (client_id, client_name, plan["name"], kind, plan["gb"], plan["price_cup"],
             now().strftime(TIME_FMT)),
        )

    def create_client(self, name: str, plan_id: int, note: str = "") -> int:
        name = name.strip()
        if not name:
            raise PanelError("El nombre es obligatorio")
        with self.lock, self.db.connect() as conn:
            plan = self._plan(conn, plan_id)
            used = {r["address"] for r in conn.execute("SELECT address FROM clients")}
            address = allocate_address(self.settings.wg_network, self.settings.server_address, used)
            private, public = generate_keypair()
            expires = (now() + timedelta(days=plan["days"])).strftime(TIME_FMT) if plan["days"] else None
            cur = conn.execute(
                "INSERT INTO clients (name, note, private_key, public_key, preshared_key, address, "
                "quota_bytes, down_mbps, up_mbps, status, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, note.strip(), private, public, generate_preshared_key(), address,
                 int(plan["gb"] * GB), plan["down_mbps"], plan["up_mbps"], ACTIVE, expires,
                 now().strftime(TIME_FMT)),
            )
            client_id = cur.lastrowid
            self._record_sale(conn, client_id, name, plan, "nuevo")
            self._connect(conn, self._client(conn, client_id))
            self._reshape(conn)
            return client_id

    def recharge(self, client_id: int, plan_id: int) -> None:
        """Vende un paquete a un cliente existente: suma GB (y días si el plan caduca)."""
        with self.lock, self.db.connect() as conn:
            client = self._client(conn, client_id)
            plan = self._plan(conn, plan_id)
            quota = client["quota_bytes"]
            if client["status"] == EXPIRED:
                # Los GB que quedaban del paquete vencido se pierden.
                quota = client["used_bytes"]
            quota += int(plan["gb"] * GB)
            if plan["days"]:
                start = max(now(), parse_time(client["expires_at"]) or now())
                expires = (start + timedelta(days=plan["days"])).strftime(TIME_FMT)
            else:
                expires = None
            conn.execute(
                "UPDATE clients SET quota_bytes = ?, expires_at = ? WHERE id = ?",
                (quota, expires, client_id),
            )
            self._record_sale(conn, client_id, client["name"], plan, "recarga")
            client = self._client(conn, client_id)
            if client["status"] != PAUSED:
                self._set_status(conn, client_id, self._natural_status(client))
            self._reshape(conn)

    def adjust_gb(self, client_id: int, gb: float) -> None:
        """Suma o resta GB a mano (regalos, correcciones). No cuenta como venta."""
        with self.lock, self.db.connect() as conn:
            client = self._client(conn, client_id)
            quota = max(client["quota_bytes"] + int(gb * GB), 0)
            conn.execute("UPDATE clients SET quota_bytes = ? WHERE id = ?", (quota, client_id))
            client = self._client(conn, client_id)
            if client["status"] != PAUSED:
                self._set_status(conn, client_id, self._natural_status(client))
            self._reshape(conn)

    def reset_usage(self, client_id: int) -> None:
        with self.lock, self.db.connect() as conn:
            conn.execute("UPDATE clients SET used_bytes = 0 WHERE id = ?", (client_id,))
            client = self._client(conn, client_id)
            if client["status"] != PAUSED:
                self._set_status(conn, client_id, self._natural_status(client))
            self._reshape(conn)

    def set_expiry(self, client_id: int, expires: datetime | None) -> None:
        with self.lock, self.db.connect() as conn:
            value = expires.strftime(TIME_FMT) if expires else None
            conn.execute("UPDATE clients SET expires_at = ? WHERE id = ?", (value, client_id))
            client = self._client(conn, client_id)
            if client["status"] != PAUSED:
                self._set_status(conn, client_id, self._natural_status(client))
            self._reshape(conn)

    def set_speed(self, client_id: int, down_mbps: float, up_mbps: float) -> None:
        with self.lock, self.db.connect() as conn:
            self._client(conn, client_id)
            conn.execute(
                "UPDATE clients SET down_mbps = ?, up_mbps = ? WHERE id = ?",
                (max(down_mbps, 0), max(up_mbps, 0), client_id),
            )
            self._reshape(conn)

    def update_info(self, client_id: int, name: str, note: str) -> None:
        if not name.strip():
            raise PanelError("El nombre es obligatorio")
        with self.db.connect() as conn:
            self._client(conn, client_id)
            conn.execute(
                "UPDATE clients SET name = ?, note = ? WHERE id = ?",
                (name.strip(), note.strip(), client_id),
            )

    def pause(self, client_id: int) -> None:
        self.collect_usage()  # guarda lo consumido antes de quitar el peer
        with self.lock, self.db.connect() as conn:
            self._set_status(conn, client_id, PAUSED)
            self._reshape(conn)

    def resume(self, client_id: int) -> None:
        with self.lock, self.db.connect() as conn:
            client = self._client(conn, client_id)
            status = self._natural_status(client)
            self._set_status(conn, client_id, status)
            self._reshape(conn)
        if status != ACTIVE:
            raise PanelError(f"El cliente no se puede activar: está {status}. Recárgalo.")

    def delete_client(self, client_id: int) -> None:
        with self.lock, self.db.connect() as conn:
            client = self._client(conn, client_id)
            if client["status"] == ACTIVE:
                self.wg.remove_peer(client["public_key"])
            conn.execute("UPDATE sales SET client_id = NULL WHERE client_id = ?", (client_id,))
            conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
            self._reshape(conn)

    def client_config(self, client) -> str:
        s = self.settings
        prefix = s.wg_network.split("/")[1] if "/" in s.wg_network else "32"
        lines = [
            "[Interface]",
            f"PrivateKey = {client['private_key']}",
            f"Address = {client['address']}/{prefix}",
        ]
        if s.client_dns:
            lines.append(f"DNS = {s.client_dns}")
        lines += [
            "",
            "[Peer]",
            f"PublicKey = {self.server_public_key}",
            f"PresharedKey = {client['preshared_key']}",
            f"AllowedIPs = {s.client_allowed_ips}",
            f"Endpoint = {s.server_endpoint}",
        ]
        if s.persistent_keepalive:
            lines.append(f"PersistentKeepalive = {s.persistent_keepalive}")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------ ventas

    def list_sales(self, limit: int = 200):
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT * FROM sales ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def summary(self) -> dict:
        with self.db.connect() as conn:
            counts = {
                r["status"]: r["n"]
                for r in conn.execute("SELECT status, COUNT(*) AS n FROM clients GROUP BY status")
            }
            today = now().strftime("%Y-%m-%d")
            month = now().strftime("%Y-%m")

            def sold(prefix: str):
                r = conn.execute(
                    "SELECT COALESCE(SUM(price_cup), 0) AS cup, COALESCE(SUM(gb), 0) AS gb, "
                    "COUNT(*) AS n FROM sales WHERE created_at LIKE ?",
                    (prefix + "%",),
                ).fetchone()
                return {"cup": r["cup"], "gb": r["gb"], "n": r["n"]}

            used = conn.execute("SELECT COALESCE(SUM(used_bytes), 0) FROM clients").fetchone()[0]
            online_since = int(now().timestamp()) - 180
            online = conn.execute(
                "SELECT COUNT(*) FROM clients WHERE status = ? AND last_handshake > ?",
                (ACTIVE, online_since),
            ).fetchone()[0]
            return {
                "counts": counts,
                "total": sum(counts.values()),
                "online": online,
                "today": sold(today),
                "month": sold(month),
                "used_bytes": used,
            }
