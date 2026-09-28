"""Conector con la API de administración de un servidor Outline (shadowbox).

Se configura con el texto que muestra el instalador de Outline:
    {"apiUrl": "https://IP:PUERTO/SECRETO", "certSha256": "ABCD..."}

El certificado de Outline es autofirmado, así que en lugar de validarlo con una
autoridad se comprueba que su huella SHA-256 sea exactamente la indicada.
"""

import hashlib
import http.client
import json
import ssl
from urllib.parse import urlparse


class OutlineError(Exception):
    pass


def parse_config(text: str) -> dict:
    """Lee el texto verde del instalador. Acepta el JSON tal cual se copia."""
    try:
        data = json.loads(text.strip())
        api_url = data["apiUrl"].strip().rstrip("/")
        cert = data["certSha256"].strip().replace(":", "").upper()
    except (ValueError, KeyError, AttributeError, TypeError):
        raise OutlineError("Pega el texto completo que empieza con {\"apiUrl\" y termina con }")
    url = urlparse(api_url)
    if url.scheme != "https" or not url.hostname or len(cert) != 64:
        raise OutlineError("El texto de Outline no tiene el formato esperado")
    return {"apiUrl": api_url, "certSha256": cert}


class OutlineAPI:
    def __init__(self, api_url: str, cert_sha256: str, timeout: float = 10):
        self.url = urlparse(api_url)
        self.base_path = self.url.path.rstrip("/")
        self.cert = cert_sha256.upper()
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # se verifica la huella a mano, abajo
        conn = http.client.HTTPSConnection(
            self.url.hostname, self.url.port or 443, timeout=self.timeout, context=ctx
        )
        try:
            conn.connect()
            der = conn.sock.getpeercert(binary_form=True)
            if hashlib.sha256(der).hexdigest().upper() != self.cert:
                raise OutlineError("La huella del certificado de Outline no coincide")
            payload = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if payload else {}
            conn.request(method, self.base_path + path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status >= 400:
                raise OutlineError(f"Outline respondió {resp.status}: {raw[:200]!r}")
            return json.loads(raw) if raw else None
        except OSError as exc:
            raise OutlineError(f"No se pudo conectar con Outline: {exc}") from exc
        finally:
            conn.close()

    # ---------------------------------------------------------------- claves

    def server(self) -> dict:
        return self._request("GET", "/server")

    def create_key(self, name: str) -> dict:
        """Crea una clave y devuelve {"id", "accessUrl", ...}."""
        key = self._request("POST", "/access-keys")
        self._request("PUT", f"/access-keys/{key['id']}/name", {"name": name})
        return key

    def rename_key(self, key_id: str, name: str) -> None:
        self._request("PUT", f"/access-keys/{key_id}/name", {"name": name})

    def delete_key(self, key_id: str) -> None:
        try:
            self._request("DELETE", f"/access-keys/{key_id}")
        except OutlineError as exc:
            if "404" not in str(exc):
                raise

    def block(self, key_id: str) -> None:
        """Límite de 0 bytes: la clave sigue existiendo pero no deja pasar tráfico."""
        self._request("PUT", f"/access-keys/{key_id}/data-limit", {"limit": {"bytes": 0}})

    def unblock(self, key_id: str) -> None:
        try:
            self._request("DELETE", f"/access-keys/{key_id}/data-limit")
        except OutlineError as exc:
            if "404" not in str(exc):
                raise

    def transfer(self) -> dict[str, int]:
        """Bytes usados por cada clave (subida + bajada), según Outline."""
        data = self._request("GET", "/metrics/transfer") or {}
        return {str(k): int(v) for k, v in data.get("bytesTransferredByUserId", {}).items()}


class FakeOutline:
    """Servidor Outline simulado para pruebas y modo de desarrollo."""

    def __init__(self):
        self.keys: dict[str, dict] = {}
        self.usage: dict[str, int] = {}
        self._next = 0

    def server(self) -> dict:
        return {"name": "Outline de prueba"}

    def create_key(self, name: str) -> dict:
        key_id = str(self._next)
        self._next += 1
        key = {"id": key_id, "name": name, "blocked": False,
               "accessUrl": f"ss://Y2hhY2hhMjAtaWV0Zi1wb2x5MTMwNTpwcnVlYmE{key_id}@127.0.0.1:21028/?outline=1"}
        self.keys[key_id] = key
        return dict(key)

    def rename_key(self, key_id: str, name: str) -> None:
        self.keys[key_id]["name"] = name

    def delete_key(self, key_id: str) -> None:
        self.keys.pop(key_id, None)

    def block(self, key_id: str) -> None:
        self.keys[key_id]["blocked"] = True

    def unblock(self, key_id: str) -> None:
        self.keys[key_id]["blocked"] = False

    def transfer(self) -> dict[str, int]:
        return dict(self.usage)
