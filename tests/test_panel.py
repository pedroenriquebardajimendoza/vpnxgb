import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from vpnxgb import service
from vpnxgb.app import create_app
from vpnxgb.auth import hash_password, verify_password
from vpnxgb.config import load_settings
from vpnxgb.service import GB
from vpnxgb.wg import PeerStats, Shaping, build_tc_commands, parse_dump


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("VPNXGB_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("VPNXGB_DRY_RUN", "1")
    monkeypatch.setenv("VPNXGB_ADMIN_PASSWORD_HASH", hash_password("secreto"))
    monkeypatch.setenv("VPNXGB_SERVER_ENDPOINT", "1.2.3.4:51820")
    return load_settings()


@pytest.fixture
def app(settings):
    return create_app(settings, start_worker=False)


@pytest.fixture
def panel(app):
    return app.state.panel


def plan_id(panel, name):
    return next(p["id"] for p in panel.list_plans() if p["name"] == name)


def use(panel, client_id, rx, tx):
    """Simula que el cliente ha consumido tráfico (contadores acumulados de wg)."""
    pk = panel.get_client(client_id)["public_key"]
    panel.wg.fake_peers[pk] = PeerStats(pk, rx, tx, 1)


def test_default_plans(panel):
    plans = {p["name"]: (p["gb"], p["price_cup"]) for p in panel.list_plans()}
    assert plans == {
        "Prueba gratis": (1, 0), "3 GB": (3, 1000), "6 GB": (6, 1500), "10 GB": (10, 3000)
    }


def test_create_client_and_sale(panel):
    cid = panel.create_client("Juan", plan_id(panel, "3 GB"), "53555")
    c = panel.get_client(cid)
    assert c["status"] == "activo"
    assert c["quota_bytes"] == 3 * GB
    assert c["address"] == "10.8.0.2"
    assert c["public_key"] in panel.wg.fake_peers
    cfg = panel.client_config(c)
    assert "Endpoint = 1.2.3.4:51820" in cfg
    assert "Address = 10.8.0.2/24" in cfg
    sales = panel.list_sales()
    assert len(sales) == 1 and sales[0]["price_cup"] == 1000
    assert panel.summary()["today"]["cup"] == 1000


def test_usage_cutoff_and_recharge(panel):
    cid = panel.create_client("Ana", plan_id(panel, "3 GB"))
    use(panel, cid, 1 * GB, 1 * GB)
    panel.collect_usage()
    assert panel.get_client(cid)["used_bytes"] == 2 * GB
    assert panel.get_client(cid)["status"] == "activo"

    use(panel, cid, 1 * GB + GB // 2, 1 * GB + GB // 2)
    panel.collect_usage()
    c = panel.get_client(cid)
    assert c["used_bytes"] == 3 * GB
    assert c["status"] == "agotado"
    assert c["public_key"] not in panel.wg.fake_peers

    panel.recharge(cid, plan_id(panel, "6 GB"))
    c = panel.get_client(cid)
    assert c["status"] == "activo"
    assert c["quota_bytes"] == 9 * GB
    assert c["public_key"] in panel.wg.fake_peers
    assert panel.summary()["today"]["cup"] == 2500


def test_counter_reset_is_handled(panel):
    cid = panel.create_client("Luis", plan_id(panel, "10 GB"))
    use(panel, cid, 500, 500)
    panel.collect_usage()
    use(panel, cid, 100, 100)  # la interfaz se reinició: contadores menores
    panel.collect_usage()
    assert panel.get_client(cid)["used_bytes"] == 1200


def test_pause_resume_delete(panel):
    cid = panel.create_client("Eva", plan_id(panel, "3 GB"))
    use(panel, cid, 1000, 0)
    panel.pause(cid)
    c = panel.get_client(cid)
    assert c["status"] == "pausado" and c["used_bytes"] == 1000
    assert c["public_key"] not in panel.wg.fake_peers
    panel.collect_usage()  # pausado: no cuenta
    panel.resume(cid)
    assert panel.get_client(cid)["status"] == "activo"
    panel.delete_client(cid)
    assert panel.list_clients() == []
    assert panel.list_sales()[0]["client_id"] is None


def test_expiry(panel):
    panel.save_plan(None, "1 GB semana", 1, 300, 7, 0, 0, True)
    cid = panel.create_client("Rosa", plan_id(panel, "1 GB semana"))
    assert panel.get_client(cid)["expires_at"]
    panel.set_expiry(cid, service.now() - timedelta(minutes=1))
    assert panel.get_client(cid)["status"] == "vencido"
    panel.recharge(cid, plan_id(panel, "1 GB semana"))
    assert panel.get_client(cid)["status"] == "activo"


def test_speed_limit_commands(panel):
    cid = panel.create_client("Pepe", plan_id(panel, "3 GB"))
    panel.wg.commands.clear()
    panel.set_speed(cid, 2, 1)
    flat = [" ".join(c) for c in panel.wg.commands]
    assert any("rate 2000kbit" in c and "wg0" in c for c in flat)
    assert any("rate 1000kbit" in c and "ifb0" in c for c in flat)
    assert any("match ip dst 10.8.0.2/32" in c for c in flat)
    assert any("match ip src 10.8.0.2/32" in c for c in flat)


def test_build_tc_no_limits_only_cleans():
    cmds = build_tc_commands("wg0", "ifb0", [])
    assert all(c[2] == "del" for c in cmds)
    cmds = build_tc_commands("wg0", "ifb0", [Shaping(1, "10.8.0.2", 5, 0)])
    assert not any("ifb0" in c and "add" in c for c in cmds)


def test_parse_dump():
    out = (
        "privkey\tpubkey\t51820\toff\n"
        "PEER1=\t(none)\t1.1.1.1:5000\t10.8.0.2/32\t1700000000\t100\t200\t25\n"
    )
    peers = parse_dump(out)
    assert peers["PEER1="].rx == 100 and peers["PEER1="].tx == 200


def test_password():
    h = hash_password("abc")
    assert verify_password("abc", h) and not verify_password("abd", h)


def test_web_flow(app, panel):
    with TestClient(app) as web:
        assert web.get("/", follow_redirects=False).status_code == 303
        r = web.post("/login", data={"username": "admin", "password": "mala"})
        assert "incorrectos" in r.text
        web.post("/login", data={"username": "admin", "password": "secreto"})
        assert "Ventas hoy" in web.get("/").text

        r = web.post("/clients/new", data={"name": "Web", "plan_id": plan_id(panel, "6 GB")})
        m = re.search(r"/clients/(\d+)/qr.svg", r.text)
        assert m, r.text
        cid = int(m.group(1))
        assert web.get(f"/clients/{cid}/qr.svg").headers["content-type"].startswith("image/svg")
        conf = web.get(f"/clients/{cid}/config")
        assert "[Interface]" in conf.text

        web.post(f"/clients/{cid}/speed", data={"down_mbps": "3", "up_mbps": "1,5"})
        c = panel.get_client(cid)
        assert (c["down_mbps"], c["up_mbps"]) == (3, 1.5)
        web.post(f"/clients/{cid}/pause")
        assert panel.get_client(cid)["status"] == "pausado"
        web.post(f"/clients/{cid}/adjust", data={"gb": "1"})
        assert panel.get_client(cid)["quota_bytes"] == 7 * GB
        web.post("/plans", data={"name": "20 GB", "gb": "20", "price_cup": "5000"})
        assert "20 GB" in web.get("/plans").text
        for page in ("/clients", "/sales", f"/clients/{cid}"):
            assert web.get(page).status_code == 200


def test_x25519_rfc7748_vector():
    # Vector de prueba de la sección 6.1 del RFC 7748 (clave pública de Alice).
    import base64

    from vpnxgb.wg import public_key_from_private

    priv = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    pub = public_key_from_private(base64.b64encode(priv).decode())
    assert base64.b64decode(pub).hex() == (
        "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    )


def test_down_up_split_and_daily_history(panel):
    cid = panel.create_client("Hist", plan_id(panel, "10 GB"))
    use(panel, cid, 100, 300)  # rx = subida 100, tx = bajada 300
    panel.collect_usage()
    c = panel.get_client(cid)
    assert (c["down_bytes"], c["up_bytes"], c["used_bytes"]) == (300, 100, 400)
    days = panel.history(cid, days=30)
    assert len(days) == 30 and days[-1]["down"] == 300 and days[-1]["up"] == 100
    assert panel.history(days=7)[-1]["down"] == 300
    top = panel.top_consumers()
    assert top[0]["name"] == "Hist" and top[0]["total"] == 400
    assert panel.month_traffic() == 400


def test_live_rates_and_pending_counters(panel, monkeypatch):
    cid = panel.create_client("Live", plan_id(panel, "3 GB"))
    pk = panel.get_client(cid)["public_key"]
    clock = [1000.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])
    panel.wg.fake_peers[pk] = PeerStats(pk, 0, 0, int(service.now().timestamp()), "5.6.7.8:1234")
    panel.live()
    clock[0] += 2
    panel.wg.fake_peers[pk] = PeerStats(pk, 2000, 10000, int(service.now().timestamp()), "5.6.7.8:1234")
    row = panel.live(cid)[0]
    assert row["down_rate"] == 5000 and row["up_rate"] == 1000
    assert row["down_bytes"] == 10000 and row["up_bytes"] == 2000  # aún sin collect_usage
    assert row["online"] and row["endpoint"] == "5.6.7.8:1234"


def test_migration_adds_trial_plan_once(tmp_path):
    import sqlite3

    from vpnxgb.db import Database

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE plans (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, gb REAL, "
        "price_cup INTEGER, days INTEGER DEFAULT 0, down_mbps REAL DEFAULT 0, "
        "up_mbps REAL DEFAULT 0, active INTEGER DEFAULT 1);"
        "INSERT INTO plans (name, gb, price_cup) VALUES ('3 GB', 3, 1000);"
        "CREATE TABLE clients (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, note TEXT, "
        "private_key TEXT, public_key TEXT, preshared_key TEXT, address TEXT, "
        "quota_bytes INTEGER, used_bytes INTEGER, down_mbps REAL, up_mbps REAL, status TEXT, "
        "expires_at TEXT, created_at TEXT, last_rx INTEGER, last_tx INTEGER, "
        "last_handshake INTEGER);"
    )
    conn.close()
    db = Database(str(path))
    db.init()
    db.init()
    with db.connect() as c:
        names = [r["name"] for r in c.execute("SELECT name FROM plans")]
        cols = {r["name"] for r in c.execute("PRAGMA table_info(clients)")}
    assert names.count("Prueba gratis") == 1
    assert {"down_bytes", "up_bytes", "endpoint"} <= cols


def test_monitor_pages(app, panel):
    panel.create_client("Mon", plan_id(panel, "Prueba gratis"))
    with TestClient(app) as web:
        assert web.get("/api/live").status_code == 401
        web.post("/login", data={"username": "admin", "password": "secreto"})
        data = web.get("/api/live").json()
        assert data["clients"][0]["name"] == "Mon" and "server" in data
        assert "Monitor" in web.get("/monitor").text
        assert "GRATIS" in web.get("/").text
        assert "Gratis" in web.get("/sales").text


def test_live_rates_when_counters_mutate_in_place(panel, monkeypatch):
    cid = panel.create_client("Mut", plan_id(panel, "3 GB"))
    pk = panel.get_client(cid)["public_key"]
    clock = [50.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])
    peer = panel.wg.fake_peers[pk]
    panel.live()
    clock[0] += 1
    peer.tx += 4000
    assert panel.live(cid)[0]["down_rate"] == 4000
