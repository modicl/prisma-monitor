import asyncio
import json
from datetime import datetime, timezone

import pytest

from monitor import activity_consumer as mod
from monitor.activity import ActivityAggregator
from monitor.activity_consumer import ActivityConsumer
from monitor.consumer import TopicNotReady
from monitor.settings import Settings
from tests.fakes import FakeConsumer, Finished, Rec

K = Settings(kafka_bootstrap="kafka:9092")


def ev(**over):
    base = {"v": 1, "type": "http_request",
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "user": "abc123def456", "role": "TEACHER", "method": "GET", "route": "/api/x",
            "status": 200, "latency_ms": 12}
    return {**base, **over}


def rec(offset, event=None, raw=None):
    return Rec(0, offset, raw if raw is not None else json.dumps(event).encode(), topic=K.activity_topic)


def snap(agg):
    return agg.snapshot(datetime.now(timezone.utc))


async def correr(ac):
    with pytest.raises(Finished):
        await ac._session()


def test_settings_topic_por_defecto_y_override():
    assert Settings().activity_topic == "prisma.activity-events"
    assert Settings.from_env({}).activity_topic == "prisma.activity-events"
    assert Settings.from_env({"KAFKA_ACTIVITY_TOPIC": "otro"}).activity_topic == "otro"


async def test_mensajes_validos_llegan_al_agregador_y_connected():
    agg = ActivityAggregator()
    fake = FakeConsumer([[rec(0, ev()), rec(1, ev(user="u2"))]])
    ac = ActivityConsumer(K, agg, consumer_factory=lambda: fake)
    await correr(ac)
    s = snap(agg)
    assert s["requests_total"] == 2 and s["active_users"] == 2
    assert ac.connected is False and fake.stopped is True


async def test_no_hace_replay_del_historial():
    fake = FakeConsumer([[rec(0, ev())]])
    visto = []

    async def seek(*tps):
        visto.append(tps)

    fake.seek_to_beginning = seek
    await correr(ActivityConsumer(K, ActivityAggregator(), consumer_factory=lambda: fake))
    assert visto == []


async def test_mensaje_basura_se_cuenta_y_el_bucle_sigue():
    agg = ActivityAggregator()
    fake = FakeConsumer([[rec(0, raw=b"{roto"), rec(1, ev()), rec(2, {"v": 9})]])
    await correr(ActivityConsumer(K, agg, consumer_factory=lambda: fake))
    s = snap(agg)
    assert s["skipped_events"] == 2 and s["requests_total"] == 1


async def test_topic_inexistente_lanza_topicnotready():
    ac = ActivityConsumer(K, ActivityAggregator(), consumer_factory=lambda: FakeConsumer([], partitions=()))
    with pytest.raises(TopicNotReady):
        await ac._session()


async def test_run_reintenta_con_backoff_y_no_termina(monkeypatch):
    esperas = []

    async def sleep(s):
        esperas.append(s)
        if len(esperas) == 3:
            raise Finished()

    monkeypatch.setattr(mod.asyncio, "sleep", sleep)
    ac = ActivityConsumer(K, ActivityAggregator(), consumer_factory=lambda: FakeConsumer([], partitions=()))
    with pytest.raises(Finished):
        await ac.run()
    assert esperas == [1, 2, 4]


async def test_poda_periodica_con_reloj_real(monkeypatch):
    agg = ActivityAggregator()
    llamadas = []
    agg.prune = lambda now: llamadas.append(now) or 0
    reloj = iter([0, 100, 100, 100, 100])
    from types import SimpleNamespace
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: next(reloj)))
    fake = FakeConsumer([[rec(0, ev())], [rec(1, ev())]])
    await correr(ActivityConsumer(K, agg, consumer_factory=lambda: fake))
    assert llamadas and llamadas[0].tzinfo is not None
