"""Emite flujos SINTÉTICOS al topic de Kafka para ver el monitor sin gastar cuota de Gemini.

    docker compose run --rm monitor python scripts/emit_sample_events.py --flows 8 --pace 0.4
    docker compose run --rm monitor python scripts/emit_sample_events.py --live      # sigue emitiendo

Los session_id llevan el prefijo `demo-` para poder distinguirlos (y filtrarlos) después en la tabla log:
    SELECT * FROM monitor.v_flow_summary WHERE session_id LIKE 'demo-%';
Ojo: la tabla log es append-only, estos eventos NO se borran de ella.
"""
import argparse
import asyncio
import json
import os
import random
import time
import uuid
from datetime import datetime, timezone

from aiokafka import AIOKafkaProducer

# Cada flujo sintético termina de una forma distinta para poblar todas las tarjetas del monitor.
OUTCOMES = ["success", "success", "degraded", "cancelled", "error", "running", "awaiting_hitl", "success"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Emitter:
    def __init__(self, producer, topic, model, pace):
        self.producer, self.topic, self.model, self.pace = producer, topic, model, pace

    async def send(self, sid, type_, **fields):
        event = {"v": 1, "type": type_, "session_id": sid, "ts": now_iso(), **fields}
        await self.producer.send_and_wait(self.topic, key=sid.encode(), value=json.dumps(event).encode())

    async def agent(self, sid, name, base_in, base_out):
        await self.send(sid, "agent_started", agent=name)
        await asyncio.sleep(self.pace)
        entrada = int(base_in * random.uniform(0.8, 1.2))
        await self.send(sid, "llm_usage", agent=name, model=self.model, input_tokens=entrada,
                        cached_tokens=int(entrada * random.uniform(0, 0.5)),
                        output_tokens=int(base_out * random.uniform(0.8, 1.2)),
                        thoughts_tokens=random.choice([0, 0, int(base_out * 0.3)]))
        await asyncio.sleep(self.pace)
        await self.send(sid, "agent_finished", agent=name, duration_ms=int(self.pace * 1000 * 2), ok=True)

    async def flow(self, outcome):
        sid = "demo-" + uuid.uuid4().hex[:12]
        t0 = time.monotonic()
        await self.send(sid, "flow_started")
        await self.agent(sid, "Agente 1", 9000, 900)
        await self.agent(sid, "Agente 2", 12000, 1500)
        await self.send(sid, "hitl_required", attempt=1)
        if outcome == "awaiting_hitl":
            return sid, outcome
        await asyncio.sleep(self.pace)
        if outcome == "cancelled":
            await self.send(sid, "flow_finished", status="cancelled", duration_ms=int((time.monotonic() - t0) * 1000))
            return sid, outcome
        await self.agent(sid, "Agente 3", 15000, 3000)
        if outcome == "running":
            await self.send(sid, "agent_started", agent="Agente Crítico")
            return sid, outcome
        await self.agent(sid, "Agente Crítico", 6000, 300)
        status = {"success": "success", "degraded": "degraded", "error": "error"}[outcome]
        await self.send(sid, "flow_finished", status=status, duration_ms=int((time.monotonic() - t0) * 1000))
        return sid, outcome


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bootstrap", default=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"))
    p.add_argument("--topic", default=os.environ.get("KAFKA_TOPIC", "prisma.agent-events"))
    p.add_argument("--model", default="gemini-3.1-flash-lite")
    p.add_argument("--flows", type=int, default=8)
    p.add_argument("--pace", type=float, default=0.4, help="segundos entre eventos")
    p.add_argument("--live", action="store_true", help="emitir flujos sin parar hasta Ctrl+C")
    args = p.parse_args()

    producer = AIOKafkaProducer(bootstrap_servers=args.bootstrap, enable_idempotence=True, acks="all")
    await producer.start()
    try:
        em = Emitter(producer, args.topic, args.model, args.pace)
        n = 0
        while args.live or n < args.flows:
            sid, outcome = await em.flow(OUTCOMES[n % len(OUTCOMES)])
            n += 1
            print(f"{sid}  {outcome}")
    finally:
        await producer.stop()
    print(f"listo: {n} flujos sintéticos emitidos a {args.topic}")


if __name__ == "__main__":
    asyncio.run(main())
