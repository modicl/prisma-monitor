"""Dobles de prueba para Kafka y Postgres (sin red)."""
from dataclasses import dataclass


class Finished(Exception):
    """El guion del consumer falso se agotó: corta el bucle infinito del código bajo prueba."""


@dataclass
class Rec:
    partition: int
    offset: int
    value: bytes
    topic: str = "prisma.agent-events"


class TP(tuple):
    """Sustituto de TopicPartition: hashable, con .topic y .partition."""
    def __new__(cls, topic, partition):
        return super().__new__(cls, (topic, partition))

    topic = property(lambda self: self[0])
    partition = property(lambda self: self[1])


class FakeConsumer:
    def __init__(self, batches, *, end=None, partitions=(0,), calls=None):
        self.batches = list(batches)            # lista de listas de Rec
        self._end = end                         # {partition: offset final}; None => calculado
        self._partitions = set(partitions)
        self.calls = calls if calls is not None else []
        self._pos = {}
        self.assigned = []
        self.started = self.stopped = False

    async def start(self):
        self.started = True
        self.calls.append("start")

    async def stop(self):
        self.stopped = True
        self.calls.append("stop")

    def assignment(self):
        """Como aiokafka con group_id=None: todas las particiones del topic (vacío si aún no existe)."""
        return {TP("prisma.agent-events", p) for p in self._partitions}

    async def seek_to_beginning(self, *tps):
        self.assigned = list(tps)
        for tp in tps:
            self._pos[tp] = 0

    async def end_offsets(self, tps):
        if self._end is not None:
            return {tp: self._end.get(tp.partition, 0) for tp in tps}
        total = {}
        for batch in self.batches:
            for r in batch:
                total[r.partition] = max(total.get(r.partition, 0), r.offset + 1)
        return {tp: total.get(tp.partition, 0) for tp in tps}

    async def position(self, tp):
        return self._pos.get(tp, 0)

    async def getmany(self, timeout_ms=0, max_records=None):
        if not self.batches:
            raise Finished()
        batch = self.batches.pop(0)
        out = {}
        for r in batch:
            tp = TP(r.topic, r.partition)
            out.setdefault(tp, []).append(r)
            self._pos[tp] = max(self._pos.get(tp, 0), r.offset + 1)
        return out

    async def commit(self):
        self.calls.append("commit")


class FakeStore:
    def __init__(self, calls=None, fail=False):
        self.calls = calls if calls is not None else []
        self.fail = fail
        self.rows = []

    async def insert_batch(self, rows):
        if self.fail:
            raise RuntimeError("BD caída")
        self.calls.append(f"insert:{len(rows)}")
        self.rows.extend(rows)
