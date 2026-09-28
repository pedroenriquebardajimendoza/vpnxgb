# VPNxGB — Vende WireGuard por GB

Panel web para vender configuraciones de **WireGuard por paquetes de datos**
(3 GB, 6 GB, 10 GB…) en lugar de mensualidades.

## Qué hace

- **Planes**: vienen cuatro por defecto, que puedes cambiar o ampliar desde el panel:
  | Plan  | Precio   |
  |-------|----------|
  | Prueba gratis (1 GB) | 0 CUP |
  | 3 GB  | 1000 CUP |
  | 6 GB  | 1500 CUP |
  | 10 GB | 3000 CUP |

  Cada plan puede tener, de forma opcional, **días de validez** (0 = no vence)
  y una **velocidad por defecto**.
- **Clientes**: creas un cliente eligiendo un plan y el panel genera al momento su
  **QR** y su archivo **.conf**. La venta queda registrada.
- **Corte automático**: cada minuto el panel lee el consumo (subida + bajada).
  Cuando el cliente gasta su paquete, se desconecta solo (estado *Agotado*).
- **Recargas**: vendes otro paquete al mismo cliente, se suman los GB y se reactiva.
  El cliente **no necesita una configuración nueva**.
- **Pausar / activar / eliminar** configuraciones.
- **Velocidad**: puedes limitar la bajada y la subida (Mbps) de cada cliente cuando quieras.
- **Ajustes manuales**: regalar o quitar GB, cambiar la fecha de vencimiento,
  poner el consumo a cero.
- **Ventas**: lo vendido hoy y en el mes (en CUP) y el historial de cada cliente.
- **Monitor en vivo (estilo MikroTik)**: velocidad actual de bajada y subida de
  cada cliente (se actualiza cada 2 s), totales de bajada y subida, IP real desde
  donde se conecta, última conexión, CPU, RAM, disco y tráfico del mes del VPS.
- **Historial**: gráfica de consumo por día (30 días) de cada cliente y del
  servidor completo, y la lista de los que más consumen hoy.
- Diseño pensado para usarlo **desde el móvil**.

## Instalación en el VPS

Necesitas un VPS con **Ubuntu 22.04/24.04 o Debian 11/12**, fuera de Cuba y con IP pública.

```bash
# Conéctate como root al VPS
apt-get update && apt-get install -y git
git clone https://github.com/pedroenriquebardajimendoza/vpnxgb.git
cd vpnxgb
bash install.sh
```

El instalador te pide la IP pública, el puerto de WireGuard, el puerto del panel,
el usuario y la contraseña. Al terminar te muestra la dirección del panel, por ejemplo
`http://TU_IP:8080`.

Recomendado: pon el reloj del servidor en hora de Cuba para que las ventas
"de hoy" cuadren:

```bash
timedatectl set-timezone America/Havana && systemctl restart vpnxgb
```

Si tu VPS incluye otro tráfico mensual (1000 GB en el Droplet de 6 $ de
DigitalOcean), cámbialo en `/etc/vpnxgb/vpnxgb.env` con
`VPNXGB_MONTHLY_TRAFFIC_GB=1000` y reinicia: `systemctl restart vpnxgb`.

### Actualizar

```bash
cd vpnxgb && git pull && bash install.sh
```

Actualizar no toca los clientes, las ventas ni las claves.

## Uso diario

1. **Vender a un cliente nuevo**: *Inicio → toca el plan* (o *Clientes → Nuevo*),
   escribe el nombre y pulsa *Crear*. Envíale el QR (captura de pantalla) o el `.conf`.
2. **El cliente instala la app WireGuard** (Android / iPhone / PC), pulsa **+** y
   escanea el QR o importa el archivo.
3. **Cuando se le acaben los GB**, abre su ficha y usa **Recargar**.

## Cómo funciona (técnico)

- WireGuard (`wg0`, red `10.8.0.0/24`). Los peers no se guardan en `wg0.conf`:
  la base de datos SQLite (`/var/lib/vpnxgb/vpnxgb.db`) es la fuente de verdad, y
  al arrancar el panel añade a la interfaz los clientes activos.
- El consumo se calcula con `wg show wg0 dump` y se va **sumando** en la base de datos.
  Así no se pierde aunque se reinicie la interfaz o el VPS.
- Los límites de velocidad se aplican con `tc` (HTB):
  - La bajada se limita en `wg0` (filtro por IP destino).
  - La subida se limita en `ifb0` (el tráfico de entrada de `wg0` se redirige allí).
- El panel es FastAPI + Jinja2 y funciona como servicio systemd (`vpnxgb`).
  La configuración está en `/etc/vpnxgb/vpnxgb.env`.

Comandos útiles:

```bash
systemctl status vpnxgb          # estado del panel
journalctl -u vpnxgb -f          # logs
wg show                          # peers conectados
tc -s class show dev wg0         # límites de bajada
```

## Seguridad

- El panel usa HTTP normal. Para mayor seguridad, ponlo detrás de HTTPS (por ejemplo,
  con Caddy y un dominio) o entra por un túnel SSH:
  `ssh -L 8080:localhost:8080 root@TU_IP` y abre `http://localhost:8080`.
- Si no usas HTTPS, no entres al panel desde redes públicas.

## Notas para vender en Cuba

- En algunas redes de ETECSA el tráfico UDP de WireGuard puede ir mal o estar
  bloqueado. **Pruébalo primero con tu propia línea.** Cambiar el puerto (por
  ejemplo, a `443`) a veces ayuda.
- Revisa cuánto tráfico mensual incluye tu VPS para que el precio por GB te deje ganancia.
- 1 GB en el panel equivale a 1024³ bytes.

## Desarrollo

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
# panel de prueba sin WireGuard real:
VPNXGB_DRY_RUN=1 VPNXGB_DB=./dev.db \
VPNXGB_ADMIN_PASSWORD_HASH="$(echo demo | python -m vpnxgb.cli hash-password)" \
python -m vpnxgb.app     # http://localhost:8080  (admin / demo)
```
