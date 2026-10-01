"""prisma-monitor: API de métricas + front aislado.

    uvicorn monitor.main:create_app --factory --port 3020
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .aggregator import Aggregator
from .archiver import Archiver, ArchiveStatus
from .auth import require_superadmin
from .consumer import LiveConsumer
from .pricing import PriceTable
from .settings import Settings, get_settings
from .store import EventStore

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SPA_ROUTES = ("/", "/login", "/auth/callback")


def _csp(settings: Settings) -> str:
    cognito = f" https://{settings.cognito_domain}" if settings.cognito_domain else ""
    return (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        f"connect-src 'self'{cognito}; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )


def create_app(settings: Settings | None = None, aggregator: Aggregator | None = None,
               start_background: bool = True) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = settings or get_settings()
    aggregator = aggregator or Aggregator(
        PriceTable.from_json(settings.model_prices),
        stall_after=timedelta(minutes=settings.stall_minutes),
        retention=timedelta(hours=settings.retention_hours),
    )

    kafka_on = start_background and bool(settings.kafka_bootstrap)
    live = LiveConsumer(settings, aggregator) if kafka_on else None
    archive_on = kafka_on and bool(settings.database_url)
    archive_status = ArchiveStatus(enabled=archive_on)
    store = EventStore(settings.database_url) if archive_on else None
    archiver = Archiver(settings, store, archive_status) if archive_on else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tasks = [asyncio.create_task(x.run(), name=type(x).__name__) for x in (live, archiver) if x]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if store:
                await store.close()

    # Sin /docs ni /openapi.json: no exponer el esquema de una API operativa sin autenticar.
    app = FastAPI(title="prisma-monitor", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    csp = _csp(settings)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/health")
    def health():
        return {"status": "ok",
                "replaying": live.replaying if live else False,
                "kafka_connected": live.connected if live else False}

    @app.get("/api/monitor/metrics")
    def metrics(response: Response, _user: dict = Depends(require_superadmin)):
        response.headers["Cache-Control"] = "no-store"
        return aggregator.snapshot(
            datetime.now(timezone.utc),
            replaying=live.replaying if live else False,
            archive=archive_status.as_dict(),
        )

    # El front solo se sirve desde aquí en local. En producción (SERVE_FRONT=false) lo hospeda Amplify, este
    # servicio expone únicamente la API y no publica HTML ni configuración sin autenticar.
    # Nota: este servicio NUNCA emite CORS; con el front en otro origen, el dueño del CORS es el API Gateway
    # (si lo hicieran los dos, el navegador vería un Access-Control-Allow-Origin duplicado y rechazaría la respuesta).
    if settings.serve_front:
        @app.get("/config.json")
        def config():
            # Datos públicos y no secretos (Cognito los expone igual en el Hosted UI). `apiBase` vacío = el front
            # llama a su mismo origen; en Amplify el build genera este archivo con la URL del API Gateway.
            return {"cognitoDomain": settings.cognito_domain, "clientId": settings.cognito_client_id,
                    "region": settings.cognito_region, "apiBase": ""}

        def _index():
            return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

        for ruta in SPA_ROUTES:
            app.add_api_route(ruta, _index, methods=["GET"], include_in_schema=False)

        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
