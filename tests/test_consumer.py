import json

import pytest

from monitor.aggregator import Aggregator
from monitor.consumer import LiveConsumer, TopicNotReady, backoff_seconds
from monitor.settings import Settings
from tests.fakes import FakeConsumer, Finished, Rec
from tests.helpers import raw_event

K = Settings(kafka_bootstrap="kafka:9092")


def rec(offset, event=None, partition=0, raw=None):
    return Rec(partition, offset, raw if raw is not None else json.dumps(event).encode())


def snap(agg):
    from datetime import datetime, timezone
    return agg.snapshot(datetime(2026, 9, 29, 14, 1, tzinfo=timezone.utc))


async def correr(lc):
    with pytest.raises(Finished):
        await lc._session()


async def test_aplica_los_eventos_y_termina_el_replay_al_alcanzar_el_final():
    agg = Aggregator()
    fake = FakeConsumer([[rec(0, raw_event("flow_started")),
                          rec(1, raw_event("llm_usage", agent="A", model="m", input_tokens=50, output_tokens=5))]])
    lc = LiveConsumer(K, agg, consumer_factory=lambda: fake)
    assert lc.replaying is True
    await correr(lc)
    s = snap(agg)
    assert lc.replaying is False and s["flows"]["running"] == 1 and s["tokens"]["input"] == 50
    assert fake.assigned and fake.assigned[0].topic == K.kafka_topic        # releyó desde el inicio


async def test_con_varias_particiones_lee_todas_y_alcanza_el_final_de_cada_una():
    agg = Aggregator()
    fake = FakeConsumer([[rec(0, raw_event("flow_started", sid="a"), partition=0),
                          rec(0, raw_event("flow_started", sid="b"), partition=1)]], partitions=(0, 1, 2))
    lc = LiveConsumer(K, agg, consumer_factory=lambda: fake)
    await correr(lc)
    assert [tp.partition for tp in fake.assigned] == [0, 1, 2]
    assert snap(agg)["flows"]["running"] == 2 and lc.replaying is False


async def test_mientras_no_alcanza_el_offset_final_sigue_reconstruyendo():
    fake = FakeConsumer([[rec(0, raw_event("flow_started"))]], end={0: 5})
    lc = LiveConsumer(K, Aggregator(), consumer_factory=lambda: fake)
    await correr(lc)
    assert lc.replaying is True


async def test_topic_vacio_no_queda_reconstruyendo():
    lc = LiveConsumer(K, Aggregator(), consumer_factory=lambda: FakeConsumer([], end={0: 0}))
    await correr(lc)
    assert lc.replaying is False


async def test_mensajes_invalidos_se_cuentan_y_no_tumban_el_consumer():
    agg = Aggregator()
    fake = FakeConsumer([[rec(0, raw=b"{roto"), rec(1, raw_event("flow_started")), rec(2, {"v": 7})]])
    await correr(LiveConsumer(K, agg, consumer_factory=lambda: fake))
    s = snap(agg)
    assert s["skipped_events"] == 2 and s["flows"]["running"] == 1


async def test_cada_sesion_empieza_de_cero_para_no_contar_doble_al_releer():
    agg = Aggregator()
    agg.apply(raw_event("flow_started", sid="viejo"))
    await correr(LiveConsumer(K, agg, consumer_factory=lambda: FakeConsumer([])))
    assert snap(agg)["flows"]["running"] == 0


async def test_topic_inexistente_pide_reintento():
    lc = LiveConsumer(K, Aggregator(), consumer_factory=lambda: FakeConsumer([], partitions=()))
    with pytest.raises(TopicNotReady):
        await lc._session()


async def test_connected_es_true_dentro_de_la_sesion_y_false_al_salir():
    estados = []
    fake = FakeConsumer([[rec(0, raw_event("flow_started"))]])
    lc = LiveConsumer(K, Aggregator(), consumer_factory=lambda: fake)
    original = fake.getmany

    async def espia(**kw):
        estados.append(lc.connected)
        return await original(**kw)

    fake.getmany = espia
    await correr(lc)
    assert estados[0] is True and lc.connected is False and fake.stopped is True


def test_backoff_crece_y_se_acota():
    assert [backoff_seconds(n) for n in (1, 2, 3, 4, 5, 6, 10)] == [1, 2, 4, 8, 16, 30, 30]
