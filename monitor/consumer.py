"""Consumer en vivo: relee el topic desde el inicio y alimenta al agregador.

No usa consumer group ni confirma offsets: cada arranque (y cada reconexión) reconstruye el estado completo
con los eventos retenidos en Kafka, de forma determinista. Esa es la razón por la que las métricas no
necesitan base de datos. Mientras no alcanza los offsets finales iniciales se reporta `replaying`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable

from aiokafka import AIOKafkaConsumer

from .aggregator import Aggregator
from .settings import Settings

log = logging.getLogger("monitor.consumer")

_PRUNE_EVERY_S = 60


class TopicNotReady(Exception):
    """El topic todavía no existe (Kafka recién levantado o kafka-init sin terminar)."""


def backoff_seconds(attempt: int) -> int:
    return min(30, 2 ** max(0, attempt - 1))


class LiveConsumer:
    def __init__(self, settings: Settings, aggregator: Aggregator,
                 consumer_factory: Callable[[], AIOKafkaConsumer] | None = None):
        self.settings = settings
        self.aggregator = aggregator
        self._factory = consumer_factory or self._default_factory
        self.replaying = True
        self.connected = False

    def _default_factory(self) -> AIOKafkaConsumer:
        # Suscripción al topic con group_id=None: aiokafka usa un coordinador SIN grupo que asigna todas las
        # particiones. (Con assign() manual el consumer nunca obtiene los metadatos del topic:
        # partitions_for_topic() devuelve None.)
        return AIOKafkaConsumer(
            self.settings.kafka_topic,
            bootstrap_servers=self.settings.kafka_bootstrap,
            group_id=None,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )

    async def _session(self) -> None:
        """Una conexión completa: reset, replay desde el inicio y lectura en vivo. Solo sale por excepción."""
        self.aggregator.reset()
        self.replaying = True
        consumer = self._factory()
        await consumer.start()
        try:
            tps = sorted(consumer.assignment(), key=lambda tp: tp.partition)
            if not tps:                                    # el topic aún no existe
                raise TopicNotReady(self.settings.kafka_topic)
            await consumer.seek_to_beginning(*tps)         # explícito: cada sesión relee todo el historial
            end = await consumer.end_offsets(tps)
            self.replaying = any(end[tp] > 0 for tp in tps)
            self.connected = True
            last_prune = time.monotonic()

            while True:
                batch = await consumer.getmany(timeout_ms=1000, max_records=500)
                for messages in batch.values():
                    for message in messages:
                        self.aggregator.apply(message.value)
                if self.replaying:
                    self.replaying = not await self._caught_up(consumer, tps, end)
                if time.monotonic() - last_prune > _PRUNE_EVERY_S:
                    self.aggregator.prune(datetime.now(timezone.utc))
                    last_prune = time.monotonic()
        finally:
            self.connected = False
            await consumer.stop()

    @staticmethod
    async def _caught_up(consumer, tps, end) -> bool:
        for tp in tps:
            if await consumer.position(tp) < end[tp]:
                return False
        return True

    async def run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:                       # Kafka caído, topic ausente, red, etc.
                attempt += 1
                espera = backoff_seconds(attempt)
                log.warning("consumer en vivo: %s: %s — reintento en %ss", type(exc).__name__, exc, espera)
                await asyncio.sleep(espera)
            else:
                attempt = 0
