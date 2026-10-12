"""Plain-language confidence verdict: do the trend timeframes and the options market agree?"""
from __future__ import annotations

from dataclasses import dataclass

from .models import SymbolResult
from .trend import direction

OPPOSITE = {"bull": "bear", "bear": "bull", "neutral": "neutral"}
WORD = {"bull": "UP", "bear": "DOWN", "neutral": "SIDEWAYS"}
OPT_WORD = {"bull": "bullish", "bear": "bearish", "neutral": "neutral"}
CONFLICT_SCORE = 40


@dataclass
class Verdict:
    level: str          # HIGH | MEDIUM | LOW | CONFLICT
    direction: str      # bull | bear | neutral
    reason: str

    @property
    def icon(self) -> str:
        return {"HIGH": "✅", "MEDIUM": "🟡", "LOW": "⚪", "CONFLICT": "⚠️"}[self.level]

    def line(self) -> str:
        return f"{self.icon} Confidence {self.level}: {self.reason}"


def options_direction(r: SymbolResult) -> str | None:
    """None when there is no usable options read (missing, thin, or no components yet)."""
    if not r.bias or not r.options or r.options.thin or not r.bias.components:
        return None
    return direction(r.bias.label)


def verdict(r: SymbolResult, timeframes: list[str], profile: str | None = None) -> Verdict:
    """profile: judge that profile's composite instead of the primary trend."""
    if not r.stock or not r.stock.timeframes:
        return Verdict("LOW", "neutral", "no trend data")
    d = direction(r.stock.composites[profile][1] if profile in r.stock.composites else r.stock.label)
    dirs = [direction(r.stock.timeframes[tf].label) for tf in timeframes if tf in r.stock.timeframes]
    agree = sum(x == d for x in dirs)
    against = sum(x == OPPOSITE[d] for x in dirs) if d != "neutral" else 0
    opt = options_direction(r)
    opt_note = "options read thin" if r.options and r.options.thin else "options neutral"

    if d == "neutral":
        return Verdict("LOW", "neutral", "no clear trend. Sideways or mixed; wait for direction")
    if opt == OPPOSITE[d] and abs(r.bias.score) >= CONFLICT_SCORE:
        return Verdict("CONFLICT", d, f"trend {WORD[d]} but options money leans {OPT_WORD[opt]}. Be careful")
    if agree == len(dirs) and opt == d:
        return Verdict("HIGH", d, f"every timeframe {WORD[d]} and options {OPT_WORD[d]}")
    if agree == len(dirs):
        return Verdict("MEDIUM", d, f"every timeframe {WORD[d]}; {opt_note if opt != OPPOSITE[d] else 'options mildly against'}")
    if against == 0 and agree * 2 > len(dirs) and opt != OPPOSITE[d]:
        extra = f", options {OPT_WORD[d]}" if opt == d else ""
        return Verdict("MEDIUM", d, f"most timeframes {WORD[d]}{extra}")
    return Verdict("LOW", d, f"timeframes disagree ({agree} of {len(dirs)} {WORD[d]})")
