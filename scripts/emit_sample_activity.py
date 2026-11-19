"""Emite eventos `http_request` SINTÉTICOS al topic de actividad para ver la pestaña «Uso» del monitor.

    docker compose run --rm monitor python scripts/emit_sample_activity.py --seconds 60 --rate 5
    docker compose run --rm monitor python scripts/emit_sample_activity.py --live      # sigue emitiendo

Solo para Kafka LOCAL de desarrollo. Los usuarios son hashes falsos (12 hex), sin relación con personas reales.
"""
import argparse
import asyncio
import hashlib
import json
import os
import random
import time
from datetime import datetime, timezone

from aiokafka import AIOKafkaProducer

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "kafka", "host.docker.internal"}
ROLES = ["TEACHER"] * 24 + ["ADMIN"] * 4 + ["SUPERADMIN"]  # ~30 usuarios, casi todos docentes
ROUTES = [
    ("GET", "/api/students", 200), ("GET", "/api/students/:id", 200), ("GET", "/api/students/paci-profiles/:id", 200),
    ("GET", "/api/auth/me", 200), ("GET", "/api/jobs/:id", 200), ("GET", "/api/dashboard/stats", 200),
    ("GET", "/api/admin/tickets", 200), ("POST", "/api/students", 201), ("GET", "/api/students/:id", 404),
    ("GET", "/api/auth/me", 401), ("GET", "/api/dashboard/stats", 500),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_users(n: int):
    users = []
    for i in range(n):
        h = hashlib.sha256(f"demo-user-{i}".encode()).hexdigest()[:12]
        users.append((h, ROLES[i % len(ROLES)]))
    return users


def make_event(users):
    method, route, status = random.choice(ROUTES)
    if random.random() < 0.05:  # algo de tráfico sin sesión
        user, role = "anon", "none"
    else:
        user, role = random.choice(users)
    return {"v": 1, "type": "http_request", "ts": now_iso(), "user": user, "role": role, "method": method,
            "route": route, "status": status, "latency_ms": int(random.lognormvariate(3.8, 0.7))}


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bootstrap", default=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"))
    p.add_argument("--topic", default=os.environ.get("KAFKA_ACTIVITY_TOPIC", "prisma.activity-events"))
    p.add_argument("--users", type=int, default=30)
    p.add_argument("--rate", type=float, default=5.0, help="eventos por segundo")
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--allow-remote", action="store_true", help="permitir un Kafka que no sea local (no recomendado)")
    p.add_argument("--live", action="store_true", help="emitir sin parar hasta Ctrl+C")
    args = p.parse_args()
    if args.rate <= 0:
        p.error("--rate debe ser mayor que 0")
    hosts = {b.rsplit(":", 1)[0].strip() for b in args.bootstrap.split(",")}
    if not args.allow_remote and not hosts <= LOCAL_HOSTS:
        p.error(f"{args.bootstrap} no parece un Kafka local; usa --allow-remote solo si de verdad quieres emitir ahí")

    users = make_users(args.users)
    producer = AIOKafkaProducer(bootstrap_servers=args.bootstrap)
    await producer.start()
    sent, t0 = 0, time.monotonic()
    try:
        while args.live or time.monotonic() - t0 < args.seconds:
            await producer.send_and_wait(args.topic, value=json.dumps(make_event(users)).encode())
            sent += 1
            await asyncio.sleep(1 / args.rate)
    finally:
        await producer.stop()
    print(f"listo: {sent} eventos sintéticos emitidos a {args.topic}")


if __name__ == "__main__":
    asyncio.run(main())
