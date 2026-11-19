"""THE V3 ATR definition (one place, used everywhere in V3): 14-period ATR on completed 5-minute bars.

Wilder smoothing over the last 300 completed 5-minute bars, i.e. exactly the value V2 uses for its developing-HL /
LH threshold (mias.indicators.atr on bars5[-300:]), so the detection stays identical. Every V3 Watch stores it.
"""
from __future__ import annotations

from ..indicators import Bar, atr

WINDOW = 300


def atr_5m(bars5: list[Bar], period: int = 14) -> float | None:
    return atr(bars5[-WINDOW:], period)
