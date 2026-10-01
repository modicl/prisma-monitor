"""Agregador en memoria: pliega los eventos de Kafka en las métricas del monitor.

Es una función pura del historial de eventos: releer el topic desde el inicio reconstruye exactamente el
mismo estado. No hace I/O ni usa el reloj por su cuenta (el `now` se lo pasan), así se prueba sin Kafka.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .events import parse_event
from .pricing import PriceTable

RECENT_LIMIT = 20
_TOKEN_KEYS = ("input", "cached", "output", "thoughts")


@dataclass
class Tokens:
    input: int = 0
    cached: int = 0
    output: int = 0
    thoughts: int = 0

    def add(self, other: "Tokens") -> None:
        self.input += other.input
        self.cached += other.cached
        self.output += other.output
        self.thoughts += other.thoughts

    @property
    def total(self) -> int:
        # Gemini: total = prompt (incluye caché) + candidatos + razonamiento
        return self.input + self.output + self.thoughts

    def as_dict(self) -> dict[str, int]:
        return {"input": self.input, "cached": self.cached, "output": self.output,
                "thoughts": self.thoughts, "total": self.total}


@dataclass
class Flow:
    session_id: str
    started_at: datetime
    last_event_at: datetime
    state: str = "running"                 # running | awaiting_hitl | finished
    agent: str | None = None
    retries: int = 0
    status: str | None = None
    duration_ms: int | None = None
    usage: dict[tuple[str, str], Tokens] = field(default_factory=dict)   # (agente, modelo) -> tokens

    def tokens(self) -> Tokens:
        total = Tokens()
        for t in self.usage.values():
            total.add(t)
        return total


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


class Aggregator:
    def __init__(self, prices: PriceTable | None = None,
                 stall_after: timedelta = timedelta(minutes=30),
                 retention: timedelta = timedelta(hours=168)):
        self.prices = prices or PriceTable()
        self.stall_after = stall_after
        self.retention = retention
        self._flows: dict[str, Flow] = {}
        self._skipped = 0

    # ── escritura ───────────────────────────────────────────────────────────
    def reset(self) -> None:
        self._flows.clear()
        self._skipped = 0

    def apply(self, raw: Any) -> bool:
        event = parse_event(raw)
        if event is None:
            self._skipped += 1
            return False

        ts, sid, kind = event["_ts"], event["session_id"], event["type"]
        flow = self._flows.get(sid)
        if flow is None:
            flow = self._flows[sid] = Flow(session_id=sid, started_at=ts, last_event_at=ts)
        flow.last_event_at = max(flow.last_event_at, ts)
        flow.started_at = min(flow.started_at, ts)

        if kind == "llm_usage":
            modelo = event.get("model") or "desconocido"
            agente = event.get("agent") or "?"
            t = flow.usage.setdefault((agente, modelo), Tokens())
            t.add(Tokens(_int(event.get("input_tokens")), _int(event.get("cached_tokens")),
                         _int(event.get("output_tokens")), _int(event.get("thoughts_tokens"))))
            return True

        if flow.state == "finished":
            return True            # un evento tardío no revive un flujo cerrado

        if kind == "agent_started":
            flow.state, flow.agent = "running", event.get("agent") or flow.agent
        elif kind == "agent_retry":
            flow.retries += 1
        elif kind == "hitl_required":
            flow.state = "awaiting_hitl"
        elif kind == "flow_finished":
            flow.state = "finished"
            flow.status = event.get("status") or "error"
            flow.duration_ms = _int(event.get("duration_ms")) or None
        return True

    def prune(self, now: datetime) -> int:
        limite = now - self.retention
        viejos = [sid for sid, f in self._flows.items() if f.last_event_at < limite]
        for sid in viejos:
            del self._flows[sid]
        return len(viejos)

    # ── lectura ─────────────────────────────────────────────────────────────
    def _flow_state(self, f: Flow, now: datetime) -> str:
        if f.state == "finished":
            return "finished"
        return "stalled" if now - f.last_event_at > self.stall_after else f.state

    def _flow_cost(self, f: Flow) -> float | None:
        """Costo del flujo o None si algún modelo usado no tiene tarifa."""
        total = 0.0
        for (_agente, modelo), t in f.usage.items():
            c = self.prices.cost(modelo, input=t.input, cached=t.cached, output=t.output, thoughts=t.thoughts)
            if c is None:
                return None
            total += c
        return total

    def snapshot(self, now: datetime, *, replaying: bool = False, archive: dict | None = None) -> dict:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        contadores = {"running": 0, "awaiting_hitl": 0, "stalled": 0, "finished": 0}
        by_status: dict[str, int] = {}
        tokens = Tokens()
        por_agente: dict[str, Tokens] = {}
        costo_agente: dict[str, float | None] = {}
        costos_terminados: list[float] = []
        costo_total, priced, unpriced = 0.0, 0, 0
        sin_tarifa: set[str] = set()
        cache_aprox: set[str] = set()
        duraciones: list[int] = []
        tokens_terminados: list[int] = []

        for f in self._flows.values():
            estado = self._flow_state(f, now)
            contadores[estado] += 1
            if estado == "finished":
                by_status[f.status or "error"] = by_status.get(f.status or "error", 0) + 1
                if f.duration_ms:
                    duraciones.append(f.duration_ms)
                tokens_terminados.append(f.tokens().total)

            ft = f.tokens()
            tokens.add(ft)
            for (agente, modelo), t in f.usage.items():
                por_agente.setdefault(agente, Tokens()).add(t)
                c = self.prices.cost(modelo, input=t.input, cached=t.cached, output=t.output, thoughts=t.thoughts)
                previo = costo_agente.get(agente, 0.0)
                costo_agente[agente] = None if (c is None or previo is None) else previo + c
                if c is None:
                    sin_tarifa.add(modelo)
                elif t.cached and self.prices.cached_at_input_rate(modelo):
                    cache_aprox.add(modelo)

            if ft.total > 0:
                c = self._flow_cost(f)
                if c is None:
                    unpriced += 1
                else:
                    priced += 1
                    costo_total += c
                    if estado == "finished":
                        costos_terminados.append(c)

        recientes = sorted(self._flows.values(), key=lambda f: f.last_event_at, reverse=True)[:RECENT_LIMIT]
        return {
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "replaying": replaying,
            "flows": {**contadores, "by_status": dict(sorted(by_status.items()))},
            "tokens": tokens.as_dict(),
            "cost": {
                "currency": "USD",
                "total": costo_total if priced else None,
                "avg_per_flow": (sum(costos_terminados) / len(costos_terminados)) if costos_terminados else None,
                "priced_flows": priced,
                "unpriced_flows": unpriced,
                "unpriced_models": sorted(sin_tarifa),
                "cached_at_input_rate": sorted(cache_aprox),
            },
            "avg": {
                "duration_ms": round(sum(duraciones) / len(duraciones)) if duraciones else None,
                "tokens_per_flow": round(sum(tokens_terminados) / len(tokens_terminados)) if tokens_terminados else None,
            },
            "by_agent": sorted(
                ({"agent": a, **t.as_dict(), "cost": costo_agente.get(a)} for a, t in por_agente.items()),
                key=lambda x: x["agent"],
            ),
            "recent": [self._recent_row(f, now) for f in recientes],
            "archive": archive,
            "skipped_events": self._skipped,
        }

    def _recent_row(self, f: Flow, now: datetime) -> dict:
        estado = self._flow_state(f, now)
        fin = f.duration_ms if f.duration_ms else int((f.last_event_at - f.started_at).total_seconds() * 1000)
        return {
            "id": f.session_id[:8],
            "state": estado,
            "agent": f.agent,
            "status": f.status,
            "retries": f.retries,
            "tokens": f.tokens().total,
            "cost": self._flow_cost(f) if f.usage else None,
            "duration_ms": fin,
            "started_at": f.started_at.isoformat().replace("+00:00", "Z"),
            "updated_at": f.last_event_at.isoformat().replace("+00:00", "Z"),
        }
