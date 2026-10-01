import json

import pytest

from monitor.archiver import Archiver, ArchiveStatus
from monitor.settings import Settings
from tests.fakes import FakeConsumer, FakeStore, Finished, Rec
from tests.helpers import raw_event

K = Settings(kafka_bootstrap="kafka:9092", database_url="postgresql://x")


def rec(offset, event=None, raw=None):
    return Rec(0, offset, raw if raw is not None else json.dumps(event).encode())


def montar(batches, *, fail=False):
    calls = []
    status = ArchiveStatus(enabled=True)
    ar = Archiver(K, FakeStore(calls, fail=fail), status, consumer_factory=lambda: FakeConsumer(batches, calls=calls))
    return ar, status, calls


async def test_guarda_el_lote_y_solo_despues_confirma_el_offset():
    ar, status, calls = montar([[rec(0, raw_event("flow_started")), rec(1, raw_event("flow_finished", status="success"))]])
    with pytest.raises(Finished):
        await ar._session()
    assert calls == ["start", "insert:2", "commit", "stop"]
    assert status.ok is True and status.saved_total == 2 and status.last_saved_at is not None


async def test_mensaje_invalido_se_omite_pero_el_offset_avanza():
    ar, status, calls = montar([[rec(0, raw=b"{roto")]])
    with pytest.raises(Finished):
        await ar._session()
    assert calls == ["start", "commit", "stop"]        # no hay insert, pero no se queda pegado en el mensaje
    assert status.skipped == 1 and status.saved_total == 0


async def test_si_la_bd_falla_no_confirma_y_deja_el_error_visible():
    ar, status, calls = montar([[rec(0, raw_event("flow_started"))]], fail=True)
    with pytest.raises(RuntimeError, match="BD caída"):
        await ar._session()
    assert "commit" not in calls and calls[-1] == "stop"
    assert status.ok is False and "BD caída" in status.last_error


async def test_lote_vacio_no_inserta_ni_confirma():
    ar, _status, calls = montar([[]])
    with pytest.raises(Finished):
        await ar._session()
    assert calls == ["start", "stop"]


async def test_tras_un_error_y_recuperarse_el_estado_vuelve_a_ok():
    ar, status, _ = montar([[rec(0, raw_event("flow_started"))]])
    status.ok, status.last_error = False, "antes"
    with pytest.raises(Finished):
        await ar._session()
    assert status.ok is True and status.last_error is None


def test_status_como_dict_para_el_endpoint():
    d = ArchiveStatus(enabled=True, saved_total=3).as_dict()
    assert d["enabled"] is True and d["saved_total"] == 3 and d["last_saved_at"] is None
