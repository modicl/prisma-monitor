import json

from monitor.events import parse_event, KNOWN_TYPES


def _ev(**over):
    base = {"v": 1, "type": "flow_started", "session_id": "abc", "ts": "2026-09-29T14:00:00.000Z"}
    base.update(over)
    return base


def test_evento_valido_como_dict_y_como_bytes():
    assert parse_event(_ev())["type"] == "flow_started"
    assert parse_event(json.dumps(_ev()).encode())["session_id"] == "abc"


def test_el_contrato_v1_conoce_los_siete_tipos():
    assert KNOWN_TYPES == {
        "flow_started", "flow_finished", "agent_started", "agent_finished",
        "agent_retry", "llm_usage", "hitl_required",
    }


def test_rechaza_json_roto_version_tipo_o_campos_faltantes():
    assert parse_event(b"{no json") is None
    assert parse_event(b"[]") is None
    assert parse_event(_ev(v=2)) is None
    assert parse_event(_ev(type="otro")) is None
    assert parse_event(_ev(session_id="")) is None
    assert parse_event(_ev(session_id=None)) is None
    assert parse_event(_ev(ts="ayer")) is None
    ev = _ev()
    del ev["ts"]
    assert parse_event(ev) is None


def test_ts_se_normaliza_a_datetime_utc():
    ev = parse_event(_ev(ts="2026-09-29T14:00:00.500Z"))
    assert ev["_ts"].tzinfo is not None and ev["_ts"].microsecond == 500000
