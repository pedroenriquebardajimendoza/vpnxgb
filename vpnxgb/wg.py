"""Control de WireGuard (peers) y de velocidades con `tc`.

`WireGuard` ejecuta comandos reales. Con dry_run=True sólo los registra en
`self.commands`, lo que permite probar el panel en cualquier máquina.
"""

import base64
import ipaddress
import logging
import os
import subprocess
from dataclasses import dataclass

log = logging.getLogger("vpnxgb.wg")


# ---------------------------------------------------------------- claves

_P = 2 ** 255 - 19
_A24 = 121665


def _x25519(k: bytes, u: bytes) -> bytes:
    """Multiplicación escalar X25519 (RFC 7748), la misma que usa `wg pubkey`."""
    kb = bytearray(k)
    kb[0] &= 248
    kb[31] &= 127
    kb[31] |= 64
    scalar = int.from_bytes(kb, "little")
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in reversed(range(255)):
        bit = (scalar >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = (x2 + z2) % _P, (x2 - z2) % _P
        aa, bb = a * a % _P, b * b % _P
        e = (aa - bb) % _P
        c, d = (x3 + z3) % _P, (x3 - z3) % _P
        da, cb = d * a % _P, c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def public_key_from_private(private_b64: str) -> str:
    base_point = (9).to_bytes(32, "little")
    return base64.b64encode(_x25519(base64.b64decode(private_b64), base_point)).decode()


def generate_keypair() -> tuple[str, str]:
    """Devuelve (clave_privada, clave_publica) en base64, como `wg genkey`."""
    raw = bytearray(os.urandom(32))
    raw[0] &= 248
    raw[31] &= 127
    raw[31] |= 64
    private = base64.b64encode(bytes(raw)).decode()
    return private, public_key_from_private(private)


def generate_preshared_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


# ---------------------------------------------------------------- estado

@dataclass
class PeerStats:
    public_key: str
    rx: int              # bytes recibidos por el servidor (subida del cliente)
    tx: int              # bytes enviados por el servidor (bajada del cliente)
    latest_handshake: int


def parse_dump(output: str) -> dict[str, PeerStats]:
    """Interpreta la salida de `wg show <iface> dump`.

    La primera línea es la interfaz; las demás son peers con columnas:
    public-key, preshared-key, endpoint, allowed-ips, latest-handshake,
    transfer-rx, transfer-tx, persistent-keepalive
    """
    peers: dict[str, PeerStats] = {}
    lines = [line for line in output.splitlines() if line.strip()]
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) < 8:
            continue
        peers[cols[0]] = PeerStats(
            public_key=cols[0],
            latest_handshake=int(cols[4]),
            rx=int(cols[5]),
            tx=int(cols[6]),
        )
    return peers


# ---------------------------------------------------------------- velocidad

@dataclass
class Shaping:
    client_id: int
    address: str       # 10.8.0.5
    down_mbps: float   # 0 = sin límite
    up_mbps: float


def _rate(mbps: float) -> str:
    return f"{int(round(mbps * 1000))}kbit"


def build_tc_commands(iface: str, ifb: str, rules: list[Shaping]) -> list[list[str]]:
    """Genera los comandos `tc` que dejan los límites exactamente como `rules`.

    - Bajada del cliente = tráfico que SALE por la interfaz wg (filtro por IP destino).
    - Subida del cliente = tráfico que ENTRA por wg; se redirige a la interfaz
      ifb para poder limitarlo (filtro por IP origen).
    Siempre se borra todo y se vuelve a crear: es simple y fiable.
    """
    cmds: list[list[str]] = [
        ["tc", "qdisc", "del", "dev", iface, "root"],
        ["tc", "qdisc", "del", "dev", iface, "ingress"],
        ["tc", "qdisc", "del", "dev", ifb, "root"],
    ]
    down = [r for r in rules if r.down_mbps > 0]
    up = [r for r in rules if r.up_mbps > 0]

    if down:
        cmds += [
            ["tc", "qdisc", "add", "dev", iface, "root", "handle", "1:", "htb", "default", "ffff"],
            ["tc", "class", "add", "dev", iface, "parent", "1:", "classid", "1:ffff",
             "htb", "rate", "10gbit"],
        ]
        for r in down:
            cid = f"1:{r.client_id + 1:x}"
            cmds += [
                ["tc", "class", "add", "dev", iface, "parent", "1:", "classid", cid,
                 "htb", "rate", _rate(r.down_mbps), "ceil", _rate(r.down_mbps)],
                ["tc", "qdisc", "add", "dev", iface, "parent", cid, "fq_codel"],
                ["tc", "filter", "add", "dev", iface, "parent", "1:", "protocol", "ip",
                 "prio", "1", "u32", "match", "ip", "dst", f"{r.address}/32", "flowid", cid],
            ]

    if up:
        cmds += [
            ["ip", "link", "add", ifb, "type", "ifb"],
            ["ip", "link", "set", ifb, "up"],
            ["tc", "qdisc", "add", "dev", iface, "handle", "ffff:", "ingress"],
            ["tc", "filter", "add", "dev", iface, "parent", "ffff:", "protocol", "ip",
             "u32", "match", "u32", "0", "0", "action", "mirred", "egress",
             "redirect", "dev", ifb],
            ["tc", "qdisc", "add", "dev", ifb, "root", "handle", "2:", "htb", "default", "ffff"],
            ["tc", "class", "add", "dev", ifb, "parent", "2:", "classid", "2:ffff",
             "htb", "rate", "10gbit"],
        ]
        for r in up:
            cid = f"2:{r.client_id + 1:x}"
            cmds += [
                ["tc", "class", "add", "dev", ifb, "parent", "2:", "classid", cid,
                 "htb", "rate", _rate(r.up_mbps), "ceil", _rate(r.up_mbps)],
                ["tc", "qdisc", "add", "dev", ifb, "parent", cid, "fq_codel"],
                ["tc", "filter", "add", "dev", ifb, "parent", "2:", "protocol", "ip",
                 "prio", "1", "u32", "match", "ip", "src", f"{r.address}/32", "flowid", cid],
            ]
    return cmds


# ---------------------------------------------------------------- control

class WireGuard:
    def __init__(self, interface: str, ifb: str = "ifb0", dry_run: bool = False):
        self.interface = interface
        self.ifb = ifb
        self.dry_run = dry_run
        self.commands: list[list[str]] = []
        # En dry_run se simulan los peers y sus contadores.
        self.fake_peers: dict[str, PeerStats] = {}

    def _run(self, cmd: list[str], check: bool = True, stdin: str | None = None) -> str:
        self.commands.append(cmd)
        if self.dry_run:
            return ""
        res = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
        if check and res.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)}: {res.stderr.strip()}")
        return res.stdout

    def public_key(self) -> str:
        if self.dry_run:
            return "SERVIDOR_CLAVE_PUBLICA_DE_PRUEBA="
        return self._run(["wg", "show", self.interface, "public-key"]).strip()

    def add_peer(self, public_key: str, preshared_key: str, address: str) -> None:
        if self.dry_run:
            self.commands.append(["wg", "set", self.interface, "peer", public_key])
            self.fake_peers.setdefault(public_key, PeerStats(public_key, 0, 0, 0))
            return
        # La clave precompartida se pasa por stdin para no dejarla en disco.
        self._run(
            ["wg", "set", self.interface, "peer", public_key,
             "preshared-key", "/dev/stdin", "allowed-ips", f"{address}/32"],
            stdin=preshared_key + "\n",
        )

    def remove_peer(self, public_key: str) -> None:
        if self.dry_run:
            self.commands.append(["wg", "set", self.interface, "peer", public_key, "remove"])
            self.fake_peers.pop(public_key, None)
            return
        self._run(["wg", "set", self.interface, "peer", public_key, "remove"])

    def stats(self) -> dict[str, PeerStats]:
        if self.dry_run:
            return dict(self.fake_peers)
        return parse_dump(self._run(["wg", "show", self.interface, "dump"]))

    def apply_shaping(self, rules: list[Shaping]) -> None:
        for cmd in build_tc_commands(self.interface, self.ifb, rules):
            # Los "del" / "ip link add" fallan si no existe/ya existe: se ignora.
            tolerant = cmd[2] == "del" or cmd[:3] == ["ip", "link", "add"]
            try:
                self._run(cmd, check=not tolerant)
            except RuntimeError as exc:
                log.error("tc: %s", exc)


def allocate_address(network: str, server_address: str, used: set[str]) -> str:
    net = ipaddress.ip_network(network, strict=False)
    for host in net.hosts():
        ip = str(host)
        if ip != server_address and ip not in used:
            return ip
    raise RuntimeError("No quedan IPs libres en la red de WireGuard")
