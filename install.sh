#!/usr/bin/env bash
# Instalador de VPNxGB para Ubuntu 22.04/24.04 y Debian 11/12.
# Uso (como root, dentro de la carpeta del proyecto):   bash install.sh
# Volver a ejecutarlo actualiza el código sin tocar clientes ni claves.
set -euo pipefail

APP_DIR=/opt/vpnxgb
DATA_DIR=/var/lib/vpnxgb
ENV_DIR=/etc/vpnxgb
ENV_FILE=$ENV_DIR/vpnxgb.env
WG_IF=wg0
WG_CONF=/etc/wireguard/$WG_IF.conf
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Ejecuta este script como root (sudo bash install.sh)" >&2
  exit 1
fi

echo "==> Instalando paquetes del sistema"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y wireguard wireguard-tools iproute2 iptables python3 python3-venv python3-pip curl

echo "==> Activando reenvío de paquetes y módulo ifb (límite de subida)"
cat > /etc/sysctl.d/99-vpnxgb.conf <<EOF
net.ipv4.ip_forward = 1
EOF
sysctl --system >/dev/null
echo ifb > /etc/modules-load.d/vpnxgb.conf
modprobe ifb numifbs=0 2>/dev/null || modprobe ifb || true

WAN_IF=$(ip route get 1.1.1.1 | awk '{for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')

if [[ ! -f $ENV_FILE ]]; then
  echo
  echo "==> Configuración inicial"
  PUBLIC_IP=$(curl -4 -s --max-time 10 https://ifconfig.me || true)
  read -rp "IP pública o dominio del servidor [$PUBLIC_IP]: " ENDPOINT_HOST
  ENDPOINT_HOST=${ENDPOINT_HOST:-$PUBLIC_IP}
  read -rp "Puerto UDP de WireGuard [51820]: " WG_PORT
  WG_PORT=${WG_PORT:-51820}
  read -rp "Puerto del panel web [8080]: " PANEL_PORT
  PANEL_PORT=${PANEL_PORT:-8080}
  read -rp "Usuario del panel [admin]: " ADMIN_USER
  ADMIN_USER=${ADMIN_USER:-admin}
  while true; do
    read -rsp "Contraseña del panel: " ADMIN_PASS; echo
    read -rsp "Repite la contraseña: " ADMIN_PASS2; echo
    [[ -n $ADMIN_PASS && $ADMIN_PASS == "$ADMIN_PASS2" ]] && break
    echo "Las contraseñas no coinciden o están vacías, prueba otra vez."
  done
else
  echo "==> Configuración existente encontrada en $ENV_FILE (se conserva)"
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  WG_PORT=${VPNXGB_SERVER_ENDPOINT##*:}
fi

echo "==> Configurando WireGuard ($WG_IF)"
mkdir -p /etc/wireguard
chmod 700 /etc/wireguard
if [[ ! -f $WG_CONF ]]; then
  SERVER_KEY=$(wg genkey)
  cat > "$WG_CONF" <<EOF
# Generado por VPNxGB. Los clientes (peers) los añade el panel; no los pongas aquí.
[Interface]
Address = 10.8.0.1/24
ListenPort = $WG_PORT
PrivateKey = $SERVER_KEY
PostUp = iptables -t nat -A POSTROUTING -s 10.8.0.0/24 -o $WAN_IF -j MASQUERADE; iptables -A FORWARD -i %i -j ACCEPT; iptables -A FORWARD -o %i -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -s 10.8.0.0/24 -o $WAN_IF -j MASQUERADE; iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -j ACCEPT
EOF
  chmod 600 "$WG_CONF"
fi
systemctl enable --now "wg-quick@$WG_IF"

echo "==> Instalando el panel en $APP_DIR"
mkdir -p "$APP_DIR" "$DATA_DIR" "$ENV_DIR"
if [[ $SRC_DIR != "$APP_DIR" ]]; then
  rm -rf "$APP_DIR/vpnxgb"
  cp -r "$SRC_DIR/vpnxgb" "$SRC_DIR/requirements.txt" "$APP_DIR/"
fi
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

if [[ ! -f $ENV_FILE ]]; then
  PASS_HASH=$(printf '%s\n' "$ADMIN_PASS" | (cd "$APP_DIR" && "$APP_DIR/venv/bin/python" -m vpnxgb.cli hash-password))
  SECRET=$(head -c 32 /dev/urandom | base64 | tr -d '\n')
  cat > "$ENV_FILE" <<EOF
VPNXGB_DB=$DATA_DIR/vpnxgb.db
VPNXGB_WG_INTERFACE=$WG_IF
VPNXGB_WG_NETWORK=10.8.0.0/24
VPNXGB_SERVER_ADDRESS=10.8.0.1
VPNXGB_SERVER_ENDPOINT=$ENDPOINT_HOST:$WG_PORT
VPNXGB_CLIENT_DNS="1.1.1.1, 8.8.8.8"
VPNXGB_CLIENT_ALLOWED_IPS=0.0.0.0/0
VPNXGB_KEEPALIVE=25
VPNXGB_ADMIN_USER=$ADMIN_USER
VPNXGB_ADMIN_PASSWORD_HASH='$PASS_HASH'
VPNXGB_SECRET_KEY='$SECRET'
VPNXGB_POLL_SECONDS=60
VPNXGB_HOST=0.0.0.0
VPNXGB_PORT=$PANEL_PORT
EOF
  chmod 600 "$ENV_FILE"
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

echo "==> Creando servicio systemd"
cat > /etc/systemd/system/vpnxgb.service <<EOF
[Unit]
Description=VPNxGB - panel de WireGuard por GB
After=network-online.target wg-quick@$WG_IF.service
Requires=wg-quick@$WG_IF.service

[Service]
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python -m vpnxgb.app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable vpnxgb
systemctl restart vpnxgb

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  echo "==> Abriendo puertos en ufw"
  ufw allow "$WG_PORT/udp"
  ufw allow "$VPNXGB_PORT/tcp"
fi

HOST=${VPNXGB_SERVER_ENDPOINT%:*}
echo
echo "============================================================"
echo " VPNxGB instalado."
echo " Panel:      http://$HOST:$VPNXGB_PORT"
echo " Usuario:    $VPNXGB_ADMIN_USER"
echo " WireGuard:  $VPNXGB_SERVER_ENDPOINT (UDP)"
echo " Logs:       journalctl -u vpnxgb -f"
echo "============================================================"
