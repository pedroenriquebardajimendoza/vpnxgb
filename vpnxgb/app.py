"""Panel web (FastAPI)."""

import io
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import verify_password
from .config import Settings, load_settings
from .db import Database
from .service import GB, Panel, PanelError, now, parse_time
from .wg import WireGuard

log = logging.getLogger("vpnxgb")
HERE = os.path.dirname(__file__)

STATUS_LABELS = {
    "activo": "Activo",
    "pausado": "Pausado",
    "agotado": "Agotado",
    "vencido": "Vencido",
}


def human_bytes(n: int | float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.2f} {unit}" if unit == "GB" else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def speed_label(mbps: float) -> str:
    if not mbps:
        return "Sin límite"
    return f"{mbps:g} Mbps"


def cup(amount: int | float) -> str:
    return f"{int(amount or 0):,}".replace(",", " ")


def ago(timestamp: int) -> str:
    if not timestamp:
        return "Nunca"
    secs = int(now().timestamp()) - int(timestamp)
    if secs < 180:
        return "En línea"
    if secs < 3600:
        return f"hace {secs // 60} min"
    if secs < 86400:
        return f"hace {secs // 3600} h"
    return f"hace {secs // 86400} días"


def build_panel(settings: Settings | None = None) -> Panel:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    db.init()
    wg = WireGuard(settings.wg_interface, settings.ifb_interface, dry_run=settings.dry_run)
    return Panel(settings, db, wg)


def create_app(settings: Settings | None = None, start_worker: bool = True) -> FastAPI:
    settings = settings or load_settings()
    panel = build_panel(settings)
    stop = threading.Event()

    def worker():
        while not stop.wait(settings.poll_seconds):
            try:
                panel.collect_usage()
            except Exception:
                log.exception("Error leyendo el consumo")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            panel.sync()
        except Exception:
            log.exception("No se pudo sincronizar WireGuard al arrancar")
        thread = None
        if start_worker:
            thread = threading.Thread(target=worker, daemon=True, name="vpnxgb-usage")
            thread.start()
        yield
        stop.set()

    app = FastAPI(title="VPNxGB", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.panel = panel
    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path.startswith("/static") or path == "/login":
            return await call_next(request)
        if not request.session.get("user"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    # Se añade después para que envuelva a require_login (request.session).
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="vpnxgb",
        same_site="strict",
        max_age=7 * 24 * 3600,
    )
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

    templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
    templates.env.filters["bytes"] = human_bytes
    templates.env.filters["speed"] = speed_label
    templates.env.filters["ago"] = ago
    templates.env.filters["cup"] = cup
    templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
    templates.env.globals["GB"] = GB

    # ------------------------------------------------------------ helpers

    def render(request: Request, name: str, **ctx):
        ctx["flash"] = request.session.pop("flash", None)
        ctx["user"] = request.session.get("user")
        return templates.TemplateResponse(request, name, ctx)

    def flash(request: Request, message: str, kind: str = "ok"):
        request.session["flash"] = {"message": message, "kind": kind}

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def run(request: Request, url: str, action, ok_message: str):
        try:
            action()
            flash(request, ok_message)
        except PanelError as exc:
            flash(request, str(exc), "error")
        except Exception as exc:  # errores de wg/tc, etc.
            log.exception("Error en acción")
            flash(request, f"Error: {exc}", "error")
        return back(url)

    def to_float(value: str, default: float = 0.0) -> float:
        try:
            return float(str(value).replace(",", "."))
        except ValueError:
            return default

    # ------------------------------------------------------------ login

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return render(request, "login.html")

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...)):
        if not settings.admin_password_hash:
            flash(request, "No hay contraseña configurada (VPNXGB_ADMIN_PASSWORD_HASH).", "error")
            return back("/login")
        if username == settings.admin_user and verify_password(password, settings.admin_password_hash):
            request.session.clear()
            request.session["user"] = username
            return back("/")
        flash(request, "Usuario o contraseña incorrectos", "error")
        return back("/login")

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return back("/login")

    # ------------------------------------------------------------ inicio

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return render(
            request,
            "dashboard.html",
            summary=panel.summary(),
            recent=panel.list_sales(limit=8),
            plans=panel.list_plans(only_active=True),
        )

    @app.post("/refresh")
    def refresh(request: Request, next: str = Form("/")):
        url = next if next.startswith("/") and not next.startswith("//") else "/"
        return run(request, url, panel.collect_usage, "Consumo actualizado")

    # ------------------------------------------------------------ clientes

    @app.get("/clients", response_class=HTMLResponse)
    def clients(request: Request, status: str = "", q: str = ""):
        return render(
            request,
            "clients.html",
            clients=panel.list_clients(status or None, q.strip()),
            status=status,
            q=q,
        )

    @app.get("/clients/new", response_class=HTMLResponse)
    def client_new_form(request: Request, plan: int = 0):
        return render(request, "client_new.html",
                      plans=panel.list_plans(only_active=True), selected=plan)

    @app.post("/clients/new")
    def client_new(request: Request, name: str = Form(...), plan_id: int = Form(...),
                   note: str = Form("")):
        try:
            client_id = panel.create_client(name, plan_id, note)
        except PanelError as exc:
            flash(request, str(exc), "error")
            return back("/clients/new")
        flash(request, "Cliente creado. Envíale el QR o el archivo .conf")
        return back(f"/clients/{client_id}")

    @app.get("/clients/{client_id}", response_class=HTMLResponse)
    def client_detail(request: Request, client_id: int):
        try:
            client = panel.get_client(client_id)
        except PanelError as exc:
            flash(request, str(exc), "error")
            return back("/clients")
        sales = [s for s in panel.list_sales(limit=1000) if s["client_id"] == client_id]
        return render(
            request,
            "client.html",
            c=client,
            config=panel.client_config(client),
            plans=panel.list_plans(only_active=True),
            sales=sales,
            expires=parse_time(client["expires_at"]),
        )

    def _filename(client) -> str:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", client["name"]).strip("-")[:20] or "cliente"
        return f"{slug}-{client['id']}.conf"

    @app.get("/clients/{client_id}/config")
    def client_config(client_id: int):
        client = panel.get_client(client_id)
        return Response(
            panel.client_config(client),
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{_filename(client)}"'},
        )

    @app.get("/clients/{client_id}/qr.svg")
    def client_qr(client_id: int):
        client = panel.get_client(client_id)
        img = qrcode.make(panel.client_config(client),
                          image_factory=qrcode.image.svg.SvgPathFillImage, box_size=8)
        buf = io.BytesIO()
        img.save(buf)
        return Response(buf.getvalue(), media_type="image/svg+xml")

    @app.post("/clients/{client_id}/pause")
    def client_pause(request: Request, client_id: int):
        return run(request, f"/clients/{client_id}", lambda: panel.pause(client_id),
                   "Cliente pausado")

    @app.post("/clients/{client_id}/resume")
    def client_resume(request: Request, client_id: int):
        return run(request, f"/clients/{client_id}", lambda: panel.resume(client_id),
                   "Cliente activado")

    @app.post("/clients/{client_id}/delete")
    def client_delete(request: Request, client_id: int):
        return run(request, "/clients", lambda: panel.delete_client(client_id),
                   "Cliente eliminado")

    @app.post("/clients/{client_id}/recharge")
    def client_recharge(request: Request, client_id: int, plan_id: int = Form(...)):
        return run(request, f"/clients/{client_id}", lambda: panel.recharge(client_id, plan_id),
                   "Recarga aplicada")

    @app.post("/clients/{client_id}/adjust")
    def client_adjust(request: Request, client_id: int, gb: str = Form(...)):
        amount = to_float(gb)
        return run(request, f"/clients/{client_id}", lambda: panel.adjust_gb(client_id, amount),
                   f"Saldo ajustado en {amount:+g} GB")

    @app.post("/clients/{client_id}/reset")
    def client_reset(request: Request, client_id: int):
        return run(request, f"/clients/{client_id}", lambda: panel.reset_usage(client_id),
                   "Consumo puesto a cero")

    @app.post("/clients/{client_id}/speed")
    def client_speed(request: Request, client_id: int, down_mbps: str = Form("0"),
                     up_mbps: str = Form("0")):
        down, up = to_float(down_mbps), to_float(up_mbps)
        return run(request, f"/clients/{client_id}", lambda: panel.set_speed(client_id, down, up),
                   "Velocidad actualizada")

    @app.post("/clients/{client_id}/expiry")
    def client_expiry(request: Request, client_id: int, expires: str = Form("")):
        def action():
            value = None
            if expires.strip():
                try:
                    value = datetime.strptime(expires.strip(), "%Y-%m-%d").replace(
                        hour=23, minute=59, second=59)
                except ValueError:
                    raise PanelError("Fecha inválida")
            panel.set_expiry(client_id, value)

        return run(request, f"/clients/{client_id}", action, "Vencimiento actualizado")

    @app.post("/clients/{client_id}/info")
    def client_info(request: Request, client_id: int, name: str = Form(...),
                    note: str = Form("")):
        return run(request, f"/clients/{client_id}",
                   lambda: panel.update_info(client_id, name, note), "Datos guardados")

    # ------------------------------------------------------------ planes

    @app.get("/plans", response_class=HTMLResponse)
    def plans(request: Request):
        return render(request, "plans.html", plans=panel.list_plans())

    @app.post("/plans")
    def plan_save(request: Request, plan_id: int = Form(0), name: str = Form(...),
                  gb: str = Form(...), price_cup: str = Form(...), days: str = Form("0"),
                  down_mbps: str = Form("0"), up_mbps: str = Form("0"),
                  active: str = Form("")):
        return run(
            request,
            "/plans",
            lambda: panel.save_plan(
                plan_id or None, name, to_float(gb), int(to_float(price_cup)),
                int(to_float(days)), to_float(down_mbps), to_float(up_mbps), bool(active),
            ),
            "Plan guardado",
        )

    @app.post("/plans/{plan_id}/delete")
    def plan_delete(request: Request, plan_id: int):
        return run(request, "/plans", lambda: panel.delete_plan(plan_id), "Plan eliminado")

    # ------------------------------------------------------------ ventas

    @app.get("/sales", response_class=HTMLResponse)
    def sales(request: Request):
        return render(request, "sales.html", sales=panel.list_sales(limit=500),
                      summary=panel.summary())

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    host = os.environ.get("VPNXGB_HOST", "0.0.0.0")
    port = int(os.environ.get("VPNXGB_PORT", "8080"))
    uvicorn.run(create_app(), host=host, port=port, proxy_headers=True)


if __name__ == "__main__":
    main()
