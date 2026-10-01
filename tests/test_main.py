from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from monitor import auth
from monitor.aggregator import Aggregator
from monitor.main import create_app
from tests.helpers import FakeJWKS, SETTINGS, make_token, raw_event


def ahora():
    """Hora real: el agregador marca 'detenido' según el reloj, así que los eventos no pueden tener hora fija."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(auth, "get_jwks_client", lambda: FakeJWKS())
    agg = Aggregator()
    agg.apply(raw_event("flow_started", sid="abcdef123456", ts=ahora()))
    return TestClient(create_app(SETTINGS, aggregator=agg, start_background=False))


def bearer(groups):
    return {"Authorization": f"Bearer {make_token(groups)}"}


def test_health_es_publico(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and "replaying" in r.json()


def test_config_json_publico_solo_con_datos_no_secretos(client):
    r = client.get("/config.json")
    assert r.status_code == 200
    assert r.json() == {"cognitoDomain": "login.example.com", "clientId": "client-123", "region": "us-east-1",
                        "apiBase": ""}    # vacío: el front llama a su mismo origen


def test_metricas_sin_token_es_401(client):
    assert client.get("/api/monitor/metrics").status_code == 401


@pytest.mark.parametrize("grupos", [["ADMIN"], ["TEACHER"], [], ["us-east-1_pool_Google"]])
def test_metricas_para_quien_no_es_superadmin_es_403(client, grupos):
    assert client.get("/api/monitor/metrics", headers=bearer(grupos)).status_code == 403


def test_metricas_para_superadmin_devuelve_el_resumen(client):
    r = client.get("/api/monitor/metrics", headers=bearer(["SUPERADMIN"]))
    assert r.status_code == 200
    body = r.json()
    assert body["flows"]["running"] == 1 and body["recent"][0]["id"] == "abcdef12"
    assert {"tokens", "cost", "avg", "by_agent", "archive", "replaying", "skipped_events"} <= set(body)
    assert r.headers["cache-control"] == "no-store"


def test_el_front_se_sirve_en_las_rutas_del_spa(client):
    for ruta in ("/", "/login", "/auth/callback"):
        r = client.get(ruta)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_cabeceras_de_seguridad_y_csp_limitada_al_origen_y_a_cognito(client):
    h = client.get("/").headers
    csp = h["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert "connect-src 'self' https://login.example.com" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "no-referrer"
    assert h["x-frame-options"] == "DENY"


def test_los_assets_estaticos_existen(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


# ── Modo solo API (SERVE_FRONT=false): en producción el front vive en Amplify ──────────────────────
@pytest.fixture
def api_only(monkeypatch):
    import dataclasses
    monkeypatch.setattr(auth, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(auth, "get_jwks_client", lambda: FakeJWKS())
    agg = Aggregator()
    agg.apply(raw_event("flow_started", sid="abcdef123456", ts=ahora()))
    return TestClient(create_app(dataclasses.replace(SETTINGS, serve_front=False), aggregator=agg, start_background=False))


@pytest.mark.parametrize("ruta", ["/", "/login", "/auth/callback", "/config.json", "/static/app.js", "/static/style.css"])
def test_solo_api_no_expone_el_front_ni_su_configuracion(api_only, ruta):
    assert api_only.get(ruta).status_code == 404


def test_solo_api_mantiene_health_y_la_api_protegida(api_only):
    assert api_only.get("/health").status_code == 200
    assert api_only.get("/api/monitor/metrics").status_code == 401
    assert api_only.get("/api/monitor/metrics", headers=bearer(["ADMIN"])).status_code == 403
    r = api_only.get("/api/monitor/metrics", headers=bearer(["SUPERADMIN"]))
    assert r.status_code == 200 and r.json()["flows"]["running"] == 1


@pytest.mark.parametrize("cliente", ["client", "api_only"])
def test_el_monitor_nunca_emite_cors_lo_hace_el_api_gateway(request, cliente):
    """Si el monitor también emitiera CORS, el navegador vería dos Access-Control-Allow-Origin y rechazaría
    la respuesta. En producción el dueño del CORS es solo el API Gateway."""
    c = request.getfixturevalue(cliente)
    r = c.get("/api/monitor/metrics", headers={**bearer(["SUPERADMIN"]), "Origin": "https://monitor.amplifyapp.com"})
    assert r.status_code == 200
    assert not any(h.lower().startswith("access-control-") for h in r.headers)
