import json
import os

import pytest

from monitor.events import parse_event
from monitor.store import COLUMNS, EventStore, event_to_row
from tests.helpers import raw_event


def _row(event, topic="t", partition=2, offset=7):
    return dict(zip(COLUMNS, event_to_row(topic, partition, offset, parse_event(event))))


def test_fila_de_llm_usage_lleva_coordenadas_kafka_y_tokens():
    r = _row(raw_event("llm_usage", agent="Agente 1", model="m", input_tokens=10,
                       cached_tokens=4, output_tokens=3, thoughts_tokens=1))
    assert (r["kafka_topic"], r["kafka_partition"], r["kafka_offset"]) == ("t", 2, 7)
    assert (r["event_type"], r["session_id"], r["agent"], r["model"]) == ("llm_usage", "s1", "Agente 1", "m")
    assert (r["input_tokens"], r["cached_tokens"], r["output_tokens"], r["thoughts_tokens"]) == (10, 4, 3, 1)
    assert r["schema_version"] == 1 and r["event_ts"].year == 2026


def test_campos_que_no_aplican_quedan_en_none():
    r = _row(raw_event("flow_started"))
    assert r["agent"] is None and r["input_tokens"] is None and r["status"] is None


def test_flow_finished_guarda_estado_y_duracion():
    r = _row(raw_event("flow_finished", status="success", duration_ms=1234))
    assert r["status"] == "success" and r["duration_ms"] == 1234


def test_payload_es_el_evento_original_sin_campos_internos():
    ev = raw_event("agent_retry", agent="A", attempt=2, reason="503")
    payload = json.loads(_row(ev)["payload"])
    assert payload == ev and "_ts" not in payload


def test_valores_de_tipo_incorrecto_no_rompen_la_fila():
    r = _row(raw_event("llm_usage", input_tokens="mucho", duration_ms=True))
    assert r["input_tokens"] is None and r["duration_ms"] is None


# ── Integración: requiere Postgres (TEST_DATABASE_URL) ──────────────────────
pytestmark_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="sin TEST_DATABASE_URL")


@pytestmark_db
async def test_insercion_es_idempotente_y_la_vista_resume_el_flujo():
    store = EventStore(os.environ["TEST_DATABASE_URL"])
    await store.connect()
    try:
        await store.execute("DROP SCHEMA IF EXISTS monitor CASCADE")
        await store.ensure_schema()
        await store.ensure_schema()          # idempotente
        eventos = [
            raw_event("flow_started", sid="f1", ts="2026-09-29T14:00:00.000Z"),
            raw_event("llm_usage", sid="f1", ts="2026-09-29T14:00:01.000Z", agent="A", model="m",
                      input_tokens=100, cached_tokens=10, output_tokens=20, thoughts_tokens=5),
            raw_event("flow_finished", sid="f1", ts="2026-09-29T14:00:05.000Z", status="success", duration_ms=5000),
        ]
        rows = [event_to_row("t", 0, i, parse_event(e)) for i, e in enumerate(eventos)]
        await store.insert_batch(rows)
        await store.insert_batch(rows)       # repetir el lote NO duplica
        assert await store.fetchval("SELECT count(*) FROM monitor.agent_event_log") == 3
        resumen = await store.fetchrow("SELECT * FROM monitor.v_flow_summary WHERE session_id = 'f1'")
        assert resumen["status"] == "success" and resumen["input_tokens"] == 100
        assert resumen["output_tokens"] == 20 and resumen["duration_ms"] == 5000
    finally:
        await store.close()
