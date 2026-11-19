import json
from datetime import datetime, timedelta, timezone

from monitor.activity import ActivityAggregator, parse_activity

T0 = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)


class Agg(ActivityAggregator):
    """Reloj de prueba: salvo que se pase `now`, el reloj es el ts del propio evento."""

    def apply(self, raw, now=None):
        if now is None and isinstance(raw, dict):
            ev = parse_activity(raw)
            now = ev["_ts"] if ev else T0
        return super().apply(raw, now=now or T0)


def aev(sec=0, user="u1", role="TEACHER", method="GET", route="/api/x", status=200, lat=100, **over):
    e = {"v": 1, "type": "http_request",
         "ts": (T0 + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z"),
         "user": user, "role": role, "method": method, "route": route, "status": status, "latency_ms": lat}
    e.update(over)
    return e


def snap(a, sec=0):
    return a.snapshot(T0 + timedelta(seconds=sec))


def test_evento_valido_se_aplica_y_agrega_ts():
    a = Agg()
    assert a.apply(aev()) is True
    assert parse_activity(json.dumps(aev()))["_ts"] == T0


def test_eventos_invalidos_se_cuentan_y_no_rompen():
    a = Agg()
    sin_role = aev()
    del sin_role["role"]
    malos = [sin_role, aev(v=2), aev(status="x"), aev(lat=-1), "{roto", b"\xff", aev(role="HACKER"),
             aev(user=""), aev(ts="nope"), aev(type="otro"), aev(status=True)]
    for m in malos:
        assert a.apply(m) is False
    assert snap(a)["skipped_events"] == len(malos)
    assert snap(a)["requests_total"] == 0


def test_usuarios_activos_unicos_en_60s():
    a = Agg()
    for i, u in enumerate(["a", "b", "c", "a"]):
        a.apply(aev(sec=i, user=u))
    assert snap(a, 10)["active_users"] == 3


def test_eventos_viejos_no_cuentan_en_ventana():
    a = Agg()
    a.apply(aev(sec=0, user="viejo"))
    a.apply(aev(sec=61, user="nuevo"))
    s = snap(a, 122)  # viejo: 122s, nuevo: 61s -> ambos fuera
    assert s["active_users"] == 0 and s["requests_per_s"] == 0
    a2 = Agg()
    a2.apply(aev(sec=0, user="viejo"))
    a2.apply(aev(sec=40, user="nuevo"))
    for i in range(5):
        a2.apply(aev(sec=60 + i, user="nuevo"))
    s2 = snap(a2, 61)  # viejo 61s fuera; 6 de nuevo dentro
    assert s2["active_users"] == 1
    assert s2["requests_per_s"] == 6 / 60


def test_percentiles_interpolados():
    a = Agg()
    for i, l in enumerate([100, 200, 300, 400]):
        a.apply(aev(sec=i, lat=l))
    lat = snap(a, 5)["latency"]
    assert lat["p50_ms"] == 250
    assert 380 <= lat["p95_ms"] <= 400


def test_ventana_vacia_percentiles_none():
    s = snap(Agg())
    assert s["latency"] == {"p50_ms": None, "p95_ms": None}
    assert s["active_users"] == 0 and s["requests_per_s"] == 0 and s["recent"] == []


def test_top_routes_ordenado_y_truncado():
    a = Agg()
    for r in range(12):
        for _ in range(r + 1):
            a.apply(aev(route=f"/r{r}"))
    top = snap(a, 1)["top_routes"]
    assert len(top) == 10
    assert top[0] == {"route": "/r11", "count": 12}
    assert [t["count"] for t in top] == sorted((t["count"] for t in top), reverse=True)


def test_by_role():
    a = Agg()
    a.apply(aev(role="ADMIN"))
    a.apply(aev(role="TEACHER"))
    a.apply(aev(role="TEACHER"))
    assert snap(a, 1)["by_role"] == {"ADMIN": 1, "TEACHER": 2}


def test_recent_anillo_de_200_mas_nuevo_primero():
    a = Agg()
    for i in range(500):
        a.apply(aev(sec=0, route=f"/r{i}"))
    r = snap(a, 1)["recent"]
    assert len(r) == 200 and r[0]["route"] == "/r499"
    assert set(r[0]) == {"ts", "user", "role", "method", "route", "status", "latency_ms"}


def test_prune_descarta_mas_de_5_min_y_acota_memoria():
    a = Agg()
    a.apply(aev(sec=0))
    a.apply(aev(sec=1))
    a.apply(aev(sec=400))
    assert a.prune(T0 + timedelta(seconds=401)) == 0  # apply ya podó los de 0 y 1
    assert len(a._window) == 1
    assert a.prune(T0 + timedelta(seconds=800)) == 1
    assert len(a._window) == 0
    for i in range(1000):
        a.apply(aev(sec=1000 + i * 0.001))
    assert len(a._window) == 1000 and len(a._log) == 200
    a.prune(T0 + timedelta(seconds=2000))
    assert len(a._window) == 0


def test_evento_del_futuro_se_rechaza_y_no_rompe_la_poda():
    a = ActivityAggregator()
    assert a.apply(aev(sec=0, ts="2099-01-01T00:00:00Z"), now=T0) is False
    assert snap(a)["skipped_events"] == 1
    for i in range(1000):
        assert a.apply(aev(sec=i, user=f"u{i % 5}"), now=T0 + timedelta(seconds=i)) is True
    assert len(a._window) <= 301  # solo los ultimos 5 min
    s = snap(a, 999)
    assert s["active_users"] == 5 and s["requests_per_s"] == 61 / 60  # ventana inclusiva en ambos extremos


def test_tolera_skew_pequeno_y_tope_duro():
    a = ActivityAggregator(max_window=100)
    assert a.apply(aev(sec=30), now=T0) is True
    assert a.apply(aev(sec=61), now=T0) is False
    for _ in range(500):
        a.apply(aev(sec=0), now=T0)
    assert len(a._window) == 100
