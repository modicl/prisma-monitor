"""Archivador: respalda cada evento en la tabla log de Postgres.

Es un consumer DISTINTO al del agregador: usa un consumer group propio (`prisma-monitor-archiver`) y guarda
su avance en Kafka. Confirma el offset SOLO después de que el lote quedó guardado; si la BD falla, cierra el
consumer, espera y reabre, retomando desde el último commit. No se pierde ningún evento mientras la BD vuelva
antes de que expire la retención de Kafka. Repetir un lote es inofensivo (UNIQUE de coordenadas Kafka).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from aiokafka import AIOKafkaConsumer

from .consumer import backoff_seconds
from .events import parse_event
from .settings import Settings
from .store import EventStore, event_to_row

log = logging.getLogger("monitor.archiver")


@dataclass
class ArchiveStatus:
    enabled: bool = False
    ok: bool = True
    saved_total: int = 0
    skipped: int = 0
    last_saved_at: str | None = None
    last_error: str | None = None

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "ok": self.ok, "saved_total": self.saved_total,
                "skipped": self.skipped, "last_saved_at": self.last_saved_at, "last_error": self.last_error}


class Archiver:
    def __init__(self, settings: Settings, store: EventStore, status: ArchiveStatus,
                 consumer_factory: Callable[[], AIOKafkaConsumer] | None = None):
        self.settings = settings
        self.store = store
        self.status = status
        self._factory = consumer_factory or self._default_factory

    def _default_factory(self) -> AIOKafkaConsumer:
        return AIOKafkaConsumer(
            self.settings.kafka_topic,
            bootstrap_servers=self.settings.kafka_bootstrap,
            group_id=self.settings.archiver_group,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )

    async def _session(self) -> None:
        consumer = self._factory()
        await consumer.start()
        try:
            while True:
                batch = await consumer.getmany(timeout_ms=1000, max_records=200)
                rows, seen = [], 0
                for messages in batch.values():
                    for m in messages:
                        seen += 1
                        event = parse_event(m.value)
                        if event is None:
                            self.status.skipped += 1       # mensaje que no cumple el contrato: no bloquea el avance
                            continue
                        rows.append(event_to_row(m.topic, m.partition, m.offset, event))
                if seen == 0:
                    continue
                try:
                    if rows:
                        await self.store.insert_batch(rows)
                except Exception as exc:
                    self.status.ok = False
                    self.status.last_error = f"{type(exc).__name__}: {exc}"
                    raise
                await consumer.commit()                     # solo después de guardar
                if rows:
                    self.status.saved_total += len(rows)
                    self.status.last_saved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                self.status.ok = True
                self.status.last_error = None
        finally:
            await consumer.stop()

    async def run(self) -> None:
        attempt = 0
        while True:
            try:
                await self.store.connect()
                await self.store.ensure_schema()
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                self.status.ok = False
                self.status.last_error = f"{type(exc).__name__}: {exc}"
                espera = backoff_seconds(attempt)
                log.warning("archivador: %s — reintento en %ss", self.status.last_error, espera)
                await asyncio.sleep(espera)
            else:
                attempt = 0
