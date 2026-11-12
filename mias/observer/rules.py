"""Frozen Early Watch v1 rules (docs/observer/PREREGISTRATION.md). Do not tune these after seeing results: a change
is a new version with a new pre-registration."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time

VERSION = "early-watch-v1"
ONE = 60_000            # 1-minute bar span, ms
FIVE = 300_000          # 5-minute bar span, ms


@dataclass(frozen=True)
class Rules:
    # structure
    pivot_k: int = 2                    # a 5m pivot needs 2 completed bars on each side; known at the 2nd bar's close
    dev_atr_mult: float = 0.25          # developing HL / LH: a 1m close >= 0.25 x ATR(14, 5m) away from the extreme
    atr_period: int = 14
    ema_fast: int = 9
    ema_slow: int = 21
    # session windows (ET). Main lane: bars starting >= 09:45 and ending <= 15:00. Opening lane (research only):
    # bars starting 09:30-09:45, flagged opening_shadow_only, never actionable.
    session_open: time = time(9, 30)
    session_close: time = time(16, 0)
    or_end: time = time(9, 45)          # opening range = the 09:30-09:45 1-minute bars
    main_start: time = time(9, 45)
    main_end: time = time(15, 0)
    # lifecycle
    watch_minutes: int = 60             # a watch that is not upgraded within 60 min EXPIRES
    cooldown_minutes: int = 30          # after INVALIDATED / EXPIRED, per symbol
    chop_invalidations: int = 2         # 2 invalidations within 60 min -> 60-min chop lock, per symbol
    chop_window_minutes: int = 60
    chop_lock_minutes: int = 60
    max_per_symbol_day: int = 3
    max_per_hour: int = 6               # rolling 60 minutes, whole observer universe (main lane)
    # data
    warm_sessions: int = 10             # prior regular sessions of 1-minute bars used to warm EMAs / ATR / volume
    stale_bars_after_gap: int = 2       # an unfilled gap marks the symbol stale for this many processed bars


RULES = Rules()
