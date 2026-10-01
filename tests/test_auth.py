import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jwt import PyJWKClientConnectionError, PyJWKClientError

from monitor import auth
from tests.helpers import FakeJWKS, SETTINGS, foreign_key, make_token


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(auth, "get_jwks_client", lambda: FakeJWKS())
    app = FastAPI()

    @app.get("/x")
    def x(user: dict = Depends(auth.require_superadmin)):
        return {"email": user["email"], "role": user["role"]}

    return TestClient(app)


def call(client, token=None, scheme="Bearer"):
    headers = {"Authorization": f"{scheme} {token}"} if token else {}
    return client.get("/x", headers=headers)


def test_sin_token_es_401(client):
    assert call(client).status_code == 401


def test_esquema_distinto_de_bearer_es_401(client):
    assert call(client, make_token(), scheme="Basic").status_code == 401


def test_superadmin_pasa(client):
    r = call(client, make_token(["SUPERADMIN"]))
    assert r.status_code == 200 and r.json() == {"email": "a@b.cl", "role": "SUPERADMIN"}


def test_el_grupo_autogenerado_del_idp_no_es_un_rol_pero_no_impide_ser_superadmin(client):
    assert call(client, make_token([f"{'us-east-1_pool'}_Google", "SUPERADMIN"])).status_code == 200


def test_solo_el_grupo_del_idp_es_pending_y_da_403(client):
    r = call(client, make_token(["us-east-1_pool_Google"]))
    assert r.status_code == 403


def test_sin_grupos_es_pending_403(client):
    assert call(client, make_token(None)).status_code == 403


@pytest.mark.parametrize("rol", ["ADMIN", "TEACHER"])
def test_otros_roles_reales_tambien_dan_403(client, rol):
    assert call(client, make_token([rol])).status_code == 403


def test_access_token_es_401(client):
    assert call(client, make_token(token_use="access")).status_code == 401


def test_token_expirado_es_401(client):
    assert call(client, make_token(exp_in=-10)).status_code == 401


def test_audiencia_o_emisor_incorrectos_son_401(client):
    assert call(client, make_token(aud="otro-client")).status_code == 401
    assert call(client, make_token(iss="https://evil.example.com/pool")).status_code == 401


def test_firma_de_otra_clave_es_401(client):
    assert call(client, make_token(key=foreign_key())).status_code == 401


def test_algoritmo_hs256_es_401(client):
    assert call(client, make_token(alg="HS256")).status_code == 401


def test_kid_desconocido_es_401(client, monkeypatch):
    class Roto:
        def get_signing_key_from_jwt(self, token):
            raise PyJWKClientError("kid no encontrado")
    monkeypatch.setattr(auth, "get_jwks_client", lambda: Roto())
    assert call(client, make_token()).status_code == 401


def test_jwks_inalcanzable_es_503_no_401(client, monkeypatch):
    class Caido:
        def get_signing_key_from_jwt(self, token):
            raise PyJWKClientConnectionError("sin red")
    monkeypatch.setattr(auth, "get_jwks_client", lambda: Caido())
    assert call(client, make_token()).status_code == 503


def test_sin_configuracion_de_cognito_es_500(client, monkeypatch):
    from monitor.settings import Settings
    monkeypatch.setattr(auth, "get_settings", lambda: Settings())
    assert call(client, make_token()).status_code == 500
