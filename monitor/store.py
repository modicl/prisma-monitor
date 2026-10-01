"""Tabla log append-only en Postgres (`monitor.agent_event_log`).

- Cada fila guarda sus coordenadas Kafka (topic, partición, offset) con UNIQUE: reintentar o releer un lote
  nunca duplica filas (`ON CONFLICT DO NOTHING`).
- Solo se inserta; nunca se actualiza ni se borra. Al no tener retención es el historial de largo plazo
  (Kafka retiene 7 días).
- No guarda costo, solo tokens: se recalcula con cualquier tarifa al analizar.
- El esquema propio `monitor` la aísla de otras tablas de la misma base.
- El DDL es idempotente y lo aplica el propio servicio al arrancar (evita otro paso manual de migración).
"""
from __future__ import annotations

import json

import asyncpg

COLUMNS = (
    "kafka_topic", "kafka_partition", "kafka_offset", "event_ts", "schema_version", "event_type",
    "session_id", "agent", "model", "input_tokens", "cached_tokens", "output_tokens", "thoughts_tokens",
    "duration_ms", "status", "attempt", "payload",
)

DDL = """
CREATE SCHEMA IF NOT EXISTS monitor;

CREATE TABLE IF NOT EXISTS monitor.agent_event_log (
    id              bigserial PRIMARY KEY,
    kafka_topic     text        NOT NULL,
    kafka_partition integer     NOT NULL,
    kafka_offset    bigint      NOT NULL,
    event_ts        timestamptz NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    schema_version  smallint    NOT NULL,
    event_type      text        NOT NULL,
    session_id      text        NOT NULL,
    agent           text,
    model           text,
    input_tokens    integer,
    cached_tokens   integer,
    output_tokens   integer,
    thoughts_tokens integer,
    duration_ms     integer,
    status          text,
    attempt         integer,
    payload         jsonb       NOT NULL,
    UNIQUE (kafka_topic, kafka_partition, kafka_offset)
);

CREATE INDEX IF NOT EXISTS agent_event_log_session_idx ON monitor.agent_event_log (session_id, event_ts);
CREATE INDEX IF NOT EXISTS agent_event_log_type_idx    ON monitor.agent_event_log (event_type, event_ts);

CREATE OR REPLACE VIEW monitor.v_flow_summary AS
SELECT
    session_id,
    min(event_ts)    FILTER (WHERE event_type = 'flow_started')  AS started_at,
    max(event_ts)    FILTER (WHERE event_type = 'flow_finished') AS finished_at,
    max(status)      FILTER (WHERE event_type = 'flow_finished') AS status,
    max(duration_ms) FILTER (WHERE event_type = 'flow_finished') AS duration_ms,
    coalesce(sum(input_tokens)    FILTER (WHERE event_type = 'llm_usage'), 0) AS input_tokens,
    coalesce(sum(cached_tokens)   FILTER (WHERE event_type = 'llm_usage'), 0) AS cached_tokens,
    coalesce(sum(output_tokens)   FILTER (WHERE event_type = 'llm_usage'), 0) AS output_tokens,
    coalesce(sum(thoughts_tokens) FILTER (WHERE event_type = 'llm_usage'), 0) AS thoughts_tokens,
    count(*) FILTER (WHERE event_type = 'agent_retry')   AS retries,
    count(*) FILTER (WHERE event_type = 'hitl_required') AS hitl_rounds
FROM monitor.agent_event_log
GROUP BY session_id;
"""

_INSERT = (
    "INSERT INTO monitor.agent_event_log (" + ", ".join(COLUMNS) + ") VALUES ("
    + ", ".join(f"${i}" + ("::jsonb" if c == "payload" else "") for i, c in enumerate(COLUMNS, 1))
    + ") ON CONFLICT (kafka_topic, kafka_partition, kafka_offset) DO NOTHING"
)


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value):
    return value if isinstance(value, str) else None


def event_to_row(topic: str, partition: int, offset: int, event: dict) -> tuple:
    """Fila de la tabla log para un evento ya validado por `events.parse_event`."""
    original = {k: v for k, v in event.items() if k != "_ts"}
    return (
        topic, partition, offset, event["_ts"], _int(event.get("v")) or 1, event["type"],
        event["session_id"], _str(event.get("agent")), _str(event.get("model")),
        _int(event.get("input_tokens")), _int(event.get("cached_tokens")),
        _int(event.get("output_tokens")), _int(event.get("thoughts_tokens")),
        _int(event.get("duration_ms")), _str(event.get("status")), _int(event.get("attempt")),
        json.dumps(original, separators=(",", ":"), ensure_ascii=False),
    )


class EventStore:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=3, command_timeout=15)

    async def ensure_schema(self) -> None:
        await self.execute(DDL)

    async def insert_batch(self, rows: list[tuple]) -> None:
        async with self._pool.acquire() as conn:
            await conn.executemany(_INSERT, rows)

    async def execute(self, sql: str, *args):
        async with self._pool.acquire() as conn:
            return await conn.execute(sql, *args)

    async def fetchval(self, sql: str, *args):
        async with self._pool.acquire() as conn:
            return await conn.fetchval(sql, *args)

    async def fetchrow(self, sql: str, *args):
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
