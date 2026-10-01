import pytest

from monitor.pricing import PriceTable

PRICES = '{"gemini-x": {"input": 0.10, "cached_input": 0.025, "output": 0.40}}'


def test_cost_descuenta_cache_y_suma_razonamiento_a_la_salida():
    t = PriceTable.from_json(PRICES)
    # (1_000_000 - 400_000) * 0.10 + 400_000 * 0.025 + (200_000 + 100_000) * 0.40, todo / 1e6
    costo = t.cost("gemini-x", input=1_000_000, cached=400_000, output=200_000, thoughts=100_000)
    assert costo == pytest.approx(0.06 + 0.01 + 0.12)


def test_modelo_sin_tarifa_devuelve_none_no_cero():
    t = PriceTable.from_json(PRICES)
    assert t.cost("otro-modelo", input=10, cached=0, output=10, thoughts=0) is None


def test_sin_cached_input_la_cache_se_cobra_a_tarifa_de_entrada():
    t = PriceTable.from_json('{"m": {"input": 1.0, "output": 2.0}}')
    assert t.cost("m", input=1_000_000, cached=500_000, output=0, thoughts=0) == pytest.approx(1.0)
    assert t.cached_at_input_rate("m") is True


def test_cached_input_definido_no_marca_aproximacion():
    assert PriceTable.from_json(PRICES).cached_at_input_rate("gemini-x") is False


def test_cache_mayor_que_la_entrada_se_recorta():
    t = PriceTable.from_json(PRICES)
    # cached no puede exceder input: se trata como input completo en cache
    assert t.cost("gemini-x", input=1_000_000, cached=9_999_999, output=0, thoughts=0) == pytest.approx(0.025)


def test_coincidencia_por_prefijo_para_nombres_versionados():
    t = PriceTable.from_json(PRICES)
    assert t.has("gemini-x-preview-09-2026") is True
    assert t.cost("gemini-x-preview-09-2026", input=1_000_000, cached=0, output=0, thoughts=0) == pytest.approx(0.10)


def test_prefijo_mas_largo_gana():
    t = PriceTable.from_json('{"g": {"input": 1, "output": 1}, "g-lite": {"input": 5, "output": 5}}')
    assert t.cost("g-lite-1", input=1_000_000, cached=0, output=0, thoughts=0) == pytest.approx(5.0)


@pytest.mark.parametrize("raw", ["", "  ", "{}"])
def test_vacio_es_tabla_sin_tarifas(raw):
    t = PriceTable.from_json(raw)
    assert t.has("cualquiera") is False


@pytest.mark.parametrize("raw", ["no es json", "[]", '{"m": {"input": "x", "output": 1}}', '{"m": {"input": 1}}', '{"m": {"input": -1, "output": 1}}'])
def test_json_invalido_falla_con_mensaje_claro(raw):
    with pytest.raises(ValueError, match="MODEL_PRICES"):
        PriceTable.from_json(raw)
