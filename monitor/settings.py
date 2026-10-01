"""Configuración por variables de entorno. Ningún valor por defecto es un secreto."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    kafka_bootstrap: str = ""              # vacío => el monitor arranca sin consumir (útil en tests)
    kafka_topic: str = "prisma.agent-events"
    archiver_group: str = "prisma-monitor-archiver"
    database_url: str = ""                 # vacío => sin respaldo en tabla log
    cognito_region: str = "us-east-1"
    cognito_user_pool_id: str = ""
    cognito_client_id: str = ""
    cognito_domain: str = ""               # SIN https://
    model_prices: str = ""                 # JSON, ver pricing.py
    stall_minutes: int = 30
    retention_hours: int = 168
    port: int = 3020
    # True: este servicio también sirve el front (desarrollo local, compose).
    # False: solo API. En producción el front vive en Amplify y el CORS lo hace el API Gateway.
    serve_front: bool = True

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.cognito_region}.amazonaws.com/{self.cognito_user_pool_id}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env

        def s(name: str, default: str = "") -> str:
            return (e.get(name) or default).strip()

        def i(name: str, default: int) -> int:
            raw = s(name)
            try:
                return int(raw) if raw else default
            except ValueError as exc:
                raise ValueError(f"{name} debe ser un entero, llegó {raw!r}") from exc

        def b(name: str, default: bool) -> bool:
            raw = s(name).lower()
            if not raw:
                return default
            if raw in ("1", "true", "yes", "on"):
                return True
            if raw in ("0", "false", "no", "off"):
                return False
            raise ValueError(f"{name} debe ser true o false, llegó {raw!r}")

        return cls(
            kafka_bootstrap=s("KAFKA_BOOTSTRAP_SERVERS"),
            kafka_topic=s("KAFKA_TOPIC", "prisma.agent-events"),
            archiver_group=s("ARCHIVER_GROUP", "prisma-monitor-archiver"),
            database_url=s("MONITOR_DATABASE_URL"),
            cognito_region=s("COGNITO_REGION", "us-east-1"),
            cognito_user_pool_id=s("COGNITO_USER_POOL_ID"),
            cognito_client_id=s("COGNITO_CLIENT_ID"),
            cognito_domain=s("COGNITO_DOMAIN"),
            model_prices=s("MODEL_PRICES"),
            stall_minutes=i("STALL_MINUTES", 30),
            retention_hours=i("RETENTION_HOURS", 168),
            port=i("PORT", 3020),
            serve_front=b("SERVE_FRONT", True),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
