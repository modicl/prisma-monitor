"""Consumer de actividad: lee el topic de requests del BFF desde el FINAL y alimenta al agregador.

A diferencia de `LiveConsumer` no relee el historial: la ventana es de minutos, asi que al arrancar solo
interesa lo que llegue desde ahora. Sin consumer group ni commit de offsets.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable

from aiokafka import AIOKafkaConsumer

from .activity import ActivityAggregator
from .consumer import TopicNotReady, backoff_seconds
from .settings import Settings

log = logging.getLogger("monitor.activity")

_PRUNE_EVERY_S = 60


class ActivityConsumer:
    def __init__(self, settings: Settings, aggregator: ActivityAggregator,
                 consumer_factory: Callable[[], AIOKafkaConsumer] | None = None):
        self.settings = settings
        self.aggregator = aggregator
        self._factory = consumer_factory or self._default_factory
        self.connected = False

    def _default_factory(self) -> AIOKafkaConsumer:
        return AIOKafkaConsumer(
            self.settings.activity_topic,
            bootstrap_servers=self.settings.kafka_bootstrap,
            group_id=None,
            enable_auto_commit=False,
            auto_offset_reset="latest",
        )

    async def _session(self) -> None:
        """Una conexion completa. Solo sale por excepcion."""
        consumer = self._factory()
        await consumer.start()
        try:
            if not consumer.assignment():                  # el topic aun no existe
                raise TopicNotReady(self.settings.activity_topic)
            self.connected = True
            last_prune = time.monotonic()
            while True:
                batch = await consumer.getmany(timeout_ms=1000, max_records=500)
                for messages in batch.values():
                    for message in messages:
                        self.aggregator.apply(message.value)
                if time.monotonic() - last_prune > _PRUNE_EVERY_S:
                    self.aggregator.prune(datetime.now(timezone.utc))
                    last_prune = time.monotonic()
        finally:
            self.connected = False
            await consumer.stop()

    async def run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                espera = backoff_seconds(attempt)
                log.warning("consumer de actividad: %s: %s — reintento en %ss", type(exc).__name__, exc, espera)
                await asyncio.sleep(espera)
            else:
                attempt = 0
