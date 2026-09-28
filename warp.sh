#!/usr/bin/env bash
# Instala Cloudflare WARP como salida opcional para los clientes del panel.
# Uso (como root, dentro de la carpeta del proyecto):   bash warp.sh
#
# Crea una interfaz WireGuard "warp" conectada a Cloudflare con `Table = off`:
# NO cambia la salida del servidor. Sólo los clientes marcados en el panel como
# "Salida: Cloudflare" se envían por ella (regla `from IP lookup 51`).
set -euo pipefail

WARP_IF=warp
WARP_CONF=/etc/wireguard/$WARP_IF.conf
WG_NET=10.8.0.0/24
TABLE=51
WORK=/etc/vpnxgb/warp

# Escribe en stdout la configuración de la interfaz a partir del perfil de wgcf.
make_conf() {
  local prof=$1 key pub endpoint mtu addr ip
  key=$(awk '/^PrivateKey/ {sub(/^PrivateKey *= */, ""); print; exit}' "$prof")
  pub=$(awk '/^PublicKey/ {sub(/^PublicKey *= */, ""); print; exit}' "$prof")
  endpoint=$(awk '/^Endpoint/ {sub(/^Endpoint *= */, ""); print; exit}' "$prof")
  mtu=$(awk '/^MTU/ {sub(/^MTU *= */, ""); print; exit}' "$prof")
  addr=$(grep '^Address' "$prof" | tr ',= ' '\n\n\n' | grep -E '^[0-9]+(\.[0-9]+){3}/[0-9]+$' | head -1 || true)
  ip=${addr%/*}
  if [[ -z $key || -z $pub || -z $endpoint || -z $addr ]]; then
    echo "Perfil de WARP incompleto" >&2
    return 1
  fi
  cat <<CONF
# Generado por warp.sh de VPNxGB. Sólo la usan los clientes con salida Cloudflare.
[Interface]
PrivateKey = $key
Address = $addr
MTU = ${mtu:-1280}
Table = off
PostUp = ip route replace default dev %i table $TABLE; ip rule add from $ip lookup $TABLE priority 5099; iptables -t nat -A POSTROUTING -s $WG_NET -o %i -j MASQUERADE; iptables -t mangle -A FORWARD -o %i -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu; sysctl -qw net.ipv4.conf.%i.rp_filter=2
PostDown = ip rule del from $ip lookup $TABLE priority 5099; ip route flush table $TABLE; iptables -t nat -D POSTROUTING -s $WG_NET -o %i -j MASQUERADE; iptables -t mangle -D FORWARD -o %i -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu

[Peer]
PublicKey = $pub
AllowedIPs = 0.0.0.0/0
Endpoint = $endpoint
PersistentKeepalive = 25
CONF
}

# Permite probar make_conf sin instalar nada:  source warp.sh --only-functions
[[ ${1:-} == --only-functions ]] && return 0

if [[ $EUID -ne 0 ]]; then
  echo "Ejecuta este script como root (sudo bash warp.sh)" >&2
  exit 1
fi

mkdir -p "$WORK"
cd "$WORK"

if [[ ! -x /usr/local/bin/wgcf ]]; then
  echo "==> Descargando wgcf (cliente no oficial de Cloudflare WARP)"
  case "$(uname -m)" in
    x86_64) ARCH=amd64 ;;
    aarch64) ARCH=arm64 ;;
    *) echo "Arquitectura no soportada: $(uname -m)" >&2; exit 1 ;;
  esac
  URL=$(curl -fsSL https://api.github.com/repos/ViRb3/wgcf/releases/latest \
        | grep -o "https://[^\"]*linux_${ARCH}" | head -1)
  [[ -n $URL ]] || { echo "No se encontró la descarga de wgcf" >&2; exit 1; }
  curl -fsSL "$URL" -o /usr/local/bin/wgcf
  chmod +x /usr/local/bin/wgcf
fi

if [[ ! -f wgcf-account.toml ]]; then
  echo "==> Registrando una cuenta gratuita de WARP"
  wgcf register --accept-tos
fi
wgcf generate >/dev/null

echo "==> Creando la interfaz $WARP_IF"
make_conf wgcf-profile.conf > "$WARP_CONF.new"
mv "$WARP_CONF.new" "$WARP_CONF"
chmod 600 "$WARP_CONF"

systemctl enable "wg-quick@$WARP_IF" >/dev/null 2>&1
systemctl restart "wg-quick@$WARP_IF"
# El panel vuelve a poner las reglas de los clientes con salida Cloudflare.
systemctl restart vpnxgb 2>/dev/null || true

sleep 2
echo
echo "==> Comprobando la salida por Cloudflare"
TRACE=$(curl -fsS --max-time 10 --interface "$WARP_IF" https://www.cloudflare.com/cdn-cgi/trace || true)
if echo "$TRACE" | grep -q "warp=on"; then
  echo "$TRACE" | grep -E "^(ip|loc|warp)="
  echo
  echo "Cloudflare WARP funciona. En el panel, abre un cliente y elige 'Salida: Cloudflare'."
else
  echo "WARP no respondió todavía. Revisa con: wg show $WARP_IF   y   journalctl -u wg-quick@$WARP_IF"
  exit 1
fi
