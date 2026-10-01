"""Ayudantes de tests: firma tokens RS256 como lo haría Cognito y falsifica el JWKS."""
import time
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from monitor.settings import Settings

POOL = "us-east-1_pool"
CLIENT = "client-123"
SETTINGS = Settings(cognito_user_pool_id=POOL, cognito_client_id=CLIENT, cognito_domain="login.example.com")
ISSUER = SETTINGS.issuer

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJWKS:
    """Sustituye a PyJWKClient: siempre entrega la clave pública de prueba."""

    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=_KEY.public_key())


def make_token(groups=("SUPERADMIN",), *, token_use="id", aud=CLIENT, iss=ISSUER, exp_in=3600,
               alg="RS256", key=None, extra=None):
    now = int(time.time())
    payload = {"sub": "u1", "email": "a@b.cl", "token_use": token_use, "aud": aud, "iss": iss,
               "iat": now, "exp": now + exp_in}
    if groups is not None:
        payload["cognito:groups"] = list(groups)
    payload.update(extra or {})
    if alg == "HS256":
        return jwt.encode(payload, "secreto-de-prueba-suficientemente-largo-32b", algorithm="HS256")
    return jwt.encode(payload, key or _KEY, algorithm="RS256", headers={"kid": "k1"})


def foreign_key():
    return _OTHER_KEY


def raw_event(type, sid="s1", ts="2026-09-29T14:00:00.000Z", **campos):
    """Evento v1 tal como lo publica el workflow (dict listo para json.dumps)."""
    return {"v": 1, "type": type, "session_id": sid, "ts": ts, **campos}
