"""Configuración del panel, leída de variables de entorno.

En producción el instalador escribe estas variables en /etc/vpnxgb/vpnxgb.env
y el servicio systemd las carga.
"""

import os
from dataclasses import dataclass


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "si", "sí")


@dataclass
class Settings:
    db_path: str
    wg_interface: str
    wg_network: str          # red interna de los clientes, ej. 10.8.0.0/24
    server_address: str      # IP del servidor dentro de la red, ej. 10.8.0.1
    server_endpoint: str     # IP_PUBLICA:PUERTO que se pone en los .conf
    server_public_key: str   # vacío = se lee con `wg show <iface> public-key`
    client_dns: str
    client_allowed_ips: str
    persistent_keepalive: int
    admin_user: str
    admin_password_hash: str
    secret_key: str
    dry_run: bool            # True = no ejecuta wg/tc (para desarrollo y tests)
    poll_seconds: int
    ifb_interface: str
    monthly_traffic_gb: float  # tráfico incluido en el VPS (para el aviso del mes)


def load_settings() -> Settings:
    env = os.environ.get
    return Settings(
        db_path=env("VPNXGB_DB", "/var/lib/vpnxgb/vpnxgb.db"),
        wg_interface=env("VPNXGB_WG_INTERFACE", "wg0"),
        wg_network=env("VPNXGB_WG_NETWORK", "10.8.0.0/24"),
        server_address=env("VPNXGB_SERVER_ADDRESS", "10.8.0.1"),
        server_endpoint=env("VPNXGB_SERVER_ENDPOINT", "127.0.0.1:51820"),
        server_public_key=env("VPNXGB_SERVER_PUBLIC_KEY", ""),
        client_dns=env("VPNXGB_CLIENT_DNS", "1.1.1.1, 8.8.8.8"),
        client_allowed_ips=env("VPNXGB_CLIENT_ALLOWED_IPS", "0.0.0.0/0"),
        persistent_keepalive=int(env("VPNXGB_KEEPALIVE", "25")),
        admin_user=env("VPNXGB_ADMIN_USER", "admin"),
        admin_password_hash=env("VPNXGB_ADMIN_PASSWORD_HASH", ""),
        secret_key=env("VPNXGB_SECRET_KEY", "cambia-esta-clave"),
        dry_run=_bool(env("VPNXGB_DRY_RUN", "0")),
        poll_seconds=int(env("VPNXGB_POLL_SECONDS", "60")),
        ifb_interface=env("VPNXGB_IFB_INTERFACE", "ifb0"),
        monthly_traffic_gb=float(env("VPNXGB_MONTHLY_TRAFFIC_GB", "1000")),
    )
