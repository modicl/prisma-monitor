"""Tarifas por modelo y cálculo de costo.

Las tarifas NO están en el código: vienen de `MODEL_PRICES` (JSON, USD por millón de tokens):

    {"gemini-3.1-flash-lite": {"input": 0.0, "cached_input": 0.0, "output": 0.0}}

`cached_input` es opcional; sin él, los tokens en caché se cobran a tarifa de entrada (cota superior).
Un modelo sin tarifa devuelve costo `None`: nunca se muestra 0 cuando en realidad no se sabe.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

_PER = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float
    cached_input: float | None = None


def _number(value, campo: str, modelo: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"MODEL_PRICES: '{campo}' de '{modelo}' debe ser un número >= 0")
    return float(value)


class PriceTable:
    def __init__(self, prices: dict[str, ModelPrice] | None = None):
        self._prices = dict(prices or {})

    @classmethod
    def from_json(cls, raw: str) -> "PriceTable":
        if raw is None or not raw.strip():
            return cls()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"MODEL_PRICES no es JSON válido: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("MODEL_PRICES debe ser un objeto JSON {modelo: {input, output}}")
        prices: dict[str, ModelPrice] = {}
        for modelo, p in data.items():
            if not isinstance(p, dict) or "input" not in p or "output" not in p:
                raise ValueError(f"MODEL_PRICES: '{modelo}' necesita 'input' y 'output'")
            cached = p.get("cached_input")
            prices[modelo] = ModelPrice(
                input=_number(p["input"], "input", modelo),
                output=_number(p["output"], "output", modelo),
                cached_input=None if cached is None else _number(cached, "cached_input", modelo),
            )
        return cls(prices)

    def _lookup(self, model: str) -> ModelPrice | None:
        """Coincidencia exacta o por prefijo más largo (los modelos vienen a veces versionados)."""
        if model in self._prices:
            return self._prices[model]
        candidatos = [k for k in self._prices if model.startswith(k)]
        return self._prices[max(candidatos, key=len)] if candidatos else None

    def has(self, model: str) -> bool:
        return self._lookup(model) is not None

    def cached_at_input_rate(self, model: str) -> bool:
        p = self._lookup(model)
        return p is not None and p.cached_input is None

    def cost(self, model: str, *, input: int, cached: int, output: int, thoughts: int) -> float | None:
        p = self._lookup(model)
        if p is None:
            return None
        cached = max(0, min(cached, input))
        tarifa_cache = p.input if p.cached_input is None else p.cached_input
        return ((input - cached) * p.input + cached * tarifa_cache + (output + thoughts) * p.output) / _PER
