"""Guard de Cognito: solo el rol SUPERADMIN entra al monitor.

Misma validación que el resto del sistema (ver prisma_workflow/prisma_agents/api/auth.py):
JWKS del pool, RS256, `iss`, `aud` = client id y `token_use` = "id" (el front manda el ID token).

El rol sale de `cognito:groups` por LISTA BLANCA. Cognito agrega un grupo autogenerado por cada IdP
federado (`<poolId>_Google`) que NO es un rol: nunca se usa `groups[0]`. Sin grupo real el rol es PENDING.

A diferencia del workflow, aquí el token va SOLO en el header Authorization (nunca en la URL).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient, PyJWKClientConnectionError, PyJWKClientError

from .settings import Settings, get_settings

ROLES = ("SUPERADMIN", "ADMIN", "TEACHER")
PENDING_ROLE = "PENDING"
REQUIRED_ROLE = "SUPERADMIN"


def resolve_role(groups: Any) -> str:
    items = groups if isinstance(groups, list) else []
    return next((r for r in ROLES if r in items), PENDING_ROLE)


@lru_cache
def get_jwks_client() -> PyJWKClient:
    # PyJWKClient cachea las claves públicas; el issuer no cambia en runtime.
    return PyJWKClient(f"{get_settings().issuer}/.well-known/jwks.json")


def _decode(token: str, settings: Settings) -> dict:
    try:
        key = get_jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=settings.cognito_client_id,
            issuer=settings.issuer,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except PyJWKClientConnectionError as exc:
        # No se pudo ALCANZAR el JWKS: culpa del servidor, no del cliente. Va antes que
        # PyJWKClientError porque es su subclase.
        raise HTTPException(status_code=503, detail=f"No se pudo obtener el JWKS de Cognito: {exc}")
    except (jwt.InvalidTokenError, PyJWKClientError) as exc:
        raise HTTPException(status_code=401, detail=f"Token inválido: {exc}")


def require_superadmin(authorization: str | None = Header(None)) -> dict:
    settings = get_settings()
    if not settings.cognito_user_pool_id or not settings.cognito_client_id:
        raise HTTPException(status_code=500, detail="Cognito no está configurado en el monitor")

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Token de autorización requerido")
    token = authorization.split(" ", 1)[1].strip()

    payload = _decode(token, settings)
    if payload.get("token_use") != "id":
        raise HTTPException(status_code=401, detail="Se esperaba un ID token")

    role = resolve_role(payload.get("cognito:groups"))
    if role != REQUIRED_ROLE:
        raise HTTPException(status_code=403, detail="Acceso restringido a SUPERADMIN")

    payload["role"] = role
    return payload
