from datetime import datetime, timedelta, timezone

import pytest

from monitor.aggregator import Aggregator
from monitor.pricing import PriceTable

T0 = datetime(2026, 9, 29, 14, 0, 0, tzinfo=timezone.utc)
PRICES = PriceTable.from_json('{"m": {"input": 1.0, "cached_input": 0.5, "output": 2.0}}')


def ev(type, sid="s1", sec=0, **campos):
    return {"v": 1, "type": type, "session_id": sid,
            "ts": (T0 + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z"), **campos}


def usage(sid="s1", sec=1, agent="Agente 1", model="m", i=1000, c=0, o=100, t=0):
    return ev("llm_usage", sid, sec, agent=agent, model=model,
              input_tokens=i, cached_tokens=c, output_tokens=o, thoughts_tokens=t)


def nuevo(**kw):
    return Aggregator(prices=kw.pop("prices", PRICES), **kw)


def snap(a, sec=10, **kw):
    return a.snapshot(T0 + timedelta(seconds=sec), **kw)


def test_un_flujo_iniciado_cuenta_como_corriendo():
    a = nuevo()
    assert a.apply(ev("flow_started")) is True
    f = snap(a)["flows"]
    assert (f["running"], f["awaiting_hitl"], f["stalled"], f["finished"]) == (1, 0, 0, 0)


def test_hitl_lo_mueve_a_esperando_docente_y_el_siguiente_agente_lo_devuelve():
    a = nuevo()
    a.apply(ev("flow_started"))
    a.apply(ev("hitl_required", sec=2, attempt=1))
    assert snap(a)["flows"]["awaiting_hitl"] == 1 and snap(a)["flows"]["running"] == 0
    a.apply(ev("agent_started", sec=3, agent="Agente 2"))
    assert snap(a)["flows"]["running"] == 1 and snap(a)["flows"]["awaiting_hitl"] == 0


def test_flujo_sin_eventos_por_mas_de_stall_pasa_a_detenido():
    a = nuevo(stall_after=timedelta(minutes=30))
    a.apply(ev("flow_started"))
    f = snap(a, sec=31 * 60)["flows"]
    assert (f["running"], f["stalled"]) == (0, 1)


def test_flujo_terminado_cuenta_por_estado_y_ya_no_esta_corriendo():
    a = nuevo()
    a.apply(ev("flow_started"))
    a.apply(ev("flow_finished", sec=60, status="success", duration_ms=60000))
    a.apply(ev("flow_started", sid="s2"))
    a.apply(ev("flow_finished", sid="s2", sec=30, status="cancelled", duration_ms=30000))
    f = snap(a)["flows"]
    assert f["finished"] == 2 and f["running"] == 0
    assert f["by_status"] == {"success": 1, "cancelled": 1}
    assert snap(a)["avg"]["duration_ms"] == 45000


def test_se_crea_el_flujo_con_el_primer_evento_aunque_falte_flow_started():
    a = nuevo()
    a.apply(usage())
    assert snap(a)["flows"]["running"] == 1


def test_tokens_suman_entrada_cache_salida_y_razonamiento():
    a = nuevo()
    a.apply(ev("flow_started"))
    a.apply(usage(i=1000, c=200, o=100, t=50))
    a.apply(usage(sec=2, i=500, c=0, o=25, t=0))
    t = snap(a)["tokens"]
    assert t == {"input": 1500, "cached": 200, "output": 125, "thoughts": 50, "total": 1675}


def test_costo_promedio_es_costo_de_flujos_terminados_sobre_su_cantidad():
    a = nuevo()
    for sid, mult in (("s1", 1), ("s2", 3)):
        a.apply(ev("flow_started", sid=sid))
        a.apply(usage(sid=sid, i=1_000_000 * mult, c=0, o=0))
        a.apply(ev("flow_finished", sid=sid, sec=5, status="success", duration_ms=5000))
    c = snap(a)["cost"]
    assert c["total"] == pytest.approx(4.0)
    assert c["avg_per_flow"] == pytest.approx(2.0)
    assert c["priced_flows"] == 2 and c["unpriced_flows"] == 0


def test_modelo_sin_tarifa_no_inventa_costo_y_se_reporta():
    a = nuevo()
    a.apply(ev("flow_started"))
    a.apply(usage(model="desconocido"))
    a.apply(ev("flow_finished", sec=5, status="success", duration_ms=5000))
    c = snap(a)["cost"]
    assert c["avg_per_flow"] is None and c["total"] is None
    assert c["unpriced_flows"] == 1 and c["unpriced_models"] == ["desconocido"]
    assert snap(a)["recent"][0]["cost"] is None


def test_cache_sin_tarifa_de_cache_se_marca_como_cota_superior():
    a = nuevo(prices=PriceTable.from_json('{"m": {"input": 1.0, "output": 2.0}}'))
    a.apply(ev("flow_started"))
    a.apply(usage(i=1000, c=400))
    assert snap(a)["cost"]["cached_at_input_rate"] == ["m"]


def test_desglose_por_agente_con_costo():
    a = nuevo()
    a.apply(ev("flow_started"))
    a.apply(usage(agent="Agente 1", i=1_000_000, o=0))
    a.apply(usage(sec=2, agent="Agente 2", i=0, o=1_000_000))
    por = {x["agent"]: x for x in snap(a)["by_agent"]}
    assert por["Agente 1"]["input"] == 1_000_000 and por["Agente 1"]["cost"] == pytest.approx(1.0)
    assert por["Agente 2"]["output"] == 1_000_000 and por["Agente 2"]["cost"] == pytest.approx(2.0)


def test_agente_actual_y_reintentos_se_ven_en_recent():
    a = nuevo()
    a.apply(ev("flow_started"))
    a.apply(ev("agent_started", sec=1, agent="Agente 2"))
    a.apply(ev("agent_retry", sec=2, agent="Agente 2", attempt=1, reason="timeout"))
    r = snap(a)["recent"][0]
    assert r["id"] == "s1" and r["agent"] == "Agente 2" and r["retries"] == 1 and r["state"] == "running"


def test_recent_esta_limitado_a_20_y_ordenado_por_ultimo_evento():
    a = nuevo()
    for i in range(25):
        a.apply(ev("flow_started", sid=f"{i:08d}-xxxxxxxx", sec=i))
    r = snap(a, sec=100)["recent"]
    assert len(r) == 20 and r[0]["id"] == "00000024" and len(r[0]["id"]) == 8


def test_eventos_invalidos_se_cuentan_y_no_rompen():
    a = nuevo()
    assert a.apply({"v": 9, "type": "flow_started"}) is False
    assert a.apply("basura") is False
    assert snap(a)["skipped_events"] == 2


def test_tokens_despues_de_terminar_se_suman_pero_no_reviven_el_flujo():
    a = nuevo()
    a.apply(ev("flow_started"))
    a.apply(ev("flow_finished", sec=5, status="cancelled", duration_ms=5000))
    a.apply(usage(sec=6, i=100, o=10))
    s = snap(a)
    assert s["flows"]["finished"] == 1 and s["flows"]["running"] == 0
    assert s["tokens"]["input"] == 100


def test_prune_descarta_flujos_mas_viejos_que_la_retencion():
    a = nuevo(retention=timedelta(hours=1))
    a.apply(ev("flow_started", sid="viejo"))
    a.apply(ev("flow_started", sid="nuevo", sec=3 * 3600))
    assert a.prune(T0 + timedelta(seconds=3 * 3600 + 5)) == 1
    assert snap(a, sec=3 * 3600 + 5)["flows"]["running"] == 1


def test_reset_deja_todo_en_cero():
    a = nuevo()
    a.apply(ev("flow_started"))
    a.apply({"v": 9})
    a.reset()
    s = snap(a)
    assert s["flows"]["running"] == 0 and s["skipped_events"] == 0


def test_snapshot_incluye_estado_de_replay_y_respaldo():
    s = snap(nuevo(), replaying=True, archive={"enabled": True})
    assert s["replaying"] is True and s["archive"] == {"enabled": True}
    assert "generated_at" in s
