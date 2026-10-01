"""Contrato de eventos v1 (topic `prisma.agent-events`).

Es el único lugar que decide si un mensaje es válido; lo usan el agregador, el consumer y el archivador.
Sin PII por diseño: el contrato solo transporta ids aleatorios, contadores y nombres de agente.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1

KNOWN_TYPES = frozenset({
    "flow_started", "flow_finished",
    "agent_started", "agent_finished", "agent_retry",
    "llm_usage", "hitl_required",
})


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def parse_event(raw: bytes | str | dict) -> dict | None:
    """Devuelve el evento validado (con `_ts` datetime UTC) o None si no cumple el contrato v1."""
    if isinstance(raw, (bytes, bytearray, str)):
        try:
            raw = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
    if not isinstance(raw, dict):
        return None
    if raw.get("v") != SCHEMA_VERSION or raw.get("type") not in KNOWN_TYPES:
        return None
    session_id = raw.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    ts = parse_ts(raw.get("ts"))
    if ts is None:
        return None
    return {**raw, "_ts": ts}
