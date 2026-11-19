"""Contrato de actividad v1 (topic `prisma.activity-events`) y agregador en memoria.

El evento describe un request HTTP al BFF: `user` es un hash truncado (nunca sub ni email) y `role`
es solo informativo (el BFF no verifica el token). Una sola función decide si un mensaje es válido.
"""
from __future__ import annotations

import json
import math
from collections import Counter, deque
from datetime import datetime, timedelta, timezone

from .events import parse_ts

ACTIVITY_SCHEMA_VERSION = 1
ROLES = ("SUPERADMIN", "ADMIN", "TEACHER", "none")
TOP_ROUTES = 10
MAX_FUTURE_SKEW_S = 60   # un evento mas adelantado que esto respecto del reloj real se rechaza
WINDOW_HARD_CAP = 50_000  # tope duro de memoria, por si el consumer va mas rapido que la poda


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def parse_activity(raw: bytes | str | dict) -> dict | None:
    """Devuelve el evento validado (con `_ts` datetime UTC) o None si no cumple el contrato v1."""
    if isinstance(raw, (bytes, bytearray, str)):
        try:
            raw = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
    if not isinstance(raw, dict):
        return None
    if raw.get("v") != ACTIVITY_SCHEMA_VERSION or raw.get("type") != "http_request":
        return None
    ts = parse_ts(raw.get("ts"))
    if ts is None:
        return None
    user = raw.get("user")
    if not isinstance(user, str) or not user:
        return None
    if raw.get("role") not in ROLES:
        return None
    if not isinstance(raw.get("method"), str) or not isinstance(raw.get("route"), str):
        return None
    status = raw.get("status")
    if not isinstance(status, int) or isinstance(status, bool):
        return None
    latency = raw.get("latency_ms")
    if not _is_num(latency) or latency < 0:
        return None
    return {**raw, "_ts": ts.astimezone(timezone.utc)}


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    """Percentil con interpolación lineal (q en 0..1) sobre una lista ordenada."""
    if not sorted_vals:
        return None
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


class ActivityAggregator:
    """Estado puro en memoria: ventana larga acotada por tiempo y anillo de eventos recientes."""

    def __init__(self, window_s: int = 60, latency_window_s: int = 300, log_size: int = 200,
                 max_window: int = WINDOW_HARD_CAP):
        self.window_s = window_s
        self.latency_window_s = latency_window_s
        self._window: deque[dict] = deque(maxlen=max_window)
        self._log: deque[dict] = deque(maxlen=log_size)
        self._total = 0
        self._skipped = 0

    def apply(self, raw, now: datetime | None = None) -> bool:
        """Aplica un evento. `now` (tz-aware, por defecto el reloj real) es la referencia de poda y de
        rechazo de eventos del futuro; nunca se usa el `ts` del evento como referencia."""
        now = now or datetime.now(timezone.utc)
        ev = parse_activity(raw)
        if ev is None or ev["_ts"] > now + timedelta(seconds=MAX_FUTURE_SKEW_S):
            self._skipped += 1
            return False
        self._window.append(ev)
        self._log.append(ev)
        self._total += 1
        self.prune(now)
        return True

    def prune(self, now: datetime) -> int:
        """Descarta de la ventana larga lo anterior a `latency_window_s` respecto de `now` (tz-aware)."""
        cutoff = now - timedelta(seconds=self.latency_window_s)
        removed = 0
        while self._window and self._window[0]["_ts"] < cutoff:
            self._window.popleft()
            removed += 1
        return removed

    def snapshot(self, now: datetime) -> dict:
        """`now` debe ser tz-aware. Ventanas: requests_total es acumulado desde el arranque;
        active_users y requests_per_s usan 60 s; by_role, latency y top_routes usan 5 min."""
        short_cut = now - timedelta(seconds=self.window_s)
        long_cut = now - timedelta(seconds=self.latency_window_s)
        long_evs = [e for e in list(self._window) if e["_ts"] >= long_cut]
        short_evs = [e for e in long_evs if e["_ts"] >= short_cut]
        lats = sorted(e["latency_ms"] for e in long_evs)
        routes = Counter(e["route"] for e in long_evs)
        top = sorted(routes.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_ROUTES]
        by_role = Counter(e["role"] for e in long_evs)
        recent = [
            {"ts": e["ts"], "user": e["user"], "role": e["role"], "method": e["method"],
             "route": e["route"], "status": e["status"], "latency_ms": e["latency_ms"]}
            for e in reversed(list(self._log))
        ]
        return {
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "window_s": self.window_s,
            "active_users": len({e["user"] for e in short_evs}),
            "requests_per_s": len(short_evs) / self.window_s,
            "requests_total": self._total,
            "latency": {"p50_ms": _percentile(lats, 0.5), "p95_ms": _percentile(lats, 0.95)},
            "top_routes": [{"route": r, "count": c} for r, c in top],
            "by_role": dict(by_role),
            "recent": recent,
            "skipped_events": self._skipped,
        }
