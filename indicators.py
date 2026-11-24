"""Indicadores técnicos en Python puro, con la misma semántica que Pine Script v6.

- ta.ema  : alpha = 2/(n+1), se inicializa con el primer valor de la serie.
- ta.rma  : alpha = 1/n, se inicializa con la SMA de las primeras n velas (la usa ta.atr).
- ta.atr  : RMA del True Range (la primera vela usa high-low).
"""
from __future__ import annotations

from typing import List, Optional, Sequence


def ema(values: Sequence[float], length: int) -> List[float]:
    alpha = 2.0 / (length + 1.0)
    out: List[float] = []
    prev: Optional[float] = None
    for v in values:
        prev = v if prev is None else alpha * v + (1.0 - alpha) * prev
        out.append(prev)
    return out


def sma_series(values: Sequence[float], length: int) -> List[Optional[float]]:
    """Media móvil simple; None mientras no haya 'length' datos (como el na de Pine)."""
    out: List[Optional[float]] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= length:
            s -= values[i - length]
        if i >= length - 1:
            out[i] = s / length
    return out


def lowest_series(values: Sequence[float], length: int) -> List[Optional[float]]:
    """ta.lowest(values, length): mínimo de las últimas 'length' velas (incluida la actual)."""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    for i in range(length - 1, n):
        out[i] = min(values[i - length + 1 : i + 1])
    return out


def highest_prev(values: Sequence[float], length: int) -> List[Optional[float]]:
    """ta.highest(values, length)[1]: máximo de las 'length' velas ANTERIORES a la actual."""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    for i in range(length, n):
        out[i] = max(values[i - length : i])
    return out


def atr_series(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], length: int = 14
) -> List[Optional[float]]:
    n = len(close)
    out: List[Optional[float]] = [None] * n
    if n < length:
        return out
    tr: List[float] = []
    for i in range(n):
        if i == 0:
            tr.append(high[0] - low[0])
        else:
            tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    s = sum(tr[:length]) / length
    out[length - 1] = s
    for i in range(length, n):
        s = (s * (length - 1) + tr[i]) / length
        out[i] = s
    return out
